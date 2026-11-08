"""Document intelligence commands. Run from ./backend with the database up:

python -m app.docintel.cli extract --symbol ICICIBANK            # unprocessed PDFs of a company
python -m app.docintel.cli extract --symbol ICICIBANK --all      # re-run every PDF
python -m app.docintel.cli extract --document-id <uuid> [--no-promote]
python -m app.docintel.cli show --document-id <uuid>             # statements, periods, checks
python -m app.docintel.cli facts --symbol ICICIBANK [--statement INCOME]
python -m app.docintel.cli ocr-status
"""

import argparse
import shutil
import sys
import time
import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.core.storage import StoragePathError, data_root, resolve_inside, storage_report
from app.docintel.archives import unpack_company_archives
from app.docintel.association import (
    build_issuer_index,
    check_document_text,
    company_label,
    record_check,
    unlink_document,
)
from app.docintel.derive import derive_quarters
from app.docintel.pdf import first_pages_text, ocr_available
from app.docintel.pipeline import PROVIDER_CODE, extract_document
from app.docintel.provider import FINANCIAL_DOC_TYPES, pending_documents
from app.models import (
    Document,
    DocumentExtraction,
    DocumentLinkEvent,
    ExtractedFact,
    ExtractedStatement,
    FinancialFact,
)
from app.providers.cli import find_company


def _print_run(document: Document, run: DocumentExtraction, seconds: float) -> None:
    print(
        f"{(document.title or document.url)[:60]:60} {document.doc_type:22} "
        f"{run.status.value:13} pages={run.page_count:<4} statements={run.statements_found:<3} "
        f"facts={run.facts_extracted:<5} promoted={run.facts_promoted:<4} ({seconds:.1f}s)"
    )
    if run.error:
        print(f"    error: {run.error}")
    counts = Counter(i["code"] for i in (run.issues or []))
    if counts:
        print("    issues: " + ", ".join(f"{code} x{n}" for code, n in counts.most_common()))


def cmd_extract(args) -> int:
    data_dir = data_root()
    with Session(get_engine()) as session:
        if args.document_id:
            doc = session.get(Document, uuid.UUID(args.document_id))
            if doc is None:
                raise SystemExit(f"No document {args.document_id}")
            docs = [doc]
        else:
            company = find_company(session, args.symbol)
            # results kits (ZIP) are opened first so their PDFs are processed like any other
            for archive, outcomes in unpack_company_archives(session, company.id, data_dir):
                stored = sum(1 for o in outcomes if o.status == "STORED")
                if outcomes:
                    print(f"opened {archive.title or archive.url[-50:]}: {stored} new PDF(s)")
            session.commit()
            if args.all:
                docs = list(
                    session.scalars(
                        select(Document).where(
                            Document.company_id == company.id,
                            Document.doc_type.in_(FINANCIAL_DOC_TYPES),
                        )
                    )
                )
            else:
                docs = pending_documents(session, company.id)
            print(f"{company.common_name}: {len(docs)} document(s) to process")
        for doc in docs:
            started = time.monotonic()
            report = extract_document(session, doc, data_dir, promote=not args.no_promote)
            session.commit()
            _print_run(doc, report.extraction, time.monotonic() - started)
    return 0


def cmd_show(args) -> int:
    with Session(get_engine()) as session:
        run = session.scalar(
            select(DocumentExtraction)
            .where(
                DocumentExtraction.document_id == uuid.UUID(args.document_id),
                DocumentExtraction.superseded_at.is_(None),
            )
            .order_by(DocumentExtraction.started_at.desc(), DocumentExtraction.id.desc())
            .limit(1)
        )
        if run is None:
            raise SystemExit("This document has not been processed yet (run `extract`).")
        doc = session.get(Document, run.document_id)
        _print_run(doc, run, 0)
        statements = session.scalars(
            select(ExtractedStatement)
            .where(ExtractedStatement.extraction_id == run.id)
            .order_by(ExtractedStatement.page_number)
        ).all()
        for st in statements:
            checks = st.checks or []
            passed = sum(1 for c in checks if c["passed"])
            print(
                f"\n  page {st.page_number:<4} {st.statement:13} basis={st.basis or '?':12} "
                f"unit={st.unit or '?':8} rows={st.rows_matched} unmatched={st.rows_unmatched} "
                f"checks {passed}/{len(checks)} passed"
            )
            for col in st.columns:
                state = "note" if col["is_note"] else (col["problem"] or "ok")
                period = col["period_end"] or "-"
                months = (
                    f"{col['duration_months']}m"
                    if col["duration_months"]
                    else ("as at" if col["point_in_time"] else "")
                )
                print(
                    f"     col {col['index']}: {period:10} {months:6} {state:30} | "
                    f"{col['header'][:70]}"
                )
            for check in checks:
                if not check["passed"]:
                    print(f"     FAILED: {check['check']} [{check['evaluated']}]")
        reasons = Counter(
            session.scalars(
                select(ExtractedFact.not_promoted_reason).where(
                    ExtractedFact.extraction_id == run.id, ExtractedFact.promoted.is_(False)
                )
            )
        )
        if reasons:
            print("\n  not promoted:")
            for reason, n in reasons.most_common():
                print(f"     {n:5}  {reason}")
    return 0


def cmd_facts(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        query = select(FinancialFact).where(
            FinancialFact.company_id == company.id, FinancialFact.provider_code == PROVIDER_CODE
        )
        if args.statement:
            query = query.where(FinancialFact.statement == args.statement.upper())
        facts = session.scalars(
            query.order_by(
                FinancialFact.statement,
                FinancialFact.basis,
                FinancialFact.period_end.desc(),
                FinancialFact.line_item,
            )
        ).all()
        if not facts:
            print(f"{company.common_name}: no validated values extracted from documents yet.")
            return 1
        for f in facts:
            print(
                f"{f.statement.value:13} {f.basis.value:12} {f.period_label:20} {f.line_item:42} "
                f"{f.original_value:>16} {f.original_unit:14} p.{f.source_page}"
            )
    return 0


def cmd_storage_check(args) -> int:
    with Session(get_engine()) as session:
        report = storage_report(session, verify_hashes=args.verify_hashes)
    print(f"data directory : {report.root}  (exists={report.exists}, writable={report.writable})")
    print(f"documents      : {report.documents} in the database")
    print(f"missing files  : {len(report.missing_files)} {report.missing_files}")
    if report.verified_hashes:
        print(f"hash mismatches: {len(report.hash_mismatches)} {report.hash_mismatches}")
    print(f"unreferenced   : {report.unreferenced_files} file(s) on disk with no database row")
    print("STORAGE OK" if report.healthy else "STORAGE PROBLEM")
    return 0 if report.healthy else 1


def _explain_unlinked(session, doc, index, root) -> None:
    """Read-only: why an unlinked document was unlinked, with any relationship its former
    company's own filing states."""
    event = session.scalar(
        select(DocumentLinkEvent)
        .where(
            DocumentLinkEvent.document_id == doc.id, DocumentLinkEvent.company_before.is_not(None)
        )
        .order_by(DocumentLinkEvent.id.desc())
        .limit(1)
    )
    if event is None:
        return
    try:
        pages = first_pages_text(resolve_inside(root, doc.storage_path))
    except StoragePathError:
        return
    result = check_document_text(session, event.company_before, pages, index)
    former = company_label(session, event.company_before)
    print(f"    was linked to {former}: {result.status} / {result.relation}")
    print(f"    issuer: {result.issuer_name}")
    stated = result.evidence.get("stated_relationship")
    if stated:
        print(
            f"    {former}'s own filing lists it as '{stated['relationship']}' "
            f"(page {stated['page']})"
        )


def cmd_audit_associations(args) -> int:
    """Check, from each document's own first page, that it belongs to the company it is linked to.

    Read-only by default. `--record-status` stores the result on the document row (new metadata
    columns only). `--apply-unlink --approved-by NAME` also unlinks mismatched documents; the file
    stays, the change is written to the audit table `document_link_events`.
    """
    if args.apply_unlink and not args.approved_by:
        raise SystemExit("--apply-unlink needs --approved-by NAME (who approved this change)")
    root = data_root()
    mismatches = 0
    with Session(get_engine()) as session:
        index = build_issuer_index(session)
        for doc in session.scalars(select(Document).order_by(Document.retrieved_at)):
            if doc.company_id is None:
                print(f"{doc.id}  (not linked)  {doc.association_status:10} {doc.title or doc.url}")
                _explain_unlinked(session, doc, index, root)
                continue
            try:
                pages = first_pages_text(resolve_inside(root, doc.storage_path))
            except StoragePathError:
                pages = []
            result = check_document_text(session, doc.company_id, pages, index)
            print(
                f"{doc.id}  linked to {company_label(session, doc.company_id):14} "
                f"-> {result.status:10} {result.relation:22} issuer={result.issuer_name or '-'}"
            )
            print(f"    {doc.doc_type:22} {(doc.title or '')[:70]}")
            print(f"    {result.note}")
            for sentence in result.evidence.get("relation_statements", []):
                print(f"    document says: {sentence}")
            if result.mismatch:
                mismatches += 1
            if args.apply_unlink and result.mismatch:
                report = unlink_document(
                    session,
                    doc,
                    reason=f"first page issued by another entity: {result.note}",
                    actor=f"user-approved:{args.approved_by}",
                    result=result,
                )
                print(
                    "    UNLINKED (file kept). validated values withdrawn: "
                    f"{report.facts_withdrawn}, staged values detached: "
                    f"{report.staged_facts_detached}"
                )
            elif args.record_status:
                record_check(doc, result)
        if args.apply_unlink or args.record_status:
            session.commit()
        else:
            session.rollback()
    changed = args.apply_unlink or args.record_status
    note = "" if changed else "(read-only: nothing was changed)"
    print(f"\n{mismatches} mismatched document(s). {note}")
    return 0


def cmd_derive_quarters(args) -> int:
    """Standalone quarters from cumulative figures (H1 - Q1, 9M - H1, FY - 9M), with checks."""
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        report = derive_quarters(session, company.id, write=not args.dry_run)
        if not args.dry_run:
            session.commit()
    shown = [i for i in report.items if args.all or i.outcome in ("DERIVED", "INCONSISTENT")]
    for item in shown:
        print(
            f"{item.outcome:12} {item.basis.value:12} {item.quarter:9} "
            f"{item.line_item:36} {item.detail}"
        )
    suffix = "  (dry run: nothing written)" if args.dry_run else ""
    print(
        f"\n{company.common_name}: derived={report.count('DERIVED')} "
        f"consistent={report.count('CONSISTENT')} inconsistent={report.count('INCONSISTENT')} "
        f"skipped={report.count('SKIPPED')}{suffix}"
    )
    return 1 if report.count("INCONSISTENT") else 0


def cmd_search(args) -> int:
    """Search the pages of a company's stored documents (what the research agents will use)."""
    from app.retrieval.search import SearchError, search_company_documents

    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        try:
            hits = search_company_documents(session, company.id, args.query, limit=args.limit)
        except SearchError as exc:
            raise SystemExit(str(exc)) from exc
    print(f"{company.common_name}: {len(hits)} page(s) for {args.query!r}")
    for h in hits:
        print(f"  [{h.rank:.2f}] {h.citation} ({h.doc_type}, {h.text_method})")
        print(f"      {h.snippet[:220]}")
    return 0


def cmd_ingest_inbox(args) -> int:
    """Ingest PDFs you saved in data/inbox/<SYMBOL>/ (for sites that refuse automated access)."""
    from app.docintel.inbox import inbox_dir, ingest_inbox

    root = data_root()
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        folder = inbox_dir(root, args.symbol)
        if not folder.is_dir():
            folder.mkdir(parents=True, exist_ok=True)
            print(f"Created {folder}. Save the company's PDFs there, then run this again.")
            return 0
        if args.retry_rejected:
            # Files refused earlier (for example by an older, name-based rule) get another look.
            rejected = folder / "rejected"
            for path in sorted(rejected.glob("*.pdf")) if rejected.is_dir() else []:
                shutil.move(str(path), str(folder / path.name))
                reason = rejected / (path.name + ".reason.txt")
                if reason.exists():
                    reason.unlink()
                print(f"re-examining {path.name}")
        outcomes = ingest_inbox(session, company.id, root, args.symbol)
        session.commit()
    for o in outcomes:
        print(f"{o.status:15} {o.file[:50]:50} {o.doc_type or '-':22} {o.detail}")
    if not outcomes:
        print(f"{folder} has no new files.")
    else:
        print(f"\nNext: python -m app.docintel.cli extract --symbol {args.symbol}")
    return 1 if any(o.status == "REFUSED" for o in outcomes) else 0


def cmd_ocr_status(_args) -> int:
    if ocr_available():
        from app.docintel.ocr import tesseract_version

        print(f"OCR available ({tesseract_version()}): scanned pages will be read with it.")
        return 0
    print(
        "OCR NOT available: scanned pages are reported as unreadable, never guessed.\n"
        "To enable, install Tesseract (Windows: the UB Mannheim build) and make sure `tesseract` "
        "is on PATH, or set TESSDATA_PREFIX."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m app.docintel.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("extract")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbol")
    group.add_argument("--document-id")
    p.add_argument("--all", action="store_true", help="re-run documents already processed")
    p.add_argument("--no-promote", action="store_true", help="stage values only")
    p = sub.add_parser("show")
    p.add_argument("--document-id", required=True)
    p = sub.add_parser("facts")
    p.add_argument("--symbol", required=True)
    p.add_argument("--statement", choices=["INCOME", "BALANCE_SHEET", "CASH_FLOW"])
    sub.add_parser("ocr-status")
    p = sub.add_parser("search")
    p.add_argument("--symbol", required=True)
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p = sub.add_parser("ingest-inbox")
    p.add_argument("--symbol", required=True)
    p.add_argument(
        "--retry-rejected", action="store_true", help="re-examine files in rejected/ by content"
    )
    p = sub.add_parser("derive-quarters")
    p.add_argument("--symbol", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--all", action="store_true", help="also list consistent and skipped items")
    p = sub.add_parser("audit-associations")
    p.add_argument("--record-status", action="store_true", help="store the check on each document")
    p.add_argument("--apply-unlink", action="store_true", help="unlink mismatched documents")
    p.add_argument(
        "--approved-by", help="who approved --apply-unlink (recorded in the audit table)"
    )
    p = sub.add_parser("storage-check")
    p.add_argument("--verify-hashes", action="store_true", help="re-hash every stored file")
    args = parser.parse_args(argv)
    handlers = {
        "extract": cmd_extract,
        "show": cmd_show,
        "facts": cmd_facts,
        "ocr-status": cmd_ocr_status,
        "storage-check": cmd_storage_check,
        "ingest-inbox": cmd_ingest_inbox,
        "search": cmd_search,
        "derive-quarters": cmd_derive_quarters,
        "audit-associations": cmd_audit_associations,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
