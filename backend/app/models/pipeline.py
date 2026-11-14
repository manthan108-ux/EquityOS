"""Persistent state of the research pipeline: runs, per-company stage checkpoints, events, schedule.

Nothing here holds research data. It records what was attempted, what finished, what failed and why,
so a run can be stopped and resumed at the stage it stopped on, and so the UI can show the real
state of every stage rather than a command count.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# stage statuses
PENDING, RUNNING, SUCCEEDED, FAILED, SKIPPED = (
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "SKIPPED",
)
# run statuses
QUEUED, PAUSED, CANCELLING, CANCELLED, COMPLETED = (
    "QUEUED",
    "PAUSED",
    "CANCELLING",
    "CANCELLED",
    "COMPLETED",
)


class PipelineRun(Base):
    """One execution of the pipeline over a set of companies and stages."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("ix_pipeline_runs_status", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # MANUAL | SCHEDULED | SINGLE | STAGE | RETRY
    trigger: Mapped[str] = mapped_column(String(20))
    # QUEUED | RUNNING | PAUSED | CANCELLING | CANCELLED | COMPLETED | FAILED
    status: Mapped[str] = mapped_column(String(20), default=QUEUED)
    # NONE | PAUSE | CANCEL: what an operator asked for; the worker acts on it between companies
    control: Mapped[str] = mapped_column(String(10), default="NONE")
    stages: Mapped[list] = mapped_column(JSONB)
    symbols: Mapped[list | None] = mapped_column(JSONB)  # explicit companies, else the next batch
    batch_size: Mapped[int] = mapped_column(Integer, default=10)
    options: Mapped[dict | None] = mapped_column(JSONB)  # force, concurrency, retry_failed ...
    companies: Mapped[list | None] = mapped_column(JSONB)  # company ids this run covers, fixed once
    summary: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(60), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PipelineStageState(Base):
    """The latest state of one stage for one company (the checkpoint a restart resumes from)."""

    __tablename__ = "pipeline_stage_states"
    __table_args__ = (Index("ix_pipeline_stage_status", "stage", "status"),)

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[str] = mapped_column(String(20), primary_key=True)
    # PENDING | RUNNING | SUCCEEDED | FAILED | SKIPPED
    status: Mapped[str] = mapped_column(String(12), default=PENDING)
    # what the stage's result was computed from; unchanged inputs mean the work is not repeated
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)  # the stage's one-line outcome
    detail: Mapped[dict | None] = mapped_column(JSONB)  # structured outcome (readiness, counts)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PipelineEvent(Base):
    """Append-only stage log: every stage attempt of every run."""

    __tablename__ = "pipeline_events"
    __table_args__ = (
        Index("ix_pipeline_events_run", "run_id", "id"),
        Index("ix_pipeline_events_company", "company_id", "id"),
        Index("ix_pipeline_events_stage_at", "stage", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="CASCADE"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
    )
    stage: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(12))
    message: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    duration_ms: Mapped[int | None] = mapped_column(Integer)  # how long the stage attempt took
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class PipelineSchedule(Base):
    """The single, editable schedule the worker follows (row id 1)."""

    __tablename__ = "pipeline_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=360)
    batch_size: Mapped[int] = mapped_column(Integer, default=10)
    concurrency: Mapped[int] = mapped_column(Integer, default=2)
    stages: Mapped[list | None] = mapped_column(JSONB)
    symbols: Mapped[list | None] = mapped_column(JSONB)  # the selected companies; None = universe
    worker_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_note: Mapped[str | None] = mapped_column(String(120))  # host / process of the worker
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
