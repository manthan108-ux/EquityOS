"""Open ZIP result kits (Kotak, M&M and others publish "results kits") and store the PDFs inside.

A ZIP is untrusted input. Safeguards: only members that are PDFs by their first bytes are used; the
member's own name is never used as a path (only its base name, sanitised, ends up in a temporary
file and in the document's title); at most MAX_MEMBERS members, each at most MAX_MEMBER_BYTES and
MAX_TOTAL_BYTES in all, measured while reading rather than trusted from the archive's header (so a
ZIP bomb cannot expand past the limits); nested archives are ignored. Each PDF then goes through the
normal ingestion path, so the issuer check, content typing and de-duplication all apply.
"""

import re
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.docintel.inbox import InboxOutcome, guess_doc_type
from app.docintel.pdf import first_pages_text, page_count
from app.models import Document
from app.providers.documents import AssociationMismatch
from app.providers.manual import ingest_document_file

MAX_MEMBERS = 40
MAX_MEMBER_BYTES = 120 * 1024 * 1024
MAX_TOTAL_BYTES = 300 * 1024 * 1024
_CHUNK = 1024 * 1024


class ArchiveError(ValueError):
    pass


def _safe_name(name: str) -> str:
    base = re.split(r"[\\/]", name)[-1]
    return re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:120] or "member.pdf"


def read_pdf_members(zip_path: Path) -> list[tuple[str, bytes]]:
    """(base name, bytes) of each PDF member, within the size limits."""
    out: list[tuple[str, bytes]] = []
    total = 0
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError("not a valid ZIP file") from exc
    with archive:
        members = [i for i in archive.infolist() if not i.is_dir()]
        if len(members) > MAX_MEMBERS:
            raise ArchiveError(f"more than {MAX_MEMBERS} files in the archive")
        for info in members:
            name = _safe_name(info.filename)
            if info.filename.startswith("__MACOSX") or name.startswith("."):
                continue
            if not name.lower().endswith(".pdf") or info.flag_bits & 0x1:  # encrypted
                continue
            data = bytearray()
            with archive.open(info) as handle:
                while chunk := handle.read(_CHUNK):
                    data.extend(chunk)
                    if len(data) > MAX_MEMBER_BYTES or total + len(data) > MAX_TOTAL_BYTES:
                        raise ArchiveError(f"{name} expands beyond the size limit")
            if not bytes(data[:5]) == b"%PDF-":
                continue  # a file called .pdf that is not one
            total += len(data)
            out.append((name, bytes(data)))
    return out


def unpack_document(
    session: Session, document: Document, root: Path, data_root_path: Path
) -> list[InboxOutcome]:
    """Store every PDF in a ZIP document as its own document of the same company."""
    outcomes: list[InboxOutcome] = []
    if document.company_id is None:
        return [InboxOutcome(document.title or "archive", "REFUSED", "the archive has no company")]
    zip_path = data_root_path / document.storage_path
    try:
        members = read_pdf_members(zip_path)
    except ArchiveError as exc:
        return [InboxOutcome(document.title or "archive", "REFUSED", str(exc))]
    for name, body in members:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_bytes(body)
            kind = guess_doc_type(name, first_pages_text(path, 3), page_count(path))
            title = f"{document.title or 'results kit'} / {name}"[:500]
            try:
                stored, created = ingest_document_file(
                    session,
                    path,
                    company_id=document.company_id,
                    doc_type=kind,
                    title=title,
                    financial_period=document.financial_period,
                    data_dir=root,
                )
            except AssociationMismatch as exc:
                outcomes.append(InboxOutcome(name, "REFUSED", str(exc), kind))
                continue
            except (ValueError, OSError) as exc:
                outcomes.append(InboxOutcome(name, "REFUSED", f"not stored: {exc}", kind))
                continue
        outcomes.append(
            InboxOutcome(
                name,
                "STORED" if created else "ALREADY_STORED",
                f"{stored.storage_path} ({stored.association_status})",
                kind,
            )
        )
    return outcomes


def unpack_company_archives(
    session: Session, company_id, root: Path
) -> list[tuple[Document, list[InboxOutcome]]]:
    """Unpack every ZIP document of a company (safe to repeat: stored files are de-duplicated)."""
    result = []
    for document in session.scalars(
        select(Document).where(
            Document.company_id == company_id, Document.content_type.like("%zip%")
        )
    ):
        result.append((document, unpack_document(session, document, root, root)))
    return result


__all__ = [
    "ArchiveError",
    "MAX_MEMBERS",
    "read_pdf_members",
    "unpack_company_archives",
    "unpack_document",
]
