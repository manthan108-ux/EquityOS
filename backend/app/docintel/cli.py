"""Document intelligence commands. Run from ./backend with the database up:

python -m app.docintel.cli extract --symbol ICICIBANK            # unprocessed PDFs of a company
python -m app.docintel.cli extract --symbol ICICIBANK --all      # re-run every PDF
python -m app.docintel.cli extract --document-id <uuid> [--no-promote]
python -m app.docintel.cli show --document-id <uuid>             # statements, periods, checks
python -m app.docintel.cli facts --symbol ICICIBANK [--statement INCOME]
python -m app.docintel.cli ocr-status
"""

import argparse
import sys
import time
import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_data_dir
from app.core.database import get_engine
from app.docintel.pdf import ocr_available
from app.docintel.pipeline import PROVIDER_CODE, extract_document
from app.docintel.provider import FINANCIAL_DOC_TYPES, pending_documents
from app.models import (
    Document,
    DocumentExtraction,
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
    data_dir = resolve_data_dir(get_settings())
    with Session(get_engine()) as session:
        if args.document_id:
            doc = session.get(Document, uuid.UUID(args.document_id))
            if doc is None:
                raise SystemExit(f"No document {args.document_id}")
            docs = [doc]
        else:
            company = find_company(session, args.symbol)
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
            .where(DocumentExtraction.document_id == uuid.UUID(args.document_id))
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


def cmd_ocr_status(_args) -> int:
    if ocr_available():
        print("OCR available: scanned pages will be read with Tesseract.")
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
    args = parser.parse_args(argv)
    handlers = {
        "extract": cmd_extract,
        "show": cmd_show,
        "facts": cmd_facts,
        "ocr-status": cmd_ocr_status,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
