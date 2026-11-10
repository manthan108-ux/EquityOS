"""Phase 10 tables: benchmark intelligence and the three scoring methodologies.

Nothing here replaces the Phase 8 relative engine (its results stay in `peer_sets` and
`evidence_records`). These tables add the absolute-scoring side: versioned benchmark statistics and
their observations, versioned metric definitions and scoring configuration, and every score run with
its metric-level results, so a score can always be traced to the exact benchmark version, definition
version and evidence that produced it.
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


class BenchmarkVersion(Base):
    """One immutable build of the benchmark statistics. A rebuild is a new version."""

    __tablename__ = "benchmark_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)  # e.g. bench.2026-11-10.1
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    input_hash: Mapped[str] = mapped_column(String(64))  # over the included observations
    methodology: Mapped[dict] = mapped_column(JSONB)  # thresholds and rules used to build it
    notes: Mapped[str | None] = mapped_column(Text)


class BenchmarkObservation(Base):
    """One company-year value that fed (or was excluded from) a benchmark."""

    __tablename__ = "benchmark_observations"
    __table_args__ = (Index("ix_bench_obs_scope", "version_id", "scope_type", "scope_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("benchmark_versions.id", ondelete="CASCADE"))
    scope_type: Mapped[str] = mapped_column(String(10))  # INDUSTRY | SECTOR
    scope_code: Mapped[str] = mapped_column(String(60))
    metric_code: Mapped[str] = mapped_column(String(60))
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    period_end: Mapped[date] = mapped_column(Date)
    basis: Mapped[str] = mapped_column(String(14))
    value: Mapped[Decimal] = mapped_column(Numeric(30, 8))
    evidence_id: Mapped[int | None] = mapped_column(
        ForeignKey("evidence_records.id", ondelete="SET NULL")
    )
    included: Mapped[bool] = mapped_column(Boolean)
    exclusion_reason: Mapped[str | None] = mapped_column(String(200))


class BenchmarkStat(Base):
    """The distribution of one metric in one scope: the benchmark scores are read against."""

    __tablename__ = "benchmark_stats"
    __table_args__ = (UniqueConstraint("version_id", "scope_type", "scope_code", "metric_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("benchmark_versions.id", ondelete="CASCADE"))
    scope_type: Mapped[str] = mapped_column(String(10))
    scope_code: Mapped[str] = mapped_column(String(60))
    metric_code: Mapped[str] = mapped_column(String(60))
    n_observations: Mapped[int] = mapped_column(Integer)
    n_companies: Mapped[int] = mapped_column(Integer)
    n_excluded: Mapped[int] = mapped_column(Integer)
    period_from: Mapped[date | None] = mapped_column(Date)
    period_to: Mapped[date | None] = mapped_column(Date)
    stats: Mapped[dict | None] = mapped_column(JSONB)  # null when the sample is insufficient
    confidence: Mapped[str] = mapped_column(String(14))  # HIGH | MEDIUM | LOW | INSUFFICIENT
    method: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MetricDefinition(Base):
    """A scored metric in a profile: what it is, how it is scored and why. Versioned."""

    __tablename__ = "metric_definitions"
    __table_args__ = (UniqueConstraint("code", "profile", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60))  # the scoring identifier
    profile: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    pillar: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str] = mapped_column(String(120))
    formula: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(10))  # HIGHER | LOWER | RANGE
    kind: Mapped[str] = mapped_column(String(12))  # PERCENTILE | THRESHOLD | RANGE
    curve: Mapped[dict | None] = mapped_column(JSONB)  # fallback curve / range definition
    calc_metric: Mapped[str | None] = mapped_column(String(60))  # None: no source in the system
    applies_to: Mapped[dict] = mapped_column(JSONB)  # sectors / industries
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 3))  # within its pillar
    curve_source_class: Mapped[str | None] = mapped_column(String(14))  # CONVENTION | EMPIRICAL
    curve_source_note: Mapped[str | None] = mapped_column(Text)
    unavailable_reason: Mapped[str | None] = mapped_column(Text)
    min_quality: Mapped[str] = mapped_column(String(8))
    effective_date: Mapped[date] = mapped_column(Date)


class ScoringConfig(Base):
    """Versioned pillar weights and eligibility rules of one profile's absolute scoring."""

    __tablename__ = "scoring_configs"
    __table_args__ = (UniqueConstraint("profile", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    pillar_weights: Mapped[dict] = mapped_column(JSONB)
    rationale: Mapped[dict] = mapped_column(JSONB)
    min_weight_coverage: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    critical_pillars: Mapped[list] = mapped_column(JSONB)
    max_staleness_days: Mapped[int] = mapped_column(Integer)
    effective_date: Mapped[date] = mapped_column(Date)


class ScoreRun(Base):
    """One scoring of one company by one methodology. Immutable once written."""

    __tablename__ = "score_runs"
    __table_args__ = (Index("ix_score_runs_company", "company_id", "calculated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    methodology: Mapped[str] = mapped_column(String(12))  # RELATIVE|ABSOLUTE|HYBRID|UNAVAILABLE
    requested: Mapped[str] = mapped_column(String(12))  # AUTO or the one asked for
    profile: Mapped[str | None] = mapped_column(String(30))
    overall: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    unavailable_reason: Mapped[str | None] = mapped_column(Text)
    weight_coverage: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    confidence: Mapped[str | None] = mapped_column(String(10))
    peer_count: Mapped[int] = mapped_column(Integer)
    eligible_peer_count: Mapped[int] = mapped_column(Integer)
    benchmark_version_id: Mapped[int | None] = mapped_column(ForeignKey("benchmark_versions.id"))
    config_version: Mapped[str | None] = mapped_column(String(20))
    as_of: Mapped[date] = mapped_column(Date)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_hash: Mapped[str] = mapped_column(String(64))
    pillar_scores: Mapped[dict | None] = mapped_column(JSONB)
    explanation: Mapped[dict | None] = mapped_column(JSONB)


class ScoreMetricResult(Base):
    """The scoring of one metric in one run: original value, benchmark used, normalised score."""

    __tablename__ = "score_metric_results"
    __table_args__ = (Index("ix_score_metric_results_run", "run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("score_runs.id", ondelete="CASCADE"))
    metric_code: Mapped[str] = mapped_column(String(60))
    pillar: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(14))  # SCORED | GAP | EXCLUDED
    reason: Mapped[str | None] = mapped_column(Text)
    actual_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    unit: Mapped[str | None] = mapped_column(String(20))
    normalized_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    kind: Mapped[str | None] = mapped_column(String(12))
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    benchmark_stat_id: Mapped[int | None] = mapped_column(ForeignKey("benchmark_stats.id"))
    benchmark: Mapped[dict | None] = mapped_column(JSONB)  # what the value was read against
    period_end: Mapped[date | None] = mapped_column(Date)
    basis: Mapped[str | None] = mapped_column(String(14))
    evidence_id: Mapped[int | None] = mapped_column(
        ForeignKey("evidence_records.id", ondelete="SET NULL")
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )


__all__ = [
    "BenchmarkObservation",
    "BenchmarkStat",
    "BenchmarkVersion",
    "MetricDefinition",
    "ScoreMetricResult",
    "ScoreRun",
    "ScoringConfig",
]
