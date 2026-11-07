"""Audit trail for bulk master-data imports (the official exchange security lists)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MasterDataImport(Base):
    __tablename__ = "master_data_imports"
    __table_args__ = (
        CheckConstraint("rows_imported + rows_skipped = rows_total", name="row_counts_add_up"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_code: Mapped[str] = mapped_column(String(10))
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    rows_total: Mapped[int]
    rows_imported: Mapped[int]
    rows_skipped: Mapped[int]
    notes: Mapped[str | None] = mapped_column(Text)  # skip reasons summary
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
