"""Manual document ingestion from an inbox folder.

For companies whose websites refuse automated access (HTTP 403, as with tcs.com and infosys.com),
the reliable route is: download the document yourself in a browser, drop it in

    <DATA_DIR>/inbox/<SYMBOL>/            e.g. data/inbox/TCS/AR-2025-26.pdf

and run `python -m app.docintel.cli ingest-inbox`. Each file is:

* checked to be a real PDF (magic bytes) of reasonable size, never trusted by name;
* checked against its own first page for WHO issued it (a subsidiary's file is refused);
* typed (annual report / quarterly result / press release / presentation / ...) from its CONTENT
  first and its file name only when the content is not conclusive. A file that cannot be typed is
  stored as 'other' company material, never refused for that reason;
* given its period from its own statements when extraction runs (never from the file name alone);
* stored content-addressed; the inbox file is MOVED to `inbox/<SYMBOL>/done/` on success and to
  `inbox/<SYMBOL>/rejected/` with a `.reason.txt` on refusal, so nothing is ingested twice.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.storage import INBOX_SUBDIR
from app.docintel.pdf import first_pages_text, page_count
from app.providers.documents import AssociationMismatch
from app.providers.manual import ingest_document_file

MAX_FILE_BYTES = 200 * 1024 * 1024


@dataclass
class InboxOutcome:
    file: str
    status: str  # STORED | ALREADY_STORED | REFUSED
    detail: str
    doc_type: str | None = None


_NAME_HINTS = [
    ("annual_report", re.compile(r"annual[-_ ]?report|integrated[-_ ]?report|\bAR[-_ ]?\d", re.I)),
    ("investor_presentation", re.compile(r"presentation|investor[-_ ]?deck|\bppt\b", re.I)),
    ("earnings_transcript", re.compile(r"transcript|earnings[-_ ]?call", re.I)),
    ("quarterly_result", re.compile(r"result|quarter|\bq[1-4]\b|half[-_ ]?year", re.I)),
]
_NAME_HINTS.insert(
    0, ("shareholding_pattern", re.compile(r"shareholding|\bshp[-_ ]?\d|\bshp\b", re.I))
)
_TEXT_HINTS = [
    ("annual_report", re.compile(r"annual report|board'?s report|directors'? report", re.I)),
    ("earnings_transcript", re.compile(r"conference call|earnings call|moderator", re.I)),
    (
        "investor_presentation",
        re.compile(r"safe harbou?r|investor presentation|performance review", re.I),
    ),
    (
        "quarterly_result",
        re.compile(
            r"(un)?audited (consolidated |standalone )?financial results|quarter ended", re.I
        ),
    ),
]
_SHAREHOLDING = re.compile(
    r"shareholding pattern|statement showing shareholding|regulation 31\(1\)\(b\)", re.I
)
_PRESS_RELEASE = re.compile(r"press release|media release|media statement", re.I)
# Words that appear in the primary statements of Indian companies and banks. A document whose first
# pages are mostly such tables is a financial statement, whatever its file is called.
_STATEMENT_MARKERS = [
    re.compile(p, re.I)
    for p in (
        r"revenue from operations",
        r"total income",
        r"profit before tax",
        r"statement of (consolidated |standalone )?(profit|assets|cash)",
        r"balance sheet",
        r"total assets",
        r"equity share capital",
        r"interest earned",
        r"(₹|`|rs\.?|inr)?\s*(in )?(crore|million|lakh)",
        r"earnings per (equity )?share",
    )
]
LONG_REPORT_PAGES = 80
SHORT_DOCUMENT_PAGES = 12


def classify_content(pages: list[str], page_count: int | None) -> str | None:
    """Document type from what the first pages say, or None if the content is not conclusive.

    Content outranks the file name: a two-page "Annual Integrated Report" press release is a press
    release, and statements with no title page are results, whatever the file is called.
    """
    text = "\n".join(pages[:3])
    if not text.strip():
        return None
    head = (pages[0] if pages else "")[:700]
    short = page_count is not None and page_count <= SHORT_DOCUMENT_PAGES
    if _SHAREHOLDING.search(head) or _SHAREHOLDING.search(text[:1500]):
        return "shareholding_pattern"
    if short and _PRESS_RELEASE.search(head):
        return "press_release"
    long_report = page_count is not None and page_count >= LONG_REPORT_PAGES
    if long_report and _TEXT_HINTS[0][1].search(text):
        return "annual_report"
    for kind in ("earnings_transcript", "investor_presentation"):
        pattern = dict(_TEXT_HINTS)[kind]
        if pattern.search(text):
            return kind
    markers = sum(1 for m in _STATEMENT_MARKERS if m.search(text))
    if markers >= 3:
        return "annual_report" if long_report else "quarterly_result"
    if page_count is None or page_count < LONG_REPORT_PAGES:
        if dict(_TEXT_HINTS)["quarterly_result"].search(text):
            return "quarterly_result"
    return None


def guess_doc_type(file_name: str, first_pages: list[str], page_count: int | None = None) -> str:
    """Document type: from the content if it is conclusive, else from the file name, else 'other'.

    A document is never refused for being hard to type: 'other' documents are stored (and issuer
    checked) as company material; only the financial types are sent through extraction.
    """
    by_content = classify_content(first_pages, page_count)
    if by_content:
        return by_content
    for kind, pattern in _NAME_HINTS:
        if pattern.search(file_name):
            return kind
    text = "\n".join(first_pages[:3])
    for kind, pattern in _TEXT_HINTS:
        if pattern.search(text):
            return kind
    return "other"


def inbox_dir(root: Path, symbol: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", symbol).upper()
    if not safe:
        raise ValueError("a symbol is needed to find the inbox folder")
    return root / INBOX_SUBDIR / safe


def _move(path: Path, folder: str, reason: str | None = None) -> None:
    target = path.parent / folder
    target.mkdir(exist_ok=True)
    destination = target / path.name
    n = 1
    while destination.exists():
        destination = target / f"{path.stem}-{n}{path.suffix}"
        n += 1
    shutil.move(str(path), destination)
    if reason:
        destination.with_name(destination.name + ".reason.txt").write_text(reason, encoding="utf-8")


def ingest_inbox(
    session: Session, company_id, root: Path, symbol: str, *, move: bool = True
) -> list[InboxOutcome]:
    folder = inbox_dir(root, symbol)
    outcomes: list[InboxOutcome] = []
    if not folder.is_dir():
        return outcomes
    for path in sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")):
        if path.suffix.lower() == ".txt" and path.name.endswith(".reason.txt"):
            continue
        outcome = _one(session, company_id, root, path)
        outcomes.append(outcome)
        if move:
            if outcome.status == "REFUSED":
                _move(path, "rejected", outcome.detail)
            else:
                _move(path, "done")
    return outcomes


def _one(session: Session, company_id, root: Path, path: Path) -> InboxOutcome:
    if path.suffix.lower() != ".pdf":
        return InboxOutcome(path.name, "REFUSED", "only PDF files are accepted from the inbox")
    if path.stat().st_size > MAX_FILE_BYTES:
        return InboxOutcome(path.name, "REFUSED", "file is larger than 200 MB")
    pages = first_pages_text(path, 3)
    kind = guess_doc_type(path.name, pages, page_count(path))
    try:
        document, created = ingest_document_file(
            session, path, company_id=company_id, doc_type=kind, data_dir=root
        )
    except AssociationMismatch as exc:
        return InboxOutcome(path.name, "REFUSED", str(exc), kind)
    except (ValueError, OSError) as exc:
        return InboxOutcome(path.name, "REFUSED", f"not stored: {exc}", kind)
    return InboxOutcome(
        path.name,
        "STORED" if created else "ALREADY_STORED",
        f"{document.storage_path} ({document.association_status})",
        kind,
    )
