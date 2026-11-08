"""phase 6: narrative (text) facts

A FACT may now carry a statement instead of a number, so that narrative evidence (auditor's report
flags, management commentary, news items) can be cited by interpretations. Calculations still
need a number. The change relaxes one CHECK constraint; no data changes.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_evidence_records_numeric_classes_have_value"
_NEW = (
    "evidence_class NOT IN ('FACT', 'CALCULATION') OR value IS NOT NULL "
    "OR (evidence_class = 'FACT' AND statement_text IS NOT NULL AND statement_text <> '')"
)
_OLD = "evidence_class NOT IN ('FACT', 'CALCULATION') OR value IS NOT NULL"


def upgrade() -> None:
    op.drop_constraint(op.f(_NAME), "evidence_records", type_="check")
    op.create_check_constraint(op.f(_NAME), "evidence_records", _NEW)


def downgrade() -> None:
    op.drop_constraint(op.f(_NAME), "evidence_records", type_="check")
    op.create_check_constraint(op.f(_NAME), "evidence_records", _OLD)
