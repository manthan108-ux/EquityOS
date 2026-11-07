"""Database queries for company lookup. The resolver and API call these; no logic lives here."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import AliasType, Company, CompanyAlias, Security


def find_company_ids_by_isin(session: Session, isin: str) -> list[uuid.UUID]:
    return list(
        session.scalars(select(Security.company_id).where(Security.isin == isin).distinct())
    )


def find_company_ids_by_security_code(session: Session, code: str) -> list[uuid.UUID]:
    stmt = select(Security.company_id).where(Security.security_code == code).distinct()
    return list(session.scalars(stmt))


def find_company_ids_by_symbol(session: Session, symbol: str) -> list[uuid.UUID]:
    """`symbol` must already be upper-cased; exchange symbols are stored upper-case."""
    return list(
        session.scalars(select(Security.company_id).where(Security.symbol == symbol).distinct())
    )


def find_aliases_by_normalized(
    session: Session, normalized: str
) -> list[tuple[uuid.UUID, AliasType, str]]:
    stmt = select(CompanyAlias.company_id, CompanyAlias.alias_type, CompanyAlias.alias).where(
        CompanyAlias.normalized_alias == normalized
    )
    return [(row[0], row[1], row[2]) for row in session.execute(stmt)]


def list_alias_index(session: Session) -> list[tuple[uuid.UUID, str, str]]:
    """(company_id, normalized_alias, display alias) for every alias; used for fuzzy matching."""
    stmt = select(CompanyAlias.company_id, CompanyAlias.normalized_alias, CompanyAlias.alias)
    return [(row[0], row[1], row[2]) for row in session.execute(stmt)]


def list_symbol_index(session: Session) -> list[tuple[uuid.UUID, str]]:
    stmt = select(Security.company_id, Security.symbol).distinct()
    return [(row[0], row[1]) for row in session.execute(stmt)]


def _with_relations():
    return (
        selectinload(Company.securities).selectinload(Security.exchange),
        selectinload(Company.aliases),
        selectinload(Company.sector),
        selectinload(Company.industry),
    )


def get_company(session: Session, company_id: uuid.UUID) -> Company | None:
    return session.scalar(
        select(Company).where(Company.id == company_id).options(*_with_relations())
    )


def get_companies(session: Session, company_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Company]:
    if not company_ids:
        return {}
    stmt = select(Company).where(Company.id.in_(company_ids)).options(*_with_relations())
    return {c.id: c for c in session.scalars(stmt)}
