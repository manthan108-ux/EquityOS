"""Normalized fact tables filled by data providers.

Every row keeps the ORIGINAL value, unit and period label next to the normalized value, plus the
provider, URL and retrieval time, so any number can be traced to its source (spec section 7).
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


class Statement(enum.StrEnum):
    INCOME = "INCOME"
    BALANCE_SHEET = "BALANCE_SHEET"
    CASH_FLOW = "CASH_FLOW"


class PeriodType(enum.StrEnum):
    """What the figure covers. HALF_YEAR and NINE_MONTH are CUMULATIVE year-to-date figures as
    reported (Apr-Sep, Apr-Dec); QUARTERLY is a standalone three-month figure."""

    ANNUAL = "ANNUAL"
    QUARTERLY = "QUARTERLY"
    HALF_YEAR = "HALF_YEAR"
    NINE_MONTH = "NINE_MONTH"


class Basis(enum.StrEnum):
    """Never mix these in one calculation (spec 25.1)."""

    CONSOLIDATED = "CONSOLIDATED"
    STANDALONE = "STANDALONE"


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(cls, name=name, native_enum=False, length=20, validate_strings=True)


class FinancialFact(Base):
    """One line item of one statement for one period, from one provider."""

    __tablename__ = "financial_facts"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "provider_code",
            "statement",
            "line_item",
            "period_end",
            "period_type",
            "basis",
        ),
        Index("ix_financial_facts_lookup", "company_id", "statement", "period_end"),
        Index("ix_financial_facts_document", "document_id"),
        Index("ix_financial_facts_payload", "payload_id"),
        CheckConstraint("original_unit <> ''", name="unit_present"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    provider_code: Mapped[str] = mapped_column(String(40))
    statement: Mapped[Statement] = mapped_column(_enum(Statement, "statement"))
    line_item: Mapped[str] = mapped_column(String(120))  # provider wording, e.g. "revenue"
    period_end: Mapped[date] = mapped_column(Date)
    period_type: Mapped[PeriodType] = mapped_column(_enum(PeriodType, "period_type"))
    basis: Mapped[Basis] = mapped_column(_enum(Basis, "basis"))
    # As published by the provider.
    original_value: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    original_unit: Mapped[str] = mapped_column(String(30))  # crore | inr | inr_per_share | ...
    period_label: Mapped[str] = mapped_column(String(40))  # e.g. "Mar 2026"
    # Normalized: absolute rupees (or per-share rupees). NULL if the unit is not convertible.
    value_inr: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    source_url: Mapped[str] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_payloads.id", ondelete="SET NULL")
    )
    # Set when the value was read from a stored document (Phase 5): which document and page.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    source_page: Mapped[int | None]
    # Set only on figures CALCULATED from reported ones (a standalone quarter = cumulative minus
    # the previous cumulative): the ids and values of the reported facts it was computed from.
    derived_from: Mapped[dict | None] = mapped_column(JSONB)
    # Earlier values this fact held before a later filing restated it, newest last:
    # [{value, unit, document_id, page, replaced_at, reason}]. Nothing a document said is lost.
    history: Mapped[list | None] = mapped_column(JSONB)


class ShareholdingFact(Base):
    __tablename__ = "shareholding_facts"
    __table_args__ = (
        UniqueConstraint("company_id", "provider_code", "category", "period_end"),
        CheckConstraint("percent BETWEEN 0 AND 100", name="percent_range"),
        Index("ix_shareholding_company_period", "company_id", "period_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    provider_code: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(40))  # promoters, fii, other_dii, ...
    period_end: Mapped[date] = mapped_column(Date)
    period_label: Mapped[str] = mapped_column(String(40))
    percent: Mapped[Decimal] = mapped_column(Numeric(7, 3))
    source_url: Mapped[str] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_payloads.id", ondelete="SET NULL")
    )


class CorporateAction(Base):
    """Dividends, bonus, split and rights events, stored as published (raw ratio text kept)."""

    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "provider_code", "action_type", "effective_date", "ratio_text"
        ),
        Index("ix_corporate_actions_company_date", "company_id", "effective_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    provider_code: Mapped[str] = mapped_column(String(40))
    action_type: Mapped[str] = mapped_column(String(30))  # Dividend | Bonus | Split | Rights
    effective_date: Mapped[date | None] = mapped_column(Date)  # ex-date; NULL if not parseable
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    ratio_text: Mapped[str] = mapped_column(String(40), default="", server_default="")
    details: Mapped[dict | None] = mapped_column(JSONB)  # provider's event_details, untouched
    source_url: Mapped[str] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_payloads.id", ondelete="SET NULL")
    )


class RatioSnapshot(Base):
    """Provider-computed ratios at retrieval time. Their definitions are the provider's own."""

    __tablename__ = "ratio_snapshots"
    __table_args__ = (Index("ix_ratio_snapshots_company", "company_id", "retrieved_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    provider_code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(40))  # P/E, ROE, ...
    company_value_raw: Mapped[str] = mapped_column(String(40))
    sector_value_raw: Mapped[str | None] = mapped_column(String(40))
    company_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    sector_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    is_percent: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[str] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RawPayload(Base):
    """Every provider response is archived verbatim so results can be reproduced and audited."""

    __tablename__ = "raw_payloads"
    __table_args__ = (Index("ix_raw_payloads_company_dataset", "company_id", "dataset"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    provider_code: Mapped[str] = mapped_column(String(40))
    dataset: Mapped[str] = mapped_column(String(40))
    request_url: Mapped[str] = mapped_column(Text)  # never contains credentials
    payload: Mapped[dict | list] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DatasetVerification(Base):
    """A provider supports a dataset ONLY IF a live retrieval was validated and recorded here."""

    __tablename__ = "dataset_verifications"
    __table_args__ = (
        Index("ix_dataset_verifications_lookup", "provider_code", "dataset", "verified_at"),
        Index("ix_dataset_verifications_company", "company_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_code: Mapped[str] = mapped_column(String(40))
    dataset: Mapped[str] = mapped_column(String(40))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str | None] = mapped_column(Text)  # what was checked, or why it failed
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


__all__ = [
    "Basis",
    "CorporateAction",
    "DatasetVerification",
    "FinancialFact",
    "PeriodType",
    "RatioSnapshot",
    "RawPayload",
    "ShareholdingFact",
    "Statement",
]


class ProviderRefreshState(Base):
    """Where a provider's refresh of one company stands, so a universe-wide run can resume,
    skip what is fresh and back off from what keeps failing. It stores no financial data."""

    __tablename__ = "provider_refresh_state"
    __table_args__ = (Index("ix_provider_refresh_due", "provider_code", "next_due_at"),)

    provider_code: Mapped[str] = mapped_column(String(40), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    # REFRESHED | PARTIAL | UNAVAILABLE | NO_ISIN | MAPPING_FAILED | ERROR
    status: Mapped[str] = mapped_column(String(20))
    datasets: Mapped[dict | None] = mapped_column(JSONB)  # dataset -> outcome, message, periods
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


class CompanyClassification(Base):
    """The industry a PROVIDER assigns to a company, with where and when it was read.

    Upstox's profile endpoint carries a `sector` label (for example "Refineries") and the combined
    market capitalisation of that sector. The label is the provider's own, not an NSE/BSE or
    EquityOS classification, and is only ever described as such. Companies sharing a label form the
    industry group used for industry-level growth statistics.
    """

    __tablename__ = "company_classification"
    __table_args__ = (Index("ix_company_classification_label", "scheme", "label"),)

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    scheme: Mapped[str] = mapped_column(String(30), primary_key=True)  # UPSTOX_SECTOR
    label: Mapped[str] = mapped_column(String(200))
    group_market_cap_crore: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(200))
