"""provider refresh state

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-12 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_refresh_state",
        sa.Column("provider_code", sa.String(length=40), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("datasets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("provider_code", "company_id"),
    )
    op.create_index(
        "ix_provider_refresh_due", "provider_refresh_state", ["provider_code", "next_due_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_provider_refresh_due", table_name="provider_refresh_state")
    op.drop_table("provider_refresh_state")
