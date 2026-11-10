"""The company evidence vault: accepting, checking and organising user-supplied documents.

Nothing a user uploads is trusted. Each file goes through these stages, and the outcome of every
one is recorded on its `vault_entries` row:

1. FORMAT   what the file really is (magic bytes / archive structure / text), never its extension;
            size limit; corrupt, encrypted or unsafe files are refused.
2. DUPLICATE the SHA-256 is compared with everything already stored; a duplicate is not processed
            again and the user is told where the existing copy is.
3. ISSUER   a PDF's own first page must name THIS company; a different issuer is refused, an
            unreadable one needs the user's confirmation before anything is read from it.
4. TYPE     the document type, period, basis and accounting standard are read from the content.
            Anything the system cannot decide is shown as an ambiguity and is not guessed.
5. EXTRACT  the existing document-intelligence pipeline reads the statements, with page-level
            evidence and its own arithmetic checks.
6. CONFLICTS every value read is compared with what is already stored; disagreements are listed.
7. LICENCE  only a public filing (issuer confirmed, or attested for tabular files) feeds the shared
            evidence profile; anything else is kept privately in the vault and used by nobody.

Both a synchronous API (`receive_upload`, `process_entry`) and the asynchronous job used by the web
API are the same code; the job only runs it on a worker thread with its own database session.
"""

import csv
import hashlib
import io
import re
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_data_dir
from app.docintel import taxonomy
from app.docintel.association import add_event
from app.docintel.inbox import guess_doc_type
from app.docintel.pdf import first_pages_text, page_count
from app.docintel.pipeline import extract_document
from app.evidence.engine import validate_company
from app.models import (
    Company,
    Document,
    DocumentExtraction,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    ExtractedFact,
    ExtractedStatement,
    Security,
    VaultEntry,
)
from app.providers.documents import ALLOWED_DOC_TYPES, AssociationMismatch, store_document_bytes
from app.providers.manual import REQUIRED_COLUMNS, ManualImportError, import_financials_csv
from app.research import clock
from app.research import readiness as rd

UPLOAD_SOURCE = "USER_UPLOAD"
PUBLIC_FILING = "PUBLIC_FILING"
PRIVATE_LICENCES = ("USER_LICENSED", "UNDECLARED")
LICENCES = (PUBLIC_FILING, *PRIVATE_LICENCES)
MAX_XLSX_UNCOMPRESSED = 100 * 1024 * 1024
MAX_XLSX_ENTRIES = 2000
MAX_TABLE_ROWS = 50_000
CONFLICT_PCT = 0.5  # a difference above this many percent is reported as a conflict
STALE_DOCUMENT_DAYS = 548

# vault statuses
RECEIVED, PROCESSING, ACCEPTED, HELD_PRIVATE = "RECEIVED", "PROCESSING", "ACCEPTED", "HELD_PRIVATE"
NEEDS_CLARIFICATION, REJECTED, DUPLICATE, FAILED = (
    "NEEDS_CLARIFICATION",
    "REJECTED",
    "DUPLICATE",
    "FAILED",
)
FINISHED = (ACCEPTED, HELD_PRIVATE, REJECTED, DUPLICATE, FAILED)

_IND_AS = re.compile(r"\bind\s?as\b|indian accounting standards?", re.I)
_IGAAP = re.compile(
    r"indian gaap|accounting standards? \(as\)|companies \(accounting standards\)", re.I
)


# ---------------------------------------------------------------------------- stage 1: format


@dataclass
class Inspected:
    kind: str | None  # pdf | xlsx | csv
    mime: str | None
    problem: str | None = None
    pages: int | None = None


_MIME = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}


def max_upload_bytes() -> int:
    return get_settings().research_max_upload_mb * 1024 * 1024


def inspect_upload(filename: str, body: bytes) -> Inspected:
    """What the file really is, from its content. The extension is only compared, never trusted."""
    if not body:
        return Inspected(None, None, "the file is empty")
    if len(body) > max_upload_bytes():
        return Inspected(
            None, None, f"the file is larger than {get_settings().research_max_upload_mb} MB"
        )
    kind = _sniff(body)
    if kind is None:
        return Inspected(
            None,
            None,
            "unsupported or unrecognised content: only PDF, XLSX and CSV files are accepted",
        )
    if isinstance(kind, tuple):  # (None, reason)
        return Inspected(None, None, kind[1])
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext in ("pdf", "xlsx", "csv") and ext != kind:
        return Inspected(
            None,
            _MIME[kind],
            f"the file name says .{ext} but its content is {kind.upper()}; rename or re-export it",
        )
    if kind == "pdf":
        pages = page_count(body)
        if not pages:
            return Inspected(None, _MIME[kind], "the PDF is corrupt, encrypted or has no pages")
        return Inspected("pdf", _MIME[kind], None, pages)
    if kind == "xlsx":
        problem = _check_xlsx(body)
        return Inspected(None if problem else "xlsx", _MIME[kind], problem)
    problem = _check_csv(body)
    return Inspected(None if problem else "csv", _MIME[kind], problem)


def _sniff(body: bytes):
    head = body[:8]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return (None, "the file looks like a spreadsheet but is corrupt")
        if "xl/workbook.xml" in names and "[Content_Types].xml" in names:
            return "xlsx"
        return (None, "ZIP archives are not accepted here; upload the PDF, XLSX or CSV itself")
    if b"\x00" in body[:4096]:
        return None
    lowered = body[:512].lstrip().lower()
    if lowered.startswith((b"<html", b"<!doc", b"<?xml", b"<script", b"mz")) or body[:2] == b"MZ":
        return None
    try:
        body[:65536].decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return "csv" if b"," in body[:4096] and b"\n" in body[:65536] else None


def _check_xlsx(body: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            infos = z.infolist()
            if len(infos) > MAX_XLSX_ENTRIES:
                return "the spreadsheet contains too many parts"
            if sum(i.file_size for i in infos) > MAX_XLSX_UNCOMPRESSED:
                return "the spreadsheet expands to an unsafe size"
            if any(i.filename.endswith("vbaProject.bin") for i in infos):
                return "spreadsheets with macros are not accepted"
    except zipfile.BadZipFile:
        return "the spreadsheet is corrupt"
    return None


def _check_csv(body: bytes) -> str | None:
    text = body.decode("utf-8-sig", errors="replace")
    first = text.splitlines()[0] if text.strip() else ""
    header = {h.strip().lower() for h in next(csv.reader([first]), [])}
    missing = REQUIRED_COLUMNS - header
    if missing:
        return "this CSV is not in the EquityOS financials format; missing column(s): " + ", ".join(
            sorted(missing)
        )
    return None


def xlsx_to_csv(body: bytes) -> bytes:
    """The first worksheet as CSV text (values only; formulas are never evaluated)."""
    from openpyxl import load_workbook  # noqa: PLC0415

    wb = load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    out = io.StringIO()
    writer = csv.writer(out)
    for n, row in enumerate(ws.iter_rows(values_only=True)):
        if n > MAX_TABLE_ROWS:
            raise ValueError("the spreadsheet has more rows than the limit")
        cells = []
        for v in row:
            if v is None:
                cells.append("")
            elif isinstance(v, datetime):
                cells.append(v.date().isoformat())
            elif isinstance(v, date):
                cells.append(v.isoformat())
            else:
                cells.append(str(v))
        if any(c.strip() for c in cells):
            writer.writerow(cells)
    wb.close()
    return out.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------- receiving


def fiscal_labels(period_end: date | None, months: int | None) -> tuple[str | None, str | None]:
    """(fiscal year label, quarter label) for a period; the quarter is None for a full year."""
    if period_end is None:
        return None, None
    fy = rd.fiscal_year(period_end)
    if months == 12:
        return fy, None
    quarter = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}.get(period_end.month)
    if months == 6:
        quarter = "H1"
    elif months == 9:
        quarter = "9M"
    return fy, quarter


def _entry(
    company_id: uuid.UUID,
    session_id: uuid.UUID | None,
    owner: str,
    filename: str,
    body: bytes,
    licence: str,
) -> VaultEntry:
    return VaultEntry(
        company_id=company_id,
        session_id=session_id,
        original_filename=Path(filename).name[:300] or "upload",
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        status=RECEIVED,
        upload_source=UPLOAD_SOURCE,
        uploaded_by=owner,
        licence_class=licence if licence in LICENCES else "UNDECLARED",
        visibility="PRIVATE",
        detected={"steps": [], "warnings": []},
    )


def _log(entry: VaultEntry, stage: str, ok: bool, detail: str) -> None:
    detected = dict(entry.detected or {"steps": [], "warnings": []})
    detected["steps"] = [*detected.get("steps", []), {"stage": stage, "ok": ok, "detail": detail}]
    entry.detected = detected


def _warn(entry: VaultEntry, text: str) -> None:
    detected = dict(entry.detected or {"steps": [], "warnings": []})
    detected["warnings"] = [*detected.get("warnings", []), text]
    entry.detected = detected


def _reject(entry: VaultEntry, stage: str, reason: str) -> VaultEntry:
    entry.status = REJECTED
    entry.reject_reason = reason
    _log(entry, stage, False, reason)
    return entry


def receive_upload(
    session: Session,
    company: Company,
    *,
    owner: str,
    filename: str,
    body: bytes,
    licence: str = "UNDECLARED",
    doc_type: str | None = None,
    session_id: uuid.UUID | None = None,
    data_dir: Path | None = None,
) -> VaultEntry:
    """Stages 1-3 (fast, done inside the request). Returns an entry that is RECEIVED, REJECTED or
    DUPLICATE; processing of a RECEIVED entry (`process_entry`) can then run in the background."""
    data_dir = data_dir or resolve_data_dir(get_settings())
    entry = _entry(company.id, session_id, owner, filename, body, licence)
    session.add(entry)
    session.flush()

    info = inspect_upload(filename, body)
    entry.detected_mime = info.mime
    if info.kind is None:
        return _reject(entry, "FORMAT", info.problem or "unsupported file")
    _log(entry, "FORMAT", True, f"{info.kind.upper()} verified from content ({info.mime})")
    entry.detected = {**entry.detected, "kind": info.kind, "pages": info.pages}

    earlier = session.scalar(
        select(VaultEntry)
        .where(
            VaultEntry.sha256 == entry.sha256,
            VaultEntry.id != entry.id,
            VaultEntry.status.in_(
                (ACCEPTED, HELD_PRIVATE, NEEDS_CLARIFICATION, RECEIVED, PROCESSING)
            ),
        )
        .order_by(VaultEntry.uploaded_at)
    )
    if earlier is not None:
        if earlier.company_id != company.id:
            return _reject(
                entry,
                "DUPLICATE",
                "this exact file is already stored against a different company; it cannot be "
                "attached to this one",
            )
        entry.status = DUPLICATE
        entry.document_id = earlier.document_id
        entry.supersedes_id = earlier.id
        entry.reject_reason = (
            f"this exact file was already uploaded on {earlier.uploaded_at:%d %b %Y} "
            f"(status {earlier.status}); nothing was processed again"
        )
        _log(entry, "DUPLICATE", True, entry.reject_reason)
        return entry

    if info.kind == "pdf":
        existing = session.scalar(select(Document).where(Document.sha256 == entry.sha256))
        if existing is not None and existing.company_id not in (None, company.id):
            return _reject(
                entry,
                "DUPLICATE",
                "this exact file is already stored against a different company",
            )
        chosen = doc_type if doc_type in ALLOWED_DOC_TYPES else None
        pages = first_pages_text(body, 3)
        kind = chosen or guess_doc_type(entry.original_filename, pages, info.pages)
        try:
            document, created = store_document_bytes(
                session,
                data_dir,
                body,
                url=f"user-upload://{entry.sha256[:16]}",
                doc_type=kind,
                source_code="MANUAL_UPLOAD",
                company_id=company.id,
                title=entry.original_filename,
                on_mismatch="raise",
            )
        except AssociationMismatch as exc:
            return _reject(
                entry,
                "ISSUER",
                f"this document was issued by '{exc.check.issuer_name or 'another entity'}', "
                f"not by {company.common_name}: {exc.check.note}",
            )
        except ValueError as exc:
            return _reject(entry, "STORE", f"not stored: {exc}")
        entry.document_id = document.id
        entry.category = document.doc_type
        entry.issuer_status = document.association_status
        entry.issuer_name = document.issuer_name
        _log(
            entry,
            "ISSUER",
            True,
            f"issuer {document.association_status}"
            + (f" ({document.issuer_name})" if document.issuer_name else ""),
        )
        if not created:
            _log(entry, "STORE", True, "EquityOS already held this exact file; it is reused")
    else:
        relative = Path("vault") / entry.sha256[:2] / f"{entry.sha256}.{info.kind}"
        target = data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        entry.category = "financial_data_table"
        entry.issuer_status = "UNVERIFIED"
        entry.detected = {**entry.detected, "stored_as": relative.as_posix()}
        _log(entry, "STORE", True, "table stored in the vault (its issuer cannot be read)")
    return entry


# ---------------------------------------------------------------------------- processing


def _standard_of(texts: list[str]) -> str:
    joined = "\n".join(texts)
    if _IND_AS.search(joined):
        return "IND_AS"
    if _IGAAP.search(joined):
        return "IGAAP"
    return "UNKNOWN"


def _next_version(session: Session, entry: VaultEntry) -> int:
    n = session.scalar(
        select(func.count())
        .select_from(VaultEntry)
        .where(
            VaultEntry.company_id == entry.company_id,
            VaultEntry.id != entry.id,
            VaultEntry.category == entry.category,
            VaultEntry.fiscal_year == entry.fiscal_year,
            VaultEntry.quarter.is_(entry.quarter)
            if entry.quarter is None
            else VaultEntry.quarter == entry.quarter,
            VaultEntry.basis == entry.basis,
            VaultEntry.status.in_((ACCEPTED, HELD_PRIVATE)),
        )
    )
    return (n or 0) + 1


def _extraction_conflicts(session: Session, extraction_id: int) -> list[dict]:
    """Disagreements the extractor met with documents already stored (it keeps the newer one and
    records the other in the fact's notes; here they are surfaced to the uploader)."""
    found = []
    rows = session.scalars(
        select(ExtractedFact).where(
            ExtractedFact.extraction_id == extraction_id,
            ExtractedFact.validation_notes.is_not(None),
        )
    )
    for fact in rows:
        notes = [n for n in (fact.validation_notes or []) if isinstance(n, str)]
        if not any("differs from" in n for n in notes):
            continue
        found.append(
            {
                "metric": f"{fact.statement}.{fact.canonical_item}",
                "period_end": fact.period_end.isoformat() if fact.period_end else None,
                "basis": fact.basis,
                "this_value": str(fact.original_value),
                "difference_pct": None,  # the extractor records the other value in the note
                "page": fact.page_number,
                "selected": "THIS_UPLOAD" if fact.promoted else "EXISTING",
                "note": "; ".join(notes),
            }
        )
    return found[:50]


def _conflicts(session: Session, company_id: uuid.UUID, evidence_ids: set[int]) -> list[dict]:
    if not evidence_ids:
        return []
    found = []
    rows = session.scalars(
        select(EvidenceResolution).where(
            EvidenceResolution.company_id == company_id,
            EvidenceResolution.max_difference_pct.is_not(None),
            EvidenceResolution.max_difference_pct > CONFLICT_PCT,
        )
    )
    for res in rows:
        alts = res.alternatives or []
        involved = res.selected_evidence_id in evidence_ids or any(
            a.get("evidence_id") in evidence_ids for a in alts
        )
        if not involved:
            continue
        found.append(
            {
                "metric": res.metric,
                "period_end": res.period_end.isoformat(),
                "basis": res.basis or None,
                "difference_pct": str(res.max_difference_pct),
                "selected": "THIS_UPLOAD"
                if res.selected_evidence_id in evidence_ids
                else "EXISTING",
                "alternatives": alts[:4],
                "note": res.note,
            }
        )
    return found[:50]


def _readiness_ids(session: Session, company: Company) -> set[str]:
    return {s.req.id for s in rd.evaluate(session, company).statuses if s.ok}


def process_entry(
    session: Session,
    entry_id: uuid.UUID,
    *,
    data_dir: Path | None = None,
    llm_router=None,
    force_extract: bool = False,
) -> VaultEntry:
    """Stages 4-7. Safe to call again (e.g. after a clarification): it re-reads the entry state."""
    data_dir = data_dir or resolve_data_dir(get_settings())
    entry = session.get(VaultEntry, entry_id)
    if entry is None:
        raise ValueError("no such vault entry")
    if entry.status in (REJECTED, DUPLICATE):
        return entry
    company = session.get(Company, entry.company_id)
    entry.status = PROCESSING
    entry.ambiguities = None
    session.flush()
    try:
        if (entry.detected or {}).get("kind") == "pdf":
            return _process_pdf(session, entry, company, data_dir, llm_router)
        return _process_table(session, entry, company, data_dir)
    except Exception as exc:  # noqa: BLE001 - recorded on the entry; a bad file must not kill the job
        entry.status = FAILED
        entry.reject_reason = (
            f"processing failed unexpectedly ({type(exc).__name__}); nothing was kept"
        )
        _log(entry, "PROCESS", False, entry.reject_reason)
        return entry


def _ambiguity(code: str, message: str, options: list[str] | None = None) -> dict:
    return {"code": code, "message": message, "options": options or []}


def _process_pdf(
    session: Session, entry: VaultEntry, company: Company, data_dir: Path, llm_router
) -> VaultEntry:
    document = session.get(Document, entry.document_id)
    ambiguities: list[dict] = []
    answers = entry.clarification or {}

    if document.doc_type == "other" and not answers.get("doc_type"):
        ambiguities.append(
            _ambiguity(
                "DOC_TYPE_UNKNOWN",
                "the type of this document could not be determined from its content",
                [
                    "annual_report",
                    "quarterly_result",
                    "investor_presentation",
                    "earnings_transcript",
                ],
            )
        )
    if answers.get("doc_type") in ALLOWED_DOC_TYPES and answers["doc_type"] != document.doc_type:
        document.doc_type = answers["doc_type"]
        entry.category = document.doc_type
        _log(entry, "TYPE", True, f"type set to {document.doc_type} by the uploader")
    if document.association_status == "UNVERIFIED":
        if answers.get("confirm_issuer") is True:
            document.association_status = "FORCED"
            document.association_note = (
                "issuer could not be read from the document; confirmed by the uploader"
            )
            add_event(
                session,
                document,
                before=None,
                after=company.id,
                action="LINKED",
                reason="issuer unreadable; the uploader confirmed it is this company's document",
                actor="user-approved",
                evidence={"uploaded_by": entry.uploaded_by},
            )
            entry.issuer_status = "FORCED"
            _log(entry, "ISSUER", True, "issuer confirmed by the uploader")
        else:
            ambiguities.append(
                _ambiguity(
                    "ISSUER_UNCONFIRMED",
                    "the company that issued this document could not be read from it (for "
                    "example a scan or an image cover). Confirm that it is "
                    f"{company.common_name}'s own document",
                    ["confirm_issuer"],
                )
            )
    if ambiguities:
        entry.status = NEEDS_CLARIFICATION
        entry.ambiguities = ambiguities
        _log(entry, "CLARIFY", False, f"{len(ambiguities)} question(s) need an answer")
        return entry

    before = _readiness_ids(session, company)
    shared = entry.licence_class == PUBLIC_FILING
    # Private documents are read too (to organise them) but nothing is promoted to shared evidence.
    report = extract_document(session, document, data_dir, promote=shared, llm_router=llm_router)
    run = report.extraction
    entry.extraction_status = run.status.value
    _log(
        entry,
        "EXTRACT",
        run.status.value == "COMPLETED",
        f"{run.status.value}: {run.statements_found} statement table(s), "
        f"{run.facts_extracted} value(s) read, {run.facts_promoted} accepted",
    )

    statements = list(
        session.scalars(
            select(ExtractedStatement).where(ExtractedStatement.extraction_id == run.id)
        )
    )
    ends: list[tuple[date, int | None]] = []
    bases = set()
    for st in statements:
        if st.basis:
            bases.add(st.basis)
        for col in st.columns or []:
            if col.get("period_end") and col.get("duration_months") and not col.get("is_note"):
                ends.append((date.fromisoformat(col["period_end"]), col["duration_months"]))
    if ends:
        newest = max(ends)
        entry.period_end = newest[0].isoformat()
        entry.fiscal_year, entry.quarter = fiscal_labels(*newest)
        if document.financial_period is None:
            document.financial_period = (
                entry.fiscal_year
                if entry.quarter is None
                else (f"{entry.fiscal_year}-{entry.quarter}")
            )
        if (clock.today() - newest[0]).days > STALE_DOCUMENT_DAYS:
            _warn(
                entry,
                f"this document covers {entry.fiscal_year}, which is too old to settle current "
                "requirements; it is kept as history only",
            )
    if len(bases) == 1:
        entry.basis = next(iter(bases))
    elif len(bases) > 1:
        entry.basis = "BOTH"
        _log(
            entry,
            "BASIS",
            True,
            "consolidated and standalone statements are both present; kept apart",
        )
    pages = first_pages_text(data_dir / document.storage_path, 4)
    entry.accounting_standard = _standard_of(pages)
    entry.version = _next_version(session, entry)

    if not shared:
        entry.status = HELD_PRIVATE
        entry.visibility = "PRIVATE"
        entry.validation_status = None
        _log(
            entry,
            "LICENCE",
            True,
            "kept privately: only documents declared as public filings feed the shared evidence",
        )
        return entry

    if run.status.value in ("NEEDS_OCR", "NO_STATEMENTS", "UNSUPPORTED", "FAILED"):
        reason = {
            "NEEDS_OCR": "the pages are scans without a text layer and OCR is not available here",
            "NO_STATEMENTS": "no financial statement or ratio table could be recognised",
            "UNSUPPORTED": "the PDF is encrypted or cannot be read",
            "FAILED": "extraction failed; nothing was kept",
        }[run.status.value]
        entry.status = FAILED if run.status.value == "FAILED" else REJECTED
        entry.reject_reason = f"no usable figures: {reason}"
        entry.validation_status = "FAILED"
        _log(entry, "EXTRACT", False, entry.reject_reason)
        return entry

    return _finish(session, entry, company, document, run, before)


def _finish(
    session: Session,
    entry: VaultEntry,
    company: Company,
    document: Document,
    run: DocumentExtraction,
    before: set[str],
) -> VaultEntry:
    validate_company(session, company.id)
    ids = set(
        session.scalars(
            select(EvidenceRecord.id).where(
                EvidenceRecord.company_id == company.id,
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
                EvidenceRecord.document_id == document.id,
            )
        )
    )
    metrics = (
        sorted(
            {
                m
                for (m,) in session.execute(
                    select(EvidenceRecord.metric).where(EvidenceRecord.id.in_(ids))
                )
            }
        )
        if ids
        else []
    )
    entry.evidence_refs = [{"count": len(ids), "metrics": metrics[:60]}]
    entry.conflicts = [
        *_extraction_conflicts(session, run.id),
        *_conflicts(session, company.id, ids),
    ]
    blocked = session.scalar(
        select(func.count())
        .select_from(EvidenceResolution)
        .where(
            EvidenceResolution.company_id == company.id,
            EvidenceResolution.blocked.is_(True),
            EvidenceResolution.selected_evidence_id.in_(ids or {-1}),
        )
    )
    if not ids:
        why = Counter(
            r
            for (r,) in session.execute(
                select(ExtractedFact.not_promoted_reason).where(
                    ExtractedFact.extraction_id == run.id,
                    ExtractedFact.promoted.is_(False),
                    ExtractedFact.not_promoted_reason.is_not(None),
                )
            )
        )
        top = "; ".join(f"{r} (x{n})" for r, n in why.most_common(3))
        entry.status = REJECTED
        entry.reject_reason = (
            "figures were found but none passed the validation rules, so none were accepted"
            + (f". Main reasons: {top}" if top else "")
        )
        entry.validation_status = "FAILED"
        _log(entry, "VALIDATE", False, entry.reject_reason)
        return entry
    refused = Counter(
        r
        for (r,) in session.execute(
            select(ExtractedFact.not_promoted_reason).where(
                ExtractedFact.extraction_id == run.id,
                ExtractedFact.promoted.is_(False),
                ExtractedFact.not_promoted_reason.is_not(None),
                ExtractedFact.statement.in_(("INCOME", "BALANCE_SHEET", "CASH_FLOW", "RATIOS")),
            )
        )
    )
    # a cell that duplicates one already stored is not a refusal
    refused = Counter({r: n for r, n in refused.items() if "already promoted" not in r})
    if refused:
        detected = dict(entry.detected or {})
        detected["not_accepted"] = {
            "count": sum(refused.values()),
            "reasons": [f"{r} (x{n})" for r, n in refused.most_common(5)],
        }
        entry.detected = detected
        _warn(
            entry,
            f"{sum(refused.values())} value(s) were read but not accepted, so they are not "
            "used; see the reasons on this upload",
        )
    entry.validation_status = "PARTIAL" if (blocked or entry.conflicts) else "PASSED"
    entry.status = ACCEPTED
    entry.visibility = "SHARED"
    _log(
        entry,
        "VALIDATE",
        not blocked,
        f"{len(ids)} evidence record(s); {blocked or 0} blocked by validation; "
        f"{len(entry.conflicts)} disagreement(s) with stored values",
    )
    after = _readiness_ids(session, company)
    entry.resolved_requirements = sorted(after - before)
    return entry


def _process_table(
    session: Session, entry: VaultEntry, company: Company, data_dir: Path
) -> VaultEntry:
    detected = entry.detected or {}
    path = data_dir / detected["stored_as"]
    body = path.read_bytes()
    if detected["kind"] == "xlsx":
        try:
            body = xlsx_to_csv(body)
        except Exception as exc:  # noqa: BLE001 - corrupt workbook
            entry.status = REJECTED
            entry.reject_reason = f"the spreadsheet could not be read ({type(exc).__name__})"
            _log(entry, "EXTRACT", False, entry.reject_reason)
            return entry

    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    isins = {(r.get("isin") or "").strip().upper() for r in reader}
    own = set(session.scalars(select(Security.isin).where(Security.company_id == company.id)))
    foreign = sorted(i for i in isins if i and i not in own)
    if foreign:
        entry.status = REJECTED
        entry.reject_reason = (
            f"the table contains rows for ISIN(s) that are not {company.common_name}'s: "
            + ", ".join(foreign[:5])
        )
        entry.issuer_status = "MISMATCH"
        _log(entry, "ISSUER", False, entry.reject_reason)
        return entry

    unknown = sorted(
        {
            (r.get("line_item") or "").strip()
            for r in csv.DictReader(io.StringIO(text))
            if (r.get("line_item") or "").strip().lower() not in taxonomy.BY_KEY
        }
    )
    if unknown:
        entry.status = REJECTED
        entry.reject_reason = (
            "unrecognised line item(s): "
            + ", ".join(unknown[:8])
            + ". Use EquityOS line-item names (for example revenue_from_operations, net_profit, "
            "total_assets); the full list is at GET /api/research/template"
        )
        _log(entry, "VALIDATE", False, entry.reject_reason)
        return entry

    attested = entry.licence_class == PUBLIC_FILING
    answers = entry.clarification or {}
    if attested and not (answers.get("source_reference") or "").strip():
        entry.status = NEEDS_CLARIFICATION
        entry.ambiguities = [
            _ambiguity(
                "SOURCE_REFERENCE_REQUIRED",
                "a table cannot be checked against its issuer. Say which published filing the "
                "figures come from (for example 'Q2 FY27 results filed on BSE on 30 Oct 2026')",
                ["source_reference"],
            )
        ]
        _log(entry, "CLARIFY", False, "source reference needed")
        return entry
    if not attested:
        entry.status = HELD_PRIVATE
        _log(
            entry,
            "LICENCE",
            True,
            "format verified and stored privately; not imported into the shared evidence",
        )
        return entry

    before = _readiness_ids(session, company)
    try:
        report = import_financials_csv(session, _canonical_lines(body), entry.original_filename)
    except ManualImportError as exc:
        entry.status = REJECTED
        entry.reject_reason = "the table failed validation: " + str(exc)
        entry.validation_status = "FAILED"
        _log(entry, "VALIDATE", False, entry.reject_reason)
        return entry
    entry.extraction_status = "COMPLETED"
    periods = report.periods
    if periods:
        newest = date.fromisoformat(periods[0])
        entry.period_end = periods[0]
        entry.fiscal_year, entry.quarter = fiscal_labels(newest, None)
        if (clock.today() - newest).days > STALE_DOCUMENT_DAYS:
            _warn(
                entry, "the newest period in this table is too old to settle current requirements"
            )
    entry.accounting_standard = "UNKNOWN"
    entry.issuer_status = "UNVERIFIED"
    _log(entry, "EXTRACT", True, f"{report.rows} value(s) imported for {len(periods)} period(s)")
    _warn(
        entry,
        "values from tables are recorded at the lowest source authority; a value read from the "
        "company's own filing outranks them if the two disagree",
    )
    return _finish_table(session, entry, company, before, report.periods)


def _canonical_lines(body: bytes) -> bytes:
    """The same table with line items lower-cased to their canonical key (already validated)."""
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig", errors="replace"))))
    header = [h.strip().lower() for h in rows[0]]
    col = header.index("line_item")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(rows[0])
    for row in rows[1:]:
        if len(row) > col:
            row[col] = row[col].strip().lower()
        writer.writerow(row)
    return out.getvalue().encode("utf-8")


def template() -> dict:
    """What a table upload must look like (served at GET /api/research/template)."""
    by_statement: dict[str, list[str]] = {}
    for key, item in sorted(taxonomy.BY_KEY.items()):
        by_statement.setdefault(item.statement, []).append(key)
    return {
        "columns": sorted(REQUIRED_COLUMNS),
        "statement": ["INCOME", "BALANCE_SHEET", "CASH_FLOW"],
        "period_type": ["ANNUAL", "QUARTERLY"],
        "basis": ["CONSOLIDATED", "STANDALONE"],
        "unit": ["crore", "lakh", "million", "billion", "thousand", "inr", "inr_per_share"],
        "line_items": by_statement,
        "example": (
            "isin,statement,line_item,period_end,period_type,basis,value,unit\n"
            "INE002A01018,INCOME,revenue_from_operations,2026-03-31,ANNUAL,CONSOLIDATED,1900,crore"
        ),
        "rules": [
            "one value per row; a blank or '-' value is rejected (missing is not zero)",
            "every ISIN must belong to the company being researched",
            "the whole file is rejected if any row is invalid",
            "say which published filing the figures come from; a value read from the filing "
            "itself outranks a table value if they disagree",
        ],
    }


def _finish_table(
    session: Session, entry: VaultEntry, company: Company, before: set[str], periods: list[str]
) -> VaultEntry:
    validate_company(session, company.id)
    ids = set(
        session.scalars(
            select(EvidenceRecord.id).where(
                EvidenceRecord.company_id == company.id,
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
                EvidenceRecord.provider_code == "MANUAL_IMPORT",
            )
        )
    )
    entry.evidence_refs = [{"count": len(ids), "periods": periods[:12]}]
    entry.conflicts = _conflicts(session, company.id, ids)
    entry.validation_status = "PARTIAL" if entry.conflicts else "PASSED"
    entry.status = ACCEPTED
    entry.visibility = "SHARED"
    _log(entry, "VALIDATE", True, f"{len(entry.conflicts)} disagreement(s) with stored values")
    entry.resolved_requirements = sorted(_readiness_ids(session, company) - before)
    return entry


def clarify(
    session: Session, entry_id: uuid.UUID, answers: dict, *, data_dir: Path | None = None
) -> VaultEntry:
    """Apply the uploader's answers to an entry that needs clarification and process it again."""
    entry = session.get(VaultEntry, entry_id)
    if entry is None:
        raise ValueError("no such vault entry")
    if entry.status != NEEDS_CLARIFICATION:
        raise ValueError(f"this upload does not need clarification (status {entry.status})")
    allowed = {"doc_type", "confirm_issuer", "source_reference"}
    entry.clarification = {
        **(entry.clarification or {}),
        **{k: v for k, v in answers.items() if k in allowed},
    }
    return process_entry(session, entry_id, data_dir=data_dir)


# ---------------------------------------------------------------------------- access and listing


def can_view(entry: VaultEntry, viewer: str, is_admin: bool = False) -> bool:
    """Shared, validated filings are visible to everyone with access; the rest only to the owner."""
    return is_admin or entry.uploaded_by == viewer or entry.visibility == "SHARED"


def entry_out(entry: VaultEntry, viewer: str, is_admin: bool = False) -> dict:
    mine = entry.uploaded_by == viewer or is_admin
    return {
        "id": str(entry.id),
        "company_id": str(entry.company_id),
        "session_id": str(entry.session_id) if entry.session_id else None,
        "filename": entry.original_filename if mine else None,
        "uploaded_by": entry.uploaded_by if mine else "another user",
        "uploaded_at": entry.uploaded_at.isoformat() if entry.uploaded_at else None,
        "status": entry.status,
        "reject_reason": entry.reject_reason if mine else None,
        "category": entry.category,
        "fiscal_year": entry.fiscal_year,
        "quarter": entry.quarter,
        "period_end": entry.period_end,
        "basis": entry.basis,
        "accounting_standard": entry.accounting_standard,
        "issuer_status": entry.issuer_status,
        "issuer_name": entry.issuer_name,
        "extraction_status": entry.extraction_status,
        "validation_status": entry.validation_status,
        "visibility": entry.visibility,
        "licence_class": entry.licence_class if mine else None,
        "version": entry.version,
        "sha256": entry.sha256,
        "size_bytes": entry.size_bytes,
        "ambiguities": (entry.ambiguities or []) if mine else [],
        "conflicts": (entry.conflicts or []) if mine else [],
        "evidence_refs": entry.evidence_refs,
        "resolved_requirements": entry.resolved_requirements or [],
        "steps": (entry.detected or {}).get("steps", []) if mine else [],
        "warnings": (entry.detected or {}).get("warnings", []) if mine else [],
    }


def list_vault(
    session: Session, company_id: uuid.UUID, viewer: str, is_admin: bool = False
) -> list[VaultEntry]:
    rows = session.scalars(
        select(VaultEntry)
        .where(VaultEntry.company_id == company_id)
        .order_by(VaultEntry.uploaded_at.desc())
    )
    return [e for e in rows if can_view(e, viewer, is_admin)]


__all__ = [
    "ACCEPTED",
    "DUPLICATE",
    "FAILED",
    "HELD_PRIVATE",
    "NEEDS_CLARIFICATION",
    "PUBLIC_FILING",
    "REJECTED",
    "RECEIVED",
    "clarify",
    "entry_out",
    "inspect_upload",
    "list_vault",
    "process_entry",
    "receive_upload",
]
