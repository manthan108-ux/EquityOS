"""Company master data.

A *company* is a legal entity and exists once. A *security* is one exchange listing of that
company (NSE and BSE listings are two securities of one company). This separation prevents
duplicate company records for dual-listed businesses.
"""

import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.reference import Exchange, Industry, Sector


class SecurityStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"


class AliasType(enum.StrEnum):
    LEGAL_NAME = "LEGAL_NAME"
    COMMON_NAME = "COMMON_NAME"
    ABBREVIATION = "ABBREVIATION"
    FORMER_NAME = "FORMER_NAME"
    OTHER = "OTHER"


def _str_enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    # Stored as VARCHAR + CHECK constraint (not a native PG enum): easier to migrate.
    return Enum(enum_cls, name=name, native_enum=False, length=20, validate_strings=True)


class Company(TimestampMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint("founding_year BETWEEN 1600 AND 2100", name="founding_year_range"),
        Index("ix_companies_legal_name", "legal_name"),
    )

    # Canonical company ID: stable, opaque and independent of any exchange code.
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Issuer identity derived from the ISIN (characters 1-7). It is what merges the NSE and BSE
    # listings of one company. NULL only for companies created without any ISIN.
    # Names are NOT unique: different issuers can legitimately share one.
    issuer_key: Mapped[str | None] = mapped_column(String(7), unique=True)
    legal_name: Mapped[str] = mapped_column(String(300))
    common_name: Mapped[str] = mapped_column(String(200))
    sector_id: Mapped[int | None] = mapped_column(ForeignKey("sectors.id", ondelete="RESTRICT"))
    industry_id: Mapped[int | None] = mapped_column(
        ForeignKey("industries.id", ondelete="RESTRICT")
    )
    website: Mapped[str | None] = mapped_column(String(255))
    investor_relations_url: Mapped[str | None] = mapped_column(String(500))
    headquarters: Mapped[str | None] = mapped_column(String(120))
    founding_year: Mapped[int | None]
    business_classification: Mapped[str | None] = mapped_column(Text)
    # Industry label exactly as published by the exchange (e.g. BSE). Raw provenance; the
    # research sector/industry above are assigned separately by the Phase 8 sector engine.
    exchange_industry: Mapped[str | None] = mapped_column(String(200))
    # Where the identity fields above came from and how far they are verified. NULL fields mean
    # "not verified", never "zero" or "none".
    master_data_source: Mapped[str] = mapped_column(String(200))

    sector: Mapped[Sector | None] = relationship()
    industry: Mapped[Industry | None] = relationship()
    securities: Mapped[list["Security"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    aliases: Mapped[list["CompanyAlias"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class Security(TimestampMixin, Base):
    """One exchange listing of a company."""

    __tablename__ = "securities"
    __table_args__ = (
        UniqueConstraint("exchange_id", "symbol"),
        UniqueConstraint("exchange_id", "security_code"),
        CheckConstraint("isin ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'", name="isin_format"),
        CheckConstraint(
            "security_code IS NULL OR security_code ~ '^[0-9]{6}$'", name="bse_code_format"
        ),
        Index("ix_securities_isin", "isin"),
        Index("ix_securities_company_id", "company_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(30))  # NSE symbol, or BSE scrip ID
    security_code: Mapped[str | None] = mapped_column(String(10))  # BSE 6-digit scrip code
    isin: Mapped[str] = mapped_column(String(12))
    series: Mapped[str | None] = mapped_column(String(10))  # NSE series or BSE group
    face_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    status: Mapped[SecurityStatus] = mapped_column(
        _str_enum(SecurityStatus, "security_status"), default=SecurityStatus.ACTIVE
    )
    listing_date: Mapped[date | None] = mapped_column(Date)
    last_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("master_data_imports.id", ondelete="SET NULL")
    )

    company: Mapped[Company] = relationship(back_populates="securities")
    exchange: Mapped[Exchange] = relationship()


class CompanyAlias(TimestampMixin, Base):
    """A name a user might type. `normalized_alias` is what the resolver matches against."""

    __tablename__ = "company_aliases"
    __table_args__ = (
        UniqueConstraint("company_id", "normalized_alias"),
        CheckConstraint("normalized_alias <> ''", name="normalized_alias_not_empty"),
        Index("ix_company_aliases_normalized_alias", "normalized_alias"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(200))
    normalized_alias: Mapped[str] = mapped_column(String(200))
    alias_type: Mapped[AliasType] = mapped_column(
        _str_enum(AliasType, "alias_type"), default=AliasType.OTHER
    )
    # Where this alias came from (e.g. "wikidata:Q908931"); NULL for seed/exchange-file aliases.
    source: Mapped[str | None] = mapped_column(String(100))

    company: Mapped[Company] = relationship(back_populates="aliases")
