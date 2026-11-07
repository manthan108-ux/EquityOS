"""Idempotent seeding: running it twice leaves the database unchanged.

Usage (from ./backend):  python -m app.seeds.loader
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.core.identifiers import is_valid_isin, issuer_key
from app.core.text import normalize_text
from app.models import (
    AliasType,
    Company,
    CompanyAlias,
    Exchange,
    Industry,
    Sector,
    Security,
)
from app.seeds.reference_data import EXCHANGES, INDUSTRIES, SECTORS
from app.seeds.test_universe import SOURCE, TEST_UNIVERSE


def seed_reference_data(session: Session) -> None:
    for row in EXCHANGES:
        if session.scalar(select(Exchange).where(Exchange.code == row["code"])) is None:
            session.add(Exchange(**row))

    for code, name in SECTORS.items():
        if session.scalar(select(Sector).where(Sector.code == code)) is None:
            session.add(Sector(code=code, name=name))
    session.flush()

    sectors = {s.code: s for s in session.scalars(select(Sector))}
    for (sector_code, industry_code), name in INDUSTRIES.items():
        sector = sectors[sector_code]
        exists = session.scalar(
            select(Industry).where(Industry.sector_id == sector.id, Industry.code == industry_code)
        )
        if exists is None:
            session.add(Industry(sector_id=sector.id, code=industry_code, name=name))
    session.flush()


def seed_test_universe(session: Session) -> None:
    exchanges = {e.code: e for e in session.scalars(select(Exchange))}
    sectors = {s.code: s for s in session.scalars(select(Sector))}
    industries = {(i.sector_id, i.code): i for i in session.scalars(select(Industry))}

    for row in TEST_UNIVERSE:
        if not is_valid_isin(row["isin"]):
            raise ValueError(f"Refusing to seed {row['legal_name']}: invalid ISIN {row['isin']}")

        company = session.scalar(
            select(Company).where(Company.issuer_key == issuer_key(row["isin"]))
        )
        if company is not None:
            continue  # already seeded; never overwrite data that may have been verified since

        sector = sectors[row["sector"]]
        industry = industries[(sector.id, row["industry"])]
        company = Company(
            issuer_key=issuer_key(row["isin"]),
            legal_name=row["legal_name"],
            common_name=row["common_name"],
            sector_id=sector.id,
            industry_id=industry.id,
            website=row["website"],
            headquarters=row["headquarters"],
            founding_year=row["founding_year"],
            business_classification=row["business_classification"],
            master_data_source=SOURCE,
        )
        company.securities = [
            Security(
                exchange_id=exchanges["NSE"].id,
                symbol=row["nse_symbol"],
                isin=row["isin"],
                series="EQ",
            ),
            Security(
                exchange_id=exchanges["BSE"].id,
                symbol=row["bse_symbol"],
                security_code=row["bse_code"],
                isin=row["isin"],
            ),
        ]
        seen: set[str] = set()
        for text, alias_type in row["aliases"]:
            normalized = normalize_text(text)
            if normalized in seen:
                continue
            seen.add(normalized)
            company.aliases.append(
                CompanyAlias(
                    alias=text, normalized_alias=normalized, alias_type=AliasType(alias_type)
                )
            )
        session.add(company)
    session.flush()


def run() -> None:
    with Session(get_engine()) as session, session.begin():
        seed_reference_data(session)
        seed_test_universe(session)


if __name__ == "__main__":
    run()
    print("Seed complete.")
