"""company website registry

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_website_registry",
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("investor_relations_url", sa.String(length=500), nullable=True),
        sa.Column("discovery_source", sa.String(length=60), nullable=True),
        sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("company_id"),
    )
    op.create_index(
        "ix_website_registry_status", "company_website_registry", ["status", "next_retry_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_website_registry_status", table_name="company_website_registry")
    op.drop_table("company_website_registry")
