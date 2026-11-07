import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict

from app.models import SecurityStatus
from app.services.company_resolver import MatchType, ResolutionStatus


class ListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    exchange: str
    symbol: str
    security_code: str | None
    isin: str
    status: SecurityStatus


class CompanySummary(BaseModel):
    id: uuid.UUID
    legal_name: str
    common_name: str
    sector: str | None
    industry: str | None
    listings: list[ListingOut]


class CompanyMatch(BaseModel):
    company: CompanySummary
    match_type: MatchType
    score: float
    matched_on: str


class SearchResponse(BaseModel):
    query: str
    status: ResolutionStatus
    matches: list[CompanyMatch]
    notes: list[str]


class AliasOut(BaseModel):
    alias: str
    alias_type: str


class CompanyProfile(CompanySummary):
    website: str | None
    investor_relations_url: str | None
    headquarters: str | None
    founding_year: int | None
    business_classification: str | None
    aliases: list[AliasOut]
    master_data_source: str
    # Fields that are NULL above are "not verified", never "none"/"zero".
    unverified_fields: list[str]
    listing_dates: dict[str, date | None]
