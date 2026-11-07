"""Download and store documents (annual reports, results, presentations) with provenance.

Safety: documents are untrusted input. Files are stored under a name derived from their content
hash (never from the URL or a user-supplied filename, so no path traversal), their real type is
checked against magic bytes, and nothing in a document is ever executed or treated as an
instruction.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.acquisition.http import FetchResult, PoliteClient
from app.core.config import get_settings, resolve_data_dir
from app.models import Document, FetchStatus, Source

# magic bytes -> (extension, canonical content type)
_SIGNATURES = [
    (b"%PDF-", ("pdf", "application/pdf")),
    (b"PK\x03\x04", ("zip", "application/zip")),  # also .xlsx / .docx containers (and XBRL zips)
    (b"<?xml", ("xml", "application/xml")),
    (b"<html", ("html", "text/html")),
    (b"<!doc", ("html", "text/html")),
]
ALLOWED_DOC_TYPES = {
    "annual_report",
    "quarterly_result",
    "investor_presentation",
    "earnings_transcript",
    "corporate_announcement",
    "xbrl",
    "regulatory_publication",
    "other",
}
MAX_MANUAL_BYTES = 200 * 1024 * 1024


def sniff(body: bytes) -> tuple[str, str] | None:
    head = body[:2048].lstrip().lower()
    raw = body[:8]
    for magic, kind in _SIGNATURES:
        if raw.startswith(magic) or head.startswith(magic.lower()):
            return kind
    return None


def validate_document(body: bytes, _ctype: str | None) -> str | None:
    return None if sniff(body) else "unrecognised or unsafe file type"


@dataclass
class StoredDocument:
    document: Document | None
    created: bool
    fetch: FetchResult | None
    error: str | None = None


def store_document_bytes(
    session: Session,
    root: Path,
    body: bytes,
    *,
    url: str,
    doc_type: str,
    source_code: str,
    company_id: uuid.UUID | None = None,
    title: str | None = None,
    published_at: date | None = None,
    financial_period: str | None = None,
    retrieved_at: datetime | None = None,
) -> tuple[Document, bool]:
    """Validate and store bytes. Identical content is stored once. Raises ValueError if unsafe."""
    if doc_type not in ALLOWED_DOC_TYPES:
        raise ValueError(f"Unknown doc_type '{doc_type}'")
    if not body:
        raise ValueError("Empty file")
    kind = sniff(body)
    if kind is None:
        raise ValueError("Unrecognised or unsafe file type")

    sha = hashlib.sha256(body).hexdigest()
    existing = session.scalar(select(Document).where(Document.sha256 == sha))
    if existing is not None:
        if existing.financial_period is None and financial_period:
            existing.financial_period = financial_period  # fills a gap; never overwrites
        return existing, False

    extension, content_type = kind
    relative = Path("documents") / sha[:2] / f"{sha}.{extension}"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)

    source = session.scalar(select(Source).where(Source.code == source_code))
    document = Document(
        company_id=company_id,
        source_id=source.id if source else None,
        url=url,
        title=title,
        doc_type=doc_type,
        content_type=content_type,
        sha256=sha,
        size_bytes=len(body),
        storage_path=relative.as_posix(),
        published_at=published_at,
        financial_period=financial_period,
        retrieved_at=retrieved_at or datetime.now(UTC),
    )
    session.add(document)
    session.flush()
    return document, True


class DocumentProvider:
    source_code = "COMPANY_IR"

    def __init__(self, client: PoliteClient, data_dir: Path | None = None) -> None:
        self._client = client
        self._root = data_dir or resolve_data_dir(get_settings())

    def download(
        self,
        session: Session,
        url: str,
        *,
        doc_type: str,
        company_id: uuid.UUID | None = None,
        source_code: str | None = None,
        title: str | None = None,
        published_at: date | None = None,
        financial_period: str | None = None,
    ) -> StoredDocument:
        if doc_type not in ALLOWED_DOC_TYPES:
            raise ValueError(f"Unknown doc_type '{doc_type}'")
        code = source_code or self.source_code
        fetch = self._client.fetch(url, source=code, validate=validate_document)
        if not fetch.ok or not fetch.content:
            return StoredDocument(None, False, fetch, fetch.error)
        document, created = store_document_bytes(
            session,
            self._root,
            fetch.content,
            url=url,
            doc_type=doc_type,
            source_code=code,
            company_id=company_id,
            title=title,
            published_at=published_at,
            financial_period=financial_period,
            retrieved_at=fetch.retrieved_at,
        )
        return StoredDocument(document, created, fetch)


__all__ = [
    "DocumentProvider",
    "FetchStatus",
    "StoredDocument",
    "sniff",
    "store_document_bytes",
    "validate_document",
]
