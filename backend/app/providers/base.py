"""Common types for data providers and the router.

Declared vs verified: a provider *declares* which datasets it can serve (`ProviderSpec.datasets`).
A dataset is only treated as SUPPORTED once a live retrieval has been validated and recorded in
`dataset_verifications`. Nothing here assumes a provider supports every dataset.
"""

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session


class Dataset(enum.StrEnum):
    # Structured financial data
    COMPANY_PROFILE = "COMPANY_PROFILE"
    INCOME_ANNUAL = "INCOME_ANNUAL"
    INCOME_QUARTERLY = "INCOME_QUARTERLY"
    BALANCE_SHEET = "BALANCE_SHEET"
    CASH_FLOW = "CASH_FLOW"
    SHAREHOLDING = "SHAREHOLDING"
    KEY_RATIOS = "KEY_RATIOS"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    COMPETITORS = "COMPETITORS"
    # Market data
    MARKET_PRICES_EOD = "MARKET_PRICES_EOD"
    # Documents and filings
    ANNUAL_REPORTS = "ANNUAL_REPORTS"
    QUARTERLY_RESULT_DOCS = "QUARTERLY_RESULT_DOCS"
    INVESTOR_PRESENTATIONS = "INVESTOR_PRESENTATIONS"
    EARNINGS_TRANSCRIPTS = "EARNINGS_TRANSCRIPTS"
    XBRL_FILINGS = "XBRL_FILINGS"
    CORPORATE_ANNOUNCEMENTS = "CORPORATE_ANNOUNCEMENTS"
    # Context
    NEWS = "NEWS"
    REGULATORY_PUBLICATIONS = "REGULATORY_PUBLICATIONS"
    GOVERNMENT_STATISTICS = "GOVERNMENT_STATISTICS"
    # Reference
    SECURITY_MASTER = "SECURITY_MASTER"
    NAME_HISTORY = "NAME_HISTORY"


class Outcome(enum.StrEnum):
    OK = "OK"  # data retrieved, validated and stored
    NO_DATA = "NO_DATA"  # the provider answered but has nothing for this company/period
    UNSUPPORTED = "UNSUPPORTED"  # this provider does not offer the dataset
    UNCONFIGURED = "UNCONFIGURED"  # credentials / setup missing
    FAILED = "FAILED"  # the fetch failed or the response did not validate


@dataclass(frozen=True)
class ProviderSpec:
    code: str
    name: str
    authority_tier: int
    datasets: frozenset[Dataset]  # DECLARED, not proven
    auth_requirement: str
    access_method: str
    rate_limits: str
    coverage_note: str
    terms_note: str
    redistribution_policy: str
    env_vars: tuple[str, ...] = ()  # all must be set for the provider to be configured


@dataclass(frozen=True)
class CompanyRef:
    """What providers need to know about a company, decoupled from the ORM."""

    id: uuid.UUID
    legal_name: str
    common_name: str
    isin: str | None
    nse_symbol: str | None
    bse_code: str | None
    website: str | None


@dataclass
class ProviderResult:
    provider: str
    dataset: Dataset
    outcome: Outcome
    message: str = ""
    rows: int = 0  # facts written
    periods: list[str] = field(default_factory=list)  # period labels covered, newest first
    source_url: str | None = None
    retrieved_at: datetime | None = None
    fetch_status: str | None = None  # FetchStatus value of the underlying fetch, if any
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


class DataProvider(Protocol):
    spec: ProviderSpec

    def is_configured(self) -> bool: ...

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult: ...
