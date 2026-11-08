"""Phase 8 tables: sector weight sets, peer sets, peers and weight adjustments.

Peer comparisons and scores themselves are CALCULATION evidence (`evidence_records`, methods
`peer.v1` and `score.v1`). These tables hold what is not a number: the curated weight sets with
their rationale, which companies were chosen as peers and why, and how weights were adjusted.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
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


class SectorWeightSet(Base):
    """A versioned, curated set of pillar weights for one sector.

    The weights are the project's judgement about what matters most in a sector, each with a stated
    rationale. They are not derived from data, not predictions and not produced by an LLM. A change
    is a new version; old versions stay so past scores remain explainable.
    """

    __tablename__ = "sector_weight_sets"
    __table_args__ = (UniqueConstraint("sector_code", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sector_code: Mapped[str] = mapped_column(String(40))
    version: Mapped[str] = mapped_column(String(20))
    profile: Mapped[str] = mapped_column(String(10))  # BANK | NONFIN
    pillar_weights: Mapped[dict] = mapped_column(JSONB)  # pillar -> weight, sums to 100
    rationale: Mapped[dict] = mapped_column(JSONB)  # pillar -> why this weight
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PeerSet(Base):
    """One peer selection and scoring run for one company."""

    __tablename__ = "peer_sets"
    __table_args__ = (Index("ix_peer_sets_company", "company_id", "built_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    as_of: Mapped[date] = mapped_column(Date)
    sector_code: Mapped[str | None] = mapped_column(String(40))
    weight_set_id: Mapped[int | None] = mapped_column(ForeignKey("sector_weight_sets.id"))
    criteria: Mapped[dict] = mapped_column(JSONB)
    candidates: Mapped[int] = mapped_column(Integer)  # companies considered
    peer_count: Mapped[int] = mapped_column(Integer)
    thin: Mapped[bool] = mapped_column(Boolean)  # fewer peers than the minimum for a reliable view
    summary: Mapped[dict | None] = mapped_column(JSONB)


class Peer(Base):
    __tablename__ = "peers"
    __table_args__ = (
        UniqueConstraint("peer_set_id", "company_id"),
        Index("ix_peers_peer_set", "peer_set_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    peer_set_id: Mapped[int] = mapped_column(ForeignKey("peer_sets.id", ondelete="CASCADE"))
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    tier: Mapped[str] = mapped_column(String(20))  # SAME_INDUSTRY | SAME_SECTOR
    rank: Mapped[int] = mapped_column(Integer)
    size_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    size_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))  # peer size / company size
    reason: Mapped[str] = mapped_column(Text)


class WeightAdjustment(Base):
    """How a company's pillar weights differ from its sector's base weights, and why."""

    __tablename__ = "weight_adjustments"
    __table_args__ = (Index("ix_weight_adjustments_peer_set", "peer_set_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    peer_set_id: Mapped[int] = mapped_column(ForeignKey("peer_sets.id", ondelete="CASCADE"))
    pillar: Mapped[str] = mapped_column(String(30))
    base_weight: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    adjusted_weight: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    reason: Mapped[str] = mapped_column(Text)


__all__ = ["Peer", "PeerSet", "SectorWeightSet", "WeightAdjustment"]
