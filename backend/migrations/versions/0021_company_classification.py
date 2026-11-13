"""company classification (provider industry label)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-13 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_classification",
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("scheme", sa.String(length=30), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("group_market_cap_crore", sa.Numeric(24, 2), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("company_id", "scheme"),
    )
    op.create_index(
        "ix_company_classification_label", "company_classification", ["scheme", "label"]
    )


def downgrade() -> None:
    op.drop_index("ix_company_classification_label", table_name="company_classification")
    op.drop_table("company_classification")
