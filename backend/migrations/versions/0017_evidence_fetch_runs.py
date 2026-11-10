"""evidence fetch runs

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-11 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_fetch_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("candidates_found", sa.Integer(), nullable=False),
        sa.Column("downloaded", sa.Integer(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evidence_fetch_runs_company", "evidence_fetch_runs", ["company_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_fetch_runs_company", table_name="evidence_fetch_runs")
    op.drop_table("evidence_fetch_runs")
