"""Phase 11 tables: research sessions, the company evidence vault and generated reports.

* `research_sessions` / `research_steps` hold the state of one research run, so interrupted work
  resumes without repeating a step whose inputs have not changed.
* `vault_entries` organise every user-supplied document by company, fiscal year, quarter,
  category, basis and accounting standard. The file itself is a `documents` row (content-addressed,
  de-duplicated by SHA-256); the vault entry adds who supplied it, what was found in it, whether it
  passed validation and who may use it.
* `research_reports` keep every generated report as an immutable version.

Nothing here changes an existing table's meaning; the only link to existing data is by foreign key.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
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

SESSION_STATUSES = (
    "CREATED",
    "RUNNING",
    "AWAITING_DOCUMENTS",  # critical evidence is missing: the user decides
    "READY",  # evidence is sufficient; a complete report may be generated
    "COMPLETED",  # a report exists
    "FAILED",
)


class ResearchSession(Base):
    __tablename__ = "research_sessions"
    __table_args__ = (
        Index("ix_research_sessions_company", "company_id", "created_at"),
        Index("ix_research_sessions_owner", "owner", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    owner: Mapped[str] = mapped_column(String(80))  # who started it (API client name)
    query: Mapped[str] = mapped_column(String(200))  # what the user typed
    status: Mapped[str] = mapped_column(String(24), default="CREATED")
    workflow: Mapped[str | None] = mapped_column(String(12))  # AUTOMATIC | ASSISTED | BLOCKED
    eligibility: Mapped[str | None] = mapped_column(String(24))
    readiness_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    readiness: Mapped[dict | None] = mapped_column(JSONB)  # the latest full readiness assessment
    # Hash of the evidence the steps ran on; a step is repeated only if this changed.
    evidence_fingerprint: Mapped[str | None] = mapped_column(String(64))
    choice: Mapped[str | None] = mapped_column(String(30))  # CONTINUE_WITH_AVAILABLE
    # What the user asked for: accounting basis, reporting period, optional items. The current
    # choice, and an append-only log of every earlier one, so a saved session keeps its history.
    preferences: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    preference_log: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ResearchStep(Base):
    __tablename__ = "research_steps"
    __table_args__ = (UniqueConstraint("session_id", "step"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_sessions.id", ondelete="CASCADE")
    )
    step: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(12))  # PENDING | RUNNING | DONE | SKIPPED | FAILED
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VaultEntry(Base):
    """One document in a company's evidence vault, with everything known about it."""

    __tablename__ = "vault_entries"
    __table_args__ = (
        Index("ix_vault_entries_company", "company_id", "fiscal_year", "quarter"),
        Index("ix_vault_entries_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_sessions.id", ondelete="SET NULL")
    )
    original_filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    detected_mime: Mapped[str | None] = mapped_column(String(100))
    # RECEIVED | PROCESSING | ACCEPTED | NEEDS_CLARIFICATION | REJECTED | DUPLICATE | FAILED
    status: Mapped[str] = mapped_column(String(22), default="RECEIVED")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    # Organisation
    category: Mapped[str | None] = mapped_column(String(40))  # annual_report, quarterly_result...
    fiscal_year: Mapped[str | None] = mapped_column(String(10))  # FY2026
    quarter: Mapped[str | None] = mapped_column(String(4))  # Q1..Q4 / H1 / 9M / NULL
    period_end: Mapped[str | None] = mapped_column(String(10))
    basis: Mapped[str | None] = mapped_column(String(14))  # CONSOLIDATED | STANDALONE
    accounting_standard: Mapped[str | None] = mapped_column(String(20))  # IND_AS | IGAAP | UNKNOWN
    # Provenance
    upload_source: Mapped[str] = mapped_column(String(40), default="USER_UPLOAD")
    uploaded_by: Mapped[str] = mapped_column(String(80))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    issuer_status: Mapped[str | None] = mapped_column(
        String(20)
    )  # CONFIRMED | MISMATCH | UNVERIFIED
    issuer_name: Mapped[str | None] = mapped_column(String(300))
    # Processing and validation
    extraction_status: Mapped[str | None] = mapped_column(String(20))
    validation_status: Mapped[str | None] = mapped_column(String(20))  # PASSED | FAILED | PARTIAL
    ambiguities: Mapped[list | None] = mapped_column(JSONB)  # what the user must clarify
    conflicts: Mapped[list | None] = mapped_column(JSONB)  # disagreements with stored values
    detected: Mapped[dict | None] = mapped_column(JSONB)  # the raw findings of the checks
    evidence_refs: Mapped[list | None] = mapped_column(JSONB)  # evidence ids/metrics it produced
    resolved_requirements: Mapped[list | None] = mapped_column(JSONB)
    # Access and licensing
    visibility: Mapped[str] = mapped_column(String(10), default="PRIVATE")  # PRIVATE | SHARED
    licence_class: Mapped[str] = mapped_column(String(24), default="UNDECLARED")
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vault_entries.id", ondelete="SET NULL")
    )
    clarification: Mapped[dict | None] = mapped_column(JSONB)  # what the user answered
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ResearchReport(Base):
    """One generated report. Immutable: a newer report is a new version, never an edit."""

    __tablename__ = "research_reports"
    __table_args__ = (
        UniqueConstraint("session_id", "version"),
        Index("ix_research_reports_company", "company_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_sessions.id", ondelete="CASCADE")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(20))  # AVAILABLE_EVIDENCE | COMPLETE
    readiness_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    missing_critical: Mapped[int] = mapped_column(Integer, default=0)
    missing_important: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = [
    "SESSION_STATUSES",
    "ResearchReport",
    "ResearchSession",
    "ResearchStep",
    "VaultEntry",
]
