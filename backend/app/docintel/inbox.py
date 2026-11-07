"""Manual document ingestion from an inbox folder.

For companies whose websites refuse automated access (HTTP 403, as with tcs.com and infosys.com),
the reliable route is: download the document yourself in a browser, drop it in

    <DATA_DIR>/inbox/<SYMBOL>/            e.g. data/inbox/TCS/AR-2025-26.pdf

and run `python -m app.docintel.cli ingest-inbox`. Each file is:

* checked to be a real PDF (magic bytes) of reasonable size, never trusted by name;
* checked against its own first page for WHO issued it (a subsidiary's file is refused);
* typed (annual report / quarterly result / presentation / ...) from its content, unless the file
  name carries a clear hint - the guess is reported, and a file that cannot be typed is left alone;
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
from app.docintel.pdf import first_pages_text
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


def guess_doc_type(file_name: str, first_pages: list[str]) -> str | None:
    """Document type from the file name, else from the first pages; None if unsure."""
    for kind, pattern in _NAME_HINTS:
        if pattern.search(file_name):
            return kind
    text = "\n".join(first_pages[:3])
    for kind, pattern in _TEXT_HINTS:
        if pattern.search(text):
            return kind
    return None


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
    kind = guess_doc_type(path.name, pages)
    if kind is None:
        return InboxOutcome(
            path.name,
            "REFUSED",
            "could not tell what kind of document this is; use ingest-document with --type",
        )
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
