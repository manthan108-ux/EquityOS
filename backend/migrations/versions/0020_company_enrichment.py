"""company enrichment

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-12 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_enrichment",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("role", sa.String(length=200), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("url", sa.String(length=600), nullable=False),
        sa.Column("publisher", sa.String(length=200), nullable=False),
        sa.Column("page_title", sa.String(length=300), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extractor", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_company_enrichment_company", "company_enrichment", ["company_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_company_enrichment_company", table_name="company_enrichment")
    op.drop_table("company_enrichment")
