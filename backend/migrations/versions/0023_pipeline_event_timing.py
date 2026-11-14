"""stage duration and timeout flag on pipeline events; schedule scope and worker heartbeat

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-14 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pipeline_events", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column(
        "pipeline_events",
        sa.Column("timed_out", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index("ix_pipeline_events_stage_at", "pipeline_events", ["stage", "at"])
    op.add_column(
        "pipeline_schedule", sa.Column("symbols", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "pipeline_schedule",
        sa.Column("worker_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "pipeline_schedule", sa.Column("worker_note", sa.String(length=120), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("pipeline_schedule", "worker_note")
    op.drop_column("pipeline_schedule", "worker_seen_at")
    op.drop_column("pipeline_schedule", "symbols")
    op.drop_index("ix_pipeline_events_stage_at", table_name="pipeline_events")
    op.drop_column("pipeline_events", "timed_out")
    op.drop_column("pipeline_events", "duration_ms")
