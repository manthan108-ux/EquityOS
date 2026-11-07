"""Document intelligence tables (Phase 5).

An *extraction run* processes one stored document. It keeps every page's text (for later
citation), every statement table it recognised, and every number it read, with the page, the
row label exactly as printed, the column header, the raw cell text and its position on the page.

Extracted numbers are staged here first. Only values that pass the deterministic checks (known
unit, explicit consolidated/standalone basis, a recognised reporting period and the arithmetic
cross-checks of their statement) are promoted into `financial_facts`. Everything else stays
visible here together with the reason it was not promoted.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExtractionStatus(enum.StrEnum):
    COMPLETED = "COMPLETED"  # text read and at least one statement table recognised
    NO_STATEMENTS = "NO_STATEMENTS"  # text read, but no financial statement table recognised
    NEEDS_OCR = "NEEDS_OCR"  # scanned pages without a text layer and no OCR engine available
    UNSUPPORTED = "UNSUPPORTED"  # not a PDF, encrypted, or otherwise unreadable
    FAILED = "FAILED"  # unexpected error (recorded, never hidden)


class ValidationStatus(enum.StrEnum):
    PASSED = "PASSED"  # every arithmetic check this value takes part in holds
    FAILED = "FAILED"  # at least one check failed: the value is not trusted
    UNCHECKED = "UNCHECKED"  # no check covers this value


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(cls, name=name, native_enum=False, length=20, validate_strings=True)


class DocumentExtraction(Base):
    __tablename__ = "document_extractions"
    __table_args__ = (Index("ix_document_extractions_document", "document_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    extractor_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[ExtractionStatus] = mapped_column(_enum(ExtractionStatus, "extraction_status"))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    pages_with_text: Mapped[int] = mapped_column(Integer, default=0)
    pages_needing_ocr: Mapped[int] = mapped_column(Integer, default=0)
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False)
    statements_found: Mapped[int] = mapped_column(Integer, default=0)
    facts_extracted: Mapped[int] = mapped_column(Integer, default=0)
    facts_promoted: Mapped[int] = mapped_column(Integer, default=0)
    # Structured problems: [{"page": 12, "code": "UNIT_UNKNOWN", "detail": "..."}]
    issues: Mapped[list | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentPage(Base):
    """Plain text of one page, kept for citations and later retrieval."""

    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("extraction_id", "page_number"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="CASCADE")
    )
    page_number: Mapped[int] = mapped_column(Integer)  # 1-based, as a reader counts pages
    char_count: Mapped[int] = mapped_column(Integer)
    needs_ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    text_method: Mapped[str] = mapped_column(String(20))  # "text_layer" | "ocr" | "none"
    text: Mapped[str] = mapped_column(Text)


class ExtractedStatement(Base):
    """One financial statement table recognised on one page."""

    __tablename__ = "extracted_statements"

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="CASCADE")
    )
    page_number: Mapped[int] = mapped_column(Integer)
    statement: Mapped[str] = mapped_column(String(20))  # INCOME | BALANCE_SHEET | CASH_FLOW
    basis: Mapped[str | None] = mapped_column(String(20))  # NULL = not stated on the page
    unit: Mapped[str | None] = mapped_column(String(30))  # NULL = not stated on the page
    title: Mapped[str | None] = mapped_column(Text)
    detection: Mapped[str] = mapped_column(String(200))  # how the type was recognised
    columns: Mapped[list] = mapped_column(JSONB)  # per column: header text, period, audited
    rows_matched: Mapped[int] = mapped_column(Integer)
    rows_unmatched: Mapped[int] = mapped_column(Integer)
    checks: Mapped[list | None] = mapped_column(JSONB)  # arithmetic checks and their results


class ExtractedFact(Base):
    """One number read from one cell of a statement table, with full provenance."""

    __tablename__ = "extracted_facts"
    __table_args__ = (
        Index("ix_extracted_facts_extraction", "extraction_id"),
        Index("ix_extracted_facts_company_item", "company_id", "canonical_item", "period_end"),
        CheckConstraint(
            "duration_months IN (3, 6, 9, 12) OR duration_months IS NULL", name="duration_known"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="CASCADE")
    )
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("extracted_statements.id", ondelete="CASCADE")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    statement: Mapped[str] = mapped_column(String(20))
    basis: Mapped[str | None] = mapped_column(String(20))
    canonical_item: Mapped[str] = mapped_column(String(80))
    raw_label: Mapped[str] = mapped_column(Text)  # exactly as printed (joined if it wrapped)
    page_number: Mapped[int] = mapped_column(Integer)
    row_index: Mapped[int] = mapped_column(Integer)
    column_index: Mapped[int] = mapped_column(Integer)
    column_header: Mapped[str] = mapped_column(Text)
    period_end: Mapped[date | None] = mapped_column(Date)
    duration_months: Mapped[int | None] = mapped_column(Integer)  # NULL for point-in-time / unknown
    audited: Mapped[bool | None] = mapped_column(Boolean)
    raw_value: Mapped[str] = mapped_column(String(60))
    original_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    original_unit: Mapped[str | None] = mapped_column(String(30))
    value_inr: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    bbox: Mapped[list | None] = mapped_column(JSONB)  # [x0, y0, x1, y1] in PDF points
    validation_status: Mapped[ValidationStatus] = mapped_column(
        _enum(ValidationStatus, "validation_status")
    )
    validation_notes: Mapped[list | None] = mapped_column(JSONB)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False)
    not_promoted_reason: Mapped[str | None] = mapped_column(String(200))


__all__ = [
    "DocumentExtraction",
    "DocumentPage",
    "ExtractedFact",
    "ExtractedStatement",
    "ExtractionStatus",
    "ValidationStatus",
]
