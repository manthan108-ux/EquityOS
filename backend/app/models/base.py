from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit constraint names keep Alembic migrations deterministic and let us drop
# or alter constraints by name later.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# Indexes created in SQL that the models cannot express (Alembic cannot render the regconfig
# literal of a full-text index). They exist on purpose and must not be reported as drift.
SQL_ONLY_INDEXES = {"ix_document_pages_fts"}


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Alembic filter: ignore the deliberate SQL-only indexes when comparing models and database."""
    return not (type_ == "index" and reflected and name in SQL_ONLY_INDEXES)
