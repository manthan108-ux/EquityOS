"""full-text search over document page text

Retrieval for the research agents: a question is answered with the pages that contain the words,
each with its document and page number (so every statement an agent makes can be cited). The index
is on the stored page text; nothing is copied.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_document_pages_fts ON document_pages "
        "USING gin (to_tsvector('english', text))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_document_pages_fts")
