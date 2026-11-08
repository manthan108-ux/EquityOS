"""Evidence store (Phase 6): one citable record per claim, links between them, resolved values and
validation results.

Four classes of evidence exist (docs/RESEARCH_METHODOLOGY.md section 1):

* FACT           a value read from a source, with provenance (mirrors the Phase 4/5 fact tables)
* CALCULATION    deterministic arithmetic on other evidence; the engine computes the value
* ASSUMPTION     an explicit, stated input (with its rationale)
* INTERPRETATION a reasoned statement that may only use numbers found in the evidence it cites

Records are append-only in spirit: a changed source value creates a NEW record and marks the old one
SUPERSEDED; nothing a report once cited is silently rewritten.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EvidenceClass(enum.StrEnum):
    FACT = "FACT"
    CALCULATION = "CALCULATION"
    ASSUMPTION = "ASSUMPTION"
    INTERPRETATION = "INTERPRETATION"


class EvidenceStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"  # the source now holds a different value; this one is kept
    WITHDRAWN = "WITHDRAWN"  # the source no longer holds it, or a person withdrew it


class LinkRole(enum.StrEnum):
    INPUT = "INPUT"  # a calculation's operand
    CITES = "CITES"  # an interpretation's supporting evidence
    SUPPORTS = "SUPPORTS"  # evidence behind an assumption


class Severity(enum.StrEnum):
    ERROR = "ERROR"  # the affected values must not be used in a report
    WARNING = "WARNING"  # usable, but a reader should know
    INFO = "INFO"


class ResolutionStatus(enum.StrEnum):
    SINGLE_SOURCE = "SINGLE_SOURCE"
    AGREED = "AGREED"  # several independent sources within tolerance
    RESOLVED_BY_AUTHORITY = "RESOLVED_BY_AUTHORITY"  # sources differ; a stronger source was used
    CONFLICT = "CONFLICT"  # sources of equal authority differ: no value is chosen
    CALCULATED = "CALCULATED"  # only a calculation supplies this value
    UNUSABLE = "UNUSABLE"  # every candidate failed a provenance check


class Quality(enum.StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


def _enum(cls: type[enum.StrEnum], name: str, length: int = 24) -> Enum:
    return Enum(cls, name=name, native_enum=False, length=length, validate_strings=True)


class EvidenceRecord(Base):
    __tablename__ = "evidence_records"
    __table_args__ = (
        Index(
            "uq_evidence_source_version",
            "source_table",
            "source_id",
            "content_hash",
            unique=True,
            postgresql_where=text("source_table IS NOT NULL"),
        ),
        Index("ix_evidence_company_class", "company_id", "evidence_class", "status"),
        Index("ix_evidence_company_metric", "company_id", "metric", "period_end"),
        Index("ix_evidence_document", "document_id"),
        Index("ix_evidence_source", "source_table", "source_id"),
        # A calculation always has a number; a FACT has a number or, for narrative evidence
        # (an auditor's report, a management statement, a news item), the statement itself.
        CheckConstraint(
            "evidence_class NOT IN ('FACT', 'CALCULATION') OR value IS NOT NULL "
            "OR (evidence_class = 'FACT' AND statement_text IS NOT NULL AND statement_text <> '')",
            name="numeric_classes_have_value",
        ),
        CheckConstraint(
            "evidence_class NOT IN ('ASSUMPTION', 'INTERPRETATION') "
            "OR (statement_text IS NOT NULL AND statement_text <> '')",
            name="stated_classes_have_text",
        ),
        CheckConstraint(
            "evidence_class <> 'FACT' OR (provider_code IS NOT NULL AND source_url IS NOT NULL)",
            name="fact_has_provenance",
        ),
        CheckConstraint(
            "evidence_class <> 'CALCULATION' OR expression IS NOT NULL",
            name="calculation_has_expression",
        ),
        CheckConstraint("value IS NULL OR unit IS NOT NULL", name="value_has_unit"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    evidence_class: Mapped[EvidenceClass] = mapped_column(_enum(EvidenceClass, "evidence_class"))
    # FINANCIAL_STATEMENT | DISCLOSURE | SHAREHOLDING | CALCULATION | ASSUMPTION | CLAIM
    kind: Mapped[str] = mapped_column(String(30))
    # What is measured: "INCOME.net_profit", "BALANCE_SHEET.total_assets", "SEGMENT.revenue"...
    metric: Mapped[str] = mapped_column(String(120))
    dimension: Mapped[str] = mapped_column(String(160), default="", server_default="")
    period_end: Mapped[date | None] = mapped_column(Date)
    period_type: Mapped[str | None] = mapped_column(String(20))  # NULL = point in time / n.a.
    basis: Mapped[str | None] = mapped_column(String(20))  # NULL = not applicable
    # Normalized: rupees (inr), inr_per_share, percent, ratio, shares, days, x ...
    value: Mapped[Decimal | None] = mapped_column(Numeric(30, 6))
    unit: Mapped[str | None] = mapped_column(String(30))
    # As published (FACT only).
    original_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    original_unit: Mapped[str | None] = mapped_column(String(30))
    expression: Mapped[str | None] = mapped_column(Text)  # CALCULATION: formula over aliases
    statement_text: Mapped[str | None] = mapped_column(Text)  # ASSUMPTION / INTERPRETATION
    provider_code: Mapped[str | None] = mapped_column(String(40))
    authority_tier: Mapped[int | None] = mapped_column(Integer)
    source_table: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(300))  # natural key in the source table
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    source_page: Mapped[int | None]
    source_url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Quality inputs and notes kept with the record: OCR confidence, printed decimals, other
    # documents' values for the same cell, restatement history, calculation inputs.
    details: Mapped[dict | None] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[EvidenceStatus] = mapped_column(
        _enum(EvidenceStatus, "evidence_status"),
        default=EvidenceStatus.ACTIVE,
        server_default=EvidenceStatus.ACTIVE.value,
    )
    status_reason: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(60), default="evidence-sync")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvidenceLink(Base):
    """`evidence_id` depends on `input_id`: a calculation operand, cited evidence or an
    assumption's supporting evidence."""

    __tablename__ = "evidence_links"
    __table_args__ = (
        UniqueConstraint("evidence_id", "input_id", "role", "alias"),
        Index("ix_evidence_links_input", "input_id"),
        CheckConstraint("evidence_id <> input_id", name="no_self_link"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence_records.id", ondelete="CASCADE"))
    input_id: Mapped[int] = mapped_column(ForeignKey("evidence_records.id", ondelete="CASCADE"))
    role: Mapped[LinkRole] = mapped_column(_enum(LinkRole, "link_role"))
    alias: Mapped[str] = mapped_column(String(40), default="", server_default="")


class ValidationRun(Base):
    """One execution of the validation engine for one company. Runs are kept (history)."""

    __tablename__ = "validation_runs"
    __table_args__ = (Index("ix_validation_runs_company", "company_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    rules_version: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    as_of: Mapped[date] = mapped_column(Date)  # the "today" used for freshness rules
    evidence_checked: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[int] = mapped_column(Integer, default=0)
    infos: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict | None] = mapped_column(JSONB)


class ValidationFinding(Base):
    __tablename__ = "validation_findings"
    __table_args__ = (
        Index("ix_validation_findings_run", "run_id"),
        Index("ix_validation_findings_company_rule", "company_id", "rule_code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("validation_runs.id", ondelete="CASCADE"))
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    rule_code: Mapped[str] = mapped_column(String(60))
    severity: Mapped[Severity] = mapped_column(_enum(Severity, "severity"))
    message: Mapped[str] = mapped_column(Text)
    metric: Mapped[str | None] = mapped_column(String(120))
    period_end: Mapped[date | None] = mapped_column(Date)
    evidence_ids: Mapped[list | None] = mapped_column(JSONB)
    details: Mapped[dict | None] = mapped_column(JSONB)


class EvidenceResolution(Base):
    """The ONE value a report may use for a (metric, dimension, period, basis), and why.

    Later phases ask this table, never the raw fact tables, so source authority, conflicts and
    blocking validation errors are applied in a single place.
    """

    __tablename__ = "evidence_resolutions"
    __table_args__ = (
        UniqueConstraint("company_id", "metric", "dimension", "period_end", "period_type", "basis"),
        Index("ix_evidence_resolutions_company", "company_id", "period_end"),
        Index("ix_evidence_resolutions_selected", "selected_evidence_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(120))
    dimension: Mapped[str] = mapped_column(String(160), default="", server_default="")
    period_end: Mapped[date] = mapped_column(Date)
    period_type: Mapped[str] = mapped_column(String(20), default="", server_default="")
    basis: Mapped[str] = mapped_column(String(20), default="", server_default="")
    selected_evidence_id: Mapped[int | None] = mapped_column(
        ForeignKey("evidence_records.id", ondelete="SET NULL")
    )
    status: Mapped[ResolutionStatus] = mapped_column(_enum(ResolutionStatus, "resolution_status"))
    quality: Mapped[Quality] = mapped_column(_enum(Quality, "quality", 10))
    quality_reasons: Mapped[list] = mapped_column(JSONB)
    # [{evidence_id, provider, tier, value, difference_pct, excluded_reason}]
    alternatives: Mapped[list] = mapped_column(JSONB)
    max_difference_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    # True when an ERROR finding involves the selected evidence: later phases must not use it.
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    blocked_by: Mapped[list | None] = mapped_column(JSONB)  # rule codes
    note: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("validation_runs.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = [
    "EvidenceClass",
    "EvidenceLink",
    "EvidenceRecord",
    "EvidenceResolution",
    "EvidenceStatus",
    "LinkRole",
    "Quality",
    "ResolutionStatus",
    "Severity",
    "ValidationFinding",
    "ValidationRun",
]
