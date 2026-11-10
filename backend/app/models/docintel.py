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
    # Set when a newer run of the same document replaces this one. Superseded runs stay for the
    # audit trail but readers use only the current (non-superseded) run.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ocr_engine: Mapped[str | None] = mapped_column(String(60))  # e.g. "tesseract 5.3.0"


class DocumentPage(Base):
    """Plain text of one page, kept for citations and later retrieval."""

    __tablename__ = "document_pages"
    # Migration 0011 adds a GIN index on to_tsvector('english', text) for full-text search (the
    # research agents' page-cited retrieval). It is created in SQL because Alembic cannot render the
    # regconfig literal; Alembic skips expression indexes when it compares models and database.
    __table_args__ = (UniqueConstraint("extraction_id", "page_number"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="CASCADE")
    )
    page_number: Mapped[int] = mapped_column(Integer)  # 1-based, as a reader counts pages
    char_count: Mapped[int] = mapped_column(Integer)
    needs_ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    text_method: Mapped[str] = mapped_column(String(20))  # text_layer | embedded_ocr | ocr | none
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))  # mean word conf. 0-100
    text: Mapped[str] = mapped_column(Text)


class ExtractedStatement(Base):
    """One financial statement or disclosure table recognised on one page."""

    __tablename__ = "extracted_statements"
    __table_args__ = (Index("ix_extracted_statements_extraction", "extraction_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="CASCADE")
    )
    page_number: Mapped[int] = mapped_column(Integer)
    # INCOME | BALANCE_SHEET | CASH_FLOW | SEGMENT | RATIOS | SHAREHOLDING | NOTE
    statement: Mapped[str] = mapped_column(String(20))
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
        Index("ix_extracted_facts_statement", "statement_id"),
        Index("ix_extracted_facts_document", "document_id"),
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
    # Segment name, note type or other grouping the value belongs to (NULL for the primary
    # statements). Part of the value's identity: the same line item exists once per segment.
    dimension: Mapped[str | None] = mapped_column(String(160))
    # True when a person should look at this value before it is relied on (OCR disagreement,
    # low OCR confidence, ambiguous characters). Never promoted while True.
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))  # lowest word conf.


class DocumentSection(Base):
    """A narrative section located by its heading: auditor's report, accounting policies,
    management commentary, notes. Stored as text with its page range, never summarised, so any
    later statement about it can quote the page."""

    __tablename__ = "document_sections"
    __table_args__ = (
        Index("ix_document_sections_extraction", "extraction_id"),
        Index("ix_document_sections_document_kind", "document_id", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="CASCADE")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))  # AUDITOR_REPORT | ACCOUNTING_POLICIES | ...
    heading: Mapped[str] = mapped_column(String(300))
    start_page: Mapped[int] = mapped_column(Integer)
    end_page: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # Explicit, quoted signals only, e.g. {"opinion": "unmodified", "quotes": ["..."]}.
    flags: Mapped[dict | None] = mapped_column(JSONB)


class GroupEntity(Base):
    """An entity a company's own filing names as related to it ("Subsidiary", "Associate", ...).

    Read from the related-party note of a document, with its page. The relationship is exactly
    the word the company printed; nothing is inferred from names or ownership data we do not have.
    `matched_company_id` links the entity to our company universe when its name is listed there.
    """

    __tablename__ = "group_entities"
    __table_args__ = (
        UniqueConstraint("extraction_id", "normalized_name", "relationship"),
        Index("ix_group_entities_company", "company_id", "normalized_name"),
        Index("ix_group_entities_document", "document_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="CASCADE")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    page_number: Mapped[int] = mapped_column(Integer)
    entity_name: Mapped[str] = mapped_column(String(300))  # as printed (footnote marks removed)
    normalized_name: Mapped[str] = mapped_column(String(300))
    relationship: Mapped[str] = mapped_column(String(80))  # as printed, e.g. "Subsidiary"
    matched_company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )


__all__ = [
    "GroupEntity",
    "DocumentSection",
    "DocumentExtraction",
    "DocumentPage",
    "ExtractedFact",
    "ExtractedStatement",
    "ExtractionStatus",
    "ValidationStatus",
]


class XbrlFiling(Base):
    """One XBRL instance read offline: all the file said, before EquityOS chose what to use.

    The instance bytes themselves are the stored document (`documents.sha256`); this row keeps the
    file's contexts, units, namespaces and taxonomy references as filed, plus what the reader
    decided (family, basis, unit) and why. `xbrl_facts` keeps every fact, mapped or not.
    """

    __tablename__ = "xbrl_filings"
    __table_args__ = (Index("ix_xbrl_filings_document", "document_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    extraction_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_extractions.id", ondelete="SET NULL")
    )
    reader_version: Mapped[str] = mapped_column(String(20))
    family: Mapped[str | None] = mapped_column(String(10))  # IND_AS | BANK | NULL (undecided)
    basis: Mapped[str | None] = mapped_column(String(20))  # CONSOLIDATED | STANDALONE | NULL
    basis_source: Mapped[str] = mapped_column(Text)
    amount_unit: Mapped[str | None] = mapped_column(String(20))
    unit_source: Mapped[str] = mapped_column(Text)
    entity_ids: Mapped[list | None] = mapped_column(JSONB)
    entity_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    schema_refs: Mapped[list | None] = mapped_column(JSONB)
    namespaces: Mapped[dict | None] = mapped_column(JSONB)
    contexts: Mapped[list | None] = mapped_column(JSONB)
    units: Mapped[list | None] = mapped_column(JSONB)
    facts_total: Mapped[int] = mapped_column(Integer, default=0)
    facts_mapped: Mapped[int] = mapped_column(Integer, default=0)
    facts_dimensional: Mapped[int] = mapped_column(Integer, default=0)
    unmapped_concepts: Mapped[list | None] = mapped_column(JSONB)
    problems: Mapped[list | None] = mapped_column(JSONB)  # defects in the instance itself
    issues: Mapped[list | None] = mapped_column(JSONB)  # what the mapping refused, and why
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class XbrlFact(Base):
    """One fact exactly as filed (concept, context, dimensions, unit, decimals, text)."""

    __tablename__ = "xbrl_facts"
    __table_args__ = (
        Index("ix_xbrl_facts_filing", "filing_id"),
        Index("ix_xbrl_facts_concept", "filing_id", "local_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("xbrl_filings.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)  # position among the instance's elements
    namespace: Mapped[str] = mapped_column(Text)
    local_name: Mapped[str] = mapped_column(String(300))
    prefix: Mapped[str | None] = mapped_column(String(60))
    context_ref: Mapped[str] = mapped_column(String(200))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)  # duration end or the instant
    is_instant: Mapped[bool] = mapped_column(Boolean, default=False)
    dimensions: Mapped[list | None] = mapped_column(JSONB)
    unit_ref: Mapped[str | None] = mapped_column(String(100))
    unit_label: Mapped[str | None] = mapped_column(String(120))
    decimals: Mapped[str | None] = mapped_column(String(20))
    precision_attr: Mapped[str | None] = mapped_column(String(20))
    value_text: Mapped[str] = mapped_column(Text)
    is_nil: Mapped[bool] = mapped_column(Boolean, default=False)
    mapped_statement: Mapped[str | None] = mapped_column(String(20))
    mapped_item: Mapped[str | None] = mapped_column(String(80))


class EvidenceFetchRun(Base):
    """One automatic attempt to collect a company's public documents from its own website.

    Records what happened at every step so the dashboard can say *why* evidence is missing:
    no website known, the site refused automated access, nothing published that we could find,
    documents downloaded, and so on. It never stores document content; documents live in
    `documents` with their own provenance.
    """

    __tablename__ = "evidence_fetch_runs"
    __table_args__ = (Index("ix_evidence_fetch_runs_company", "company_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    website: Mapped[str | None] = mapped_column(String(500))
    candidates_found: Mapped[int] = mapped_column(Integer, default=0)
    downloaded: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict | None] = mapped_column(JSONB)
