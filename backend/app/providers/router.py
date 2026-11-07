"""The Data Provider Router: the single entry point agents use to obtain a dataset.

Guarantees:
  * Providers that do not declare a dataset are never asked for it.
  * Providers are tried in a fixed, documented priority order (most authoritative first).
  * The result names the provider that supplied the data AND lists every attempt, so a fallback is
    always visible. There is no silent source substitution.
  * An unconfigured or failing provider is reported, not hidden, and its data is never invented.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.providers.base import CompanyRef, DataProvider, Dataset, Outcome, ProviderResult

# Priority per dataset (first = preferred). A dataset absent here has no router-fetchable source.
PRIORITY: dict[Dataset, list[str]] = {
    Dataset.COMPANY_PROFILE: ["UPSTOX_FUNDAMENTALS"],
    Dataset.INCOME_ANNUAL: ["DOCUMENT_EXTRACTION", "UPSTOX_FUNDAMENTALS", "MANUAL"],
    Dataset.INCOME_QUARTERLY: ["DOCUMENT_EXTRACTION", "UPSTOX_FUNDAMENTALS", "MANUAL"],
    Dataset.BALANCE_SHEET: ["DOCUMENT_EXTRACTION", "UPSTOX_FUNDAMENTALS", "MANUAL"],
    Dataset.CASH_FLOW: ["DOCUMENT_EXTRACTION", "UPSTOX_FUNDAMENTALS", "MANUAL"],
    Dataset.SHAREHOLDING: ["UPSTOX_FUNDAMENTALS"],
    Dataset.KEY_RATIOS: ["UPSTOX_FUNDAMENTALS"],
    Dataset.CORPORATE_ACTIONS: ["UPSTOX_FUNDAMENTALS"],
    Dataset.COMPETITORS: ["UPSTOX_FUNDAMENTALS"],
    Dataset.MARKET_PRICES_EOD: ["MARKET_DATA"],
    Dataset.ANNUAL_REPORTS: ["MANUAL", "COMPANY_IR"],
    Dataset.QUARTERLY_RESULT_DOCS: ["MANUAL", "COMPANY_IR"],
    Dataset.INVESTOR_PRESENTATIONS: ["MANUAL", "COMPANY_IR"],
    Dataset.EARNINGS_TRANSCRIPTS: ["MANUAL", "COMPANY_IR"],
    Dataset.XBRL_FILINGS: ["MANUAL"],
    Dataset.CORPORATE_ANNOUNCEMENTS: ["MANUAL"],  # exchange announcement APIs are not used
    Dataset.NEWS: ["NEWS_FEEDS"],
    Dataset.REGULATORY_PUBLICATIONS: ["REGULATOR_FEEDS"],
    Dataset.GOVERNMENT_STATISTICS: ["DATA_GOV_IN"],
    Dataset.SECURITY_MASTER: ["NSE_ARCHIVES", "BSE_LIST"],
    Dataset.NAME_HISTORY: ["NSE_ARCHIVES"],
}


@dataclass
class RouteResult:
    dataset: Dataset
    chosen: ProviderResult | None  # the provider that supplied the data, if any
    attempts: list[ProviderResult] = field(default_factory=list)  # every provider tried, in order
    fallback_used: bool = False  # True if the winner was not the first-choice provider

    @property
    def ok(self) -> bool:
        return self.chosen is not None and self.chosen.ok

    def explain(self) -> str:
        trail = (
            " -> ".join(f"{a.provider}:{a.outcome.value}" for a in self.attempts) or "no providers"
        )
        return f"{self.dataset.value}: {trail}"


class DataRouter:
    def __init__(self, providers: list[DataProvider]) -> None:
        self._by_code = {p.spec.code: p for p in providers}

    def providers_for(self, dataset: Dataset) -> list[DataProvider]:
        order = PRIORITY.get(dataset, [])
        # Only providers that DECLARE the dataset are eligible, in priority order.
        return [
            self._by_code[c]
            for c in order
            if c in self._by_code and dataset in self._by_code[c].spec.datasets
        ]

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> RouteResult:
        """Try providers in priority order until one succeeds."""
        candidates = self.providers_for(dataset)
        route = RouteResult(dataset, None)
        for index, provider in enumerate(candidates):
            result = self._call(session, provider, company, dataset, params)
            route.attempts.append(result)
            if result.ok:
                route.chosen = result
                route.fallback_used = index > 0
                break
        return route

    def acquire_all(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> RouteResult:
        """Ask every eligible provider (cross-source comparison); `chosen` is the first success."""
        route = RouteResult(dataset, None)
        for index, provider in enumerate(self.providers_for(dataset)):
            result = self._call(session, provider, company, dataset, params)
            route.attempts.append(result)
            if result.ok and route.chosen is None:
                route.chosen = result
                route.fallback_used = index > 0
        return route

    @staticmethod
    def _call(
        session: Session,
        provider: DataProvider,
        company: CompanyRef,
        dataset: Dataset,
        params: dict,
    ) -> ProviderResult:
        if not provider.is_configured():
            needed = ", ".join(provider.spec.env_vars) or "setup"
            return ProviderResult(
                provider.spec.code, dataset, Outcome.UNCONFIGURED, f"not configured ({needed})"
            )
        try:
            return provider.acquire(session, company, dataset, **params)
        except Exception as exc:  # noqa: BLE001 - a provider bug must not take down a research job
            return ProviderResult(
                provider.spec.code, dataset, Outcome.FAILED, f"provider error: {type(exc).__name__}"
            )
