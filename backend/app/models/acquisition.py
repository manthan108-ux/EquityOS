"""Tables for data acquisition: source registry, fetch log, stored documents, end-of-day prices."""

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

from app.models.base import Base, TimestampMixin


class FetchStatus(enum.StrEnum):
    """Outcome of one fetch. Failures are data: they are logged and reported, never hidden."""

    OK = "OK"
    CACHED = "CACHED"
    BLOCKED_BY_ROBOTS = "BLOCKED_BY_ROBOTS"  # the site's robots.txt disallows us
    ACCESS_DENIED = "ACCESS_DENIED"  # 401/403/418: we do not try to bypass it
    RATE_LIMITED = "RATE_LIMITED"  # 429 and retries exhausted
    NOT_FOUND = "NOT_FOUND"
    CLIENT_ERROR = "CLIENT_ERROR"  # other 4xx: the request was refused, retrying will not help
    TIMEOUT = "TIMEOUT"
    SERVER_ERROR = "SERVER_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_CONTENT = "INVALID_CONTENT"  # HTTP 200 but not what we expected (e.g. an error page)
    TOO_LARGE = "TOO_LARGE"


def _enum(enum_cls: type[enum.StrEnum], name: str, length: int = 24) -> Enum:
    return Enum(enum_cls, name=name, native_enum=False, length=length, validate_strings=True)


class Source(TimestampMixin, Base):
    """Registry of data sources with their authority tier (1 = most authoritative)."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("authority_tier BETWEEN 1 AND 6", name="authority_tier_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # 1 exchange filings, 2 company disclosures, 3 regulators, 4 government, 5 reputable
    # secondary, 6 other public sources.
    authority_tier: Mapped[int] = mapped_column(Integer)
    base_url: Mapped[str | None] = mapped_column(String(300))
    terms_note: Mapped[str | None] = mapped_column(Text)
    # Registry fields (spec section 1). Declared by the provider spec; whether a dataset is truly
    # supported is decided by `dataset_verifications`, not by this list.
    auth_requirement: Mapped[str | None] = mapped_column(String(200))
    access_method: Mapped[str | None] = mapped_column(String(200))
    rate_limits: Mapped[str | None] = mapped_column(String(300))
    declared_datasets: Mapped[list | None] = mapped_column(JSONB)
    coverage_note: Mapped[str | None] = mapped_column(Text)
    redistribution_policy: Mapped[str | None] = mapped_column(String(300))


class AcquisitionLog(Base):
    __tablename__ = "acquisition_log"
    __table_args__ = (Index("ix_acquisition_log_source_started", "source_code", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_code: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[FetchStatus] = mapped_column(_enum(FetchStatus, "fetch_status"))
    http_status: Mapped[int | None]
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(TimestampMixin, Base):
    """A downloaded document, stored on disk and identified by its content hash."""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_company_id", "company_id"),
        Index("ix_documents_source_id", "source_id"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(500))
    doc_type: Mapped[str] = mapped_column(String(40))  # annual_report, quarterly_result, ...
    content_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(String(500))  # relative to the data directory
    published_at: Mapped[date | None] = mapped_column(Date)
    financial_period: Mapped[str | None] = mapped_column(String(40))
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Who the document is really FROM, read from its own first page (Phase 5 stabilization).
    # association_status: UNVERIFIED (never checked / cannot tell), CONFIRMED (the issuer is this
    # company), MISMATCH (the issuer is another entity), FORCED (linked by the user despite a
    # mismatch). Only CONFIRMED / UNVERIFIED / FORCED documents count toward a company's coverage.
    issuer_name: Mapped[str | None] = mapped_column(String(300))
    association_status: Mapped[str] = mapped_column(
        String(20), default="UNVERIFIED", server_default="UNVERIFIED"
    )
    association_note: Mapped[str | None] = mapped_column(Text)
    association_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentLinkEvent(Base):
    """Audit trail of every change to which company a document belongs to. Never deleted."""

    __tablename__ = "document_link_events"
    __table_args__ = (Index("ix_document_link_events_document", "document_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    company_before: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    company_after: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(20))  # LINKED | UNLINKED | RELINKED | REFUSED
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(60))  # "system" | "user-approved" | "cli"
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EodPrice(Base):
    """Official end-of-day prices from the exchanges' published bhavcopy files.

    Prices are UNADJUSTED for splits, bonuses and other corporate actions. Anything that compares
    prices across such events must adjust them first (Phase 7).
    """

    __tablename__ = "eod_prices"
    __table_args__ = (
        UniqueConstraint("security_id", "trade_date", "series"),
        Index("ix_eod_prices_security_date", "security_id", "trade_date"),
        Index("ix_eod_prices_trade_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id", ondelete="CASCADE"))
    trade_date: Mapped[date] = mapped_column(Date)
    series: Mapped[str] = mapped_column(String(10))  # NSE series or BSE group
    open: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    high: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    low: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    close: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    last: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    prev_close: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    turnover_inr: Mapped[Decimal | None] = mapped_column(Numeric(22, 2))
    trades: Mapped[int | None] = mapped_column(BigInteger)
    delivery_qty: Mapped[int | None] = mapped_column(BigInteger)  # NSE only; NULL = not published
    delivery_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))  # NSE only
    source_url: Mapped[str] = mapped_column(Text)
    file_sha256: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
