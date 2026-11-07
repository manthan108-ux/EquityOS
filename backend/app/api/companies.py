import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.models import Company
from app.repositories import company_repository as repo
from app.schemas.company import (
    AliasOut,
    CompanyMatch,
    CompanyProfile,
    CompanySummary,
    ListingOut,
    SearchResponse,
)
from app.services.company_resolver import MAX_QUERY_LENGTH, resolve_company

router = APIRouter(prefix="/api/companies", tags=["companies"])

SessionDep = Annotated[Session, Depends(get_session)]

_PROFILE_FIELDS = (
    "website",
    "investor_relations_url",
    "headquarters",
    "founding_year",
    "business_classification",
)


def _listings(company: Company) -> list[ListingOut]:
    ordered = sorted(company.securities, key=lambda s: s.exchange.code)
    return [
        ListingOut(
            exchange=s.exchange.code,
            symbol=s.symbol,
            security_code=s.security_code,
            isin=s.isin,
            status=s.status,
        )
        for s in ordered
    ]


def _summary(company: Company) -> CompanySummary:
    return CompanySummary(
        id=company.id,
        legal_name=company.legal_name,
        common_name=company.common_name,
        sector=company.sector.name if company.sector else None,
        industry=company.industry.name if company.industry else None,
        listings=_listings(company),
    )


@router.get("/search", response_model=SearchResponse)
def search_companies(
    session: SessionDep,
    q: Annotated[
        str,
        Query(
            min_length=1, max_length=MAX_QUERY_LENGTH, description="Name, ticker, BSE code or ISIN"
        ),
    ],
) -> SearchResponse:
    """Resolve free text to NSE/BSE companies. Never guesses; ambiguity returns all candidates."""
    resolution = resolve_company(session, q)
    companies = repo.get_companies(session, [c.company_id for c in resolution.candidates])
    matches = [
        CompanyMatch(
            company=_summary(companies[c.company_id]),
            match_type=c.match_type,
            score=c.score,
            matched_on=c.matched_on,
        )
        for c in resolution.candidates
        if c.company_id in companies
    ]
    return SearchResponse(
        query=resolution.query, status=resolution.status, matches=matches, notes=resolution.notes
    )


@router.get("/{company_id}", response_model=CompanyProfile)
def get_company_profile(company_id: uuid.UUID, session: SessionDep) -> CompanyProfile:
    company = repo.get_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    summary = _summary(company)
    return CompanyProfile(
        **summary.model_dump(),
        website=company.website,
        investor_relations_url=company.investor_relations_url,
        headquarters=company.headquarters,
        founding_year=company.founding_year,
        business_classification=company.business_classification,
        aliases=[
            AliasOut(alias=a.alias, alias_type=a.alias_type.value)
            for a in sorted(company.aliases, key=lambda a: a.alias)
        ],
        master_data_source=company.master_data_source,
        unverified_fields=[f for f in _PROFILE_FIELDS if getattr(company, f) is None],
        listing_dates={f"{s.exchange.code}:{s.symbol}": s.listing_date for s in company.securities},
    )
