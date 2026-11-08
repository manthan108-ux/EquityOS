"""Phase 7 tables: price adjustment events and the record of what the calculation engine did.

The calculated numbers themselves are CALCULATION evidence (`evidence_records`); these tables hold
only what has no place there: the corporate-action events used to adjust prices, and, for each run,
which metrics could NOT be calculated and why.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdjustmentKind(enum.StrEnum):
    SPLIT = "SPLIT"  # face value sub-division
    BONUS = "BONUS"
    SPLIT_OR_BONUS = "SPLIT_OR_BONUS"  # implied from prices: same effect, cause unknown
    CONSOLIDATION = "CONSOLIDATION"  # reverse split
    UNEXPLAINED = "UNEXPLAINED"  # reference price differs from the prior close; NOT applied


class AdjustmentOrigin(enum.StrEnum):
    CORPORATE_ACTION = "CORPORATE_ACTION"  # a stored corporate-action row with a parseable ratio
    IMPLIED = "IMPLIED"  # inferred from the exchange's own reference (previous close) price


class AdjustmentStatus(enum.StrEnum):
    APPLIED = "APPLIED"
    REPORTED_ONLY = "REPORTED_ONLY"


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(cls, name=name, native_enum=False, length=20, validate_strings=True)


class PriceAdjustment(Base):
    """One event that changes the meaning of old prices. Derived data: rebuilt each run.

    `factor` divides every price BEFORE `ex_date` (a 2-for-1 split has factor 2) and multiplies
    every volume before it. Cash dividends and rights issues are not adjusted (see
    docs/CALCULATION_ENGINE.md).
    """

    __tablename__ = "price_adjustments"
    __table_args__ = (
        UniqueConstraint("security_id", "ex_date", "origin"),
        Index("ix_price_adjustments_security", "security_id", "ex_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id", ondelete="CASCADE"))
    ex_date: Mapped[date] = mapped_column(Date)
    factor: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    kind: Mapped[AdjustmentKind] = mapped_column(_enum(AdjustmentKind, "adjustment_kind"))
    origin: Mapped[AdjustmentOrigin] = mapped_column(_enum(AdjustmentOrigin, "adjustment_origin"))
    status: Mapped[AdjustmentStatus] = mapped_column(_enum(AdjustmentStatus, "adjustment_status"))
    # How sure we are: EXPLICIT (a corporate-action row), CONFIRMED (both exchanges' reference
    # prices show it), SINGLE_EXCHANGE, or NOT_CLASSIFIED.
    confidence: Mapped[str] = mapped_column(String(20))
    prior_close: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    reference_close: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    corporate_action_id: Mapped[int | None] = mapped_column(
        ForeignKey("corporate_actions.id", ondelete="SET NULL")
    )
    detail: Mapped[str] = mapped_column(Text)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CalculationRun(Base):
    __tablename__ = "calculation_runs"
    __table_args__ = (Index("ix_calculation_runs_company", "company_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    engine_version: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    as_of: Mapped[date] = mapped_column(Date)
    profile: Mapped[str] = mapped_column(String(20))  # BANK | NONFIN
    calculated: Mapped[int] = mapped_column(Integer, default=0)
    gaps: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict | None] = mapped_column(JSONB)


class CalculationGap(Base):
    """A metric that could not be calculated, and exactly why. Missing is never zero."""

    __tablename__ = "calculation_gaps"
    __table_args__ = (Index("ix_calculation_gaps_run", "run_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("calculation_runs.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(120))
    period_end: Mapped[date | None] = mapped_column(Date)
    period_type: Mapped[str | None] = mapped_column(String(20))
    basis: Mapped[str | None] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    missing: Mapped[list | None] = mapped_column(JSONB)  # input names/metrics that were absent


__all__ = [
    "AdjustmentKind",
    "AdjustmentOrigin",
    "AdjustmentStatus",
    "CalculationGap",
    "CalculationRun",
    "PriceAdjustment",
]
