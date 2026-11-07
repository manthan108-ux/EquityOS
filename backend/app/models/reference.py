"""Reference (lookup) tables: exchanges, sectors, industries."""

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Exchange(TimestampMixin, Base):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), unique=True)  # "NSE", "BSE"
    name: Mapped[str] = mapped_column(String(120))
    mic: Mapped[str] = mapped_column(String(4), unique=True)  # ISO 10383 market identifier code
    country_code: Mapped[str] = mapped_column(String(2), default="IN", server_default="IN")
    timezone: Mapped[str] = mapped_column(
        String(40), default="Asia/Kolkata", server_default="Asia/Kolkata"
    )


class Sector(TimestampMixin, Base):
    """A research sector. Each sector maps to a research profile in the Phase 8 sector engine."""

    __tablename__ = "sectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)  # stable key, e.g. "it_services"
    name: Mapped[str] = mapped_column(String(120), unique=True)

    industries: Mapped[list["Industry"]] = relationship(back_populates="sector")


class Industry(TimestampMixin, Base):
    __tablename__ = "industries"
    __table_args__ = (UniqueConstraint("sector_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sector_id: Mapped[int] = mapped_column(ForeignKey("sectors.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(160))

    sector: Mapped[Sector] = relationship(back_populates="industries")
