"""Provider adapters that put the existing acquisition code behind the common interface.

Each adapter DECLARES the datasets it can serve (`spec.datasets`). Declaring is not proof: a
dataset is supported only once `dataset_verifications` holds a successful live retrieval.
"""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.acquisition.http import FetchResult, PoliteClient
from app.core.config import get_settings
from app.master_data.importer import SourceFile, import_master_data
from app.models import FetchStatus, Security
from app.providers.base import CompanyRef, Dataset, Outcome, ProviderResult, ProviderSpec
from app.providers.company_ir import CompanyIrProvider
from app.providers.exchange_files import (
    BhavcopyProvider,
    NseEquityListProvider,
    store_prices,
)
from app.providers.feeds import CompanyNewsProvider, RbiPressReleases, SebiAnnouncements
from app.providers.nse_history import (
    NseNameHistoryProvider,
    apply_proposals,
    build_proposals,
    parse_name_changes,
    parse_symbol_changes,
)
from app.providers.persistence import archive_payload

NO_FILE = (FetchStatus.NOT_FOUND, FetchStatus.INVALID_CONTENT, FetchStatus.CLIENT_ERROR)


def _failed(provider: str, dataset: Dataset, fetch: FetchResult, what: str) -> ProviderResult:
    return ProviderResult(
        provider,
        dataset,
        Outcome.FAILED,
        f"{what}: {fetch.status.value} {fetch.error or ''}".strip(),
        fetch_status=fetch.status.value,
        source_url=fetch.url,
    )


# ---------------------------------------------------------------------------------------------
class NseProvider:
    """NSE static archive files (company lists and name history). Interactive APIs are not used."""

    spec = ProviderSpec(
        code="NSE_ARCHIVES",
        name="NSE static archive files",
        authority_tier=1,
        datasets=frozenset({Dataset.SECURITY_MASTER, Dataset.NAME_HISTORY}),
        auth_requirement="None",
        access_method="Public static downloads on archives.nseindia.com, honest User-Agent",
        rate_limits="Self-imposed: 1 request per 2 s per host",
        coverage_note="All NSE equities (main board + SME); name and symbol change history. "
        "Filings, announcements, results and XBRL are NOT available (protected APIs, not used).",
        terms_note="Public files for own research use; NSE terms apply.",
        redistribution_policy="No redistribution",
    )

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def is_configured(self) -> bool:
        return True

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        code = self.spec.code
        if dataset is Dataset.SECURITY_MASTER:
            provider = NseEquityListProvider(self._client)
            files = [("EQUITY_L.csv", provider.fetch()), ("SME_EQUITY_L.csv", provider.fetch_sme())]
            for name, fetch in files:
                if not fetch.ok:
                    return _failed(code, dataset, fetch, name)
            report = import_master_data(
                session, [SourceFile("NSE", name, f.content or b"") for name, f in files]
            )
            return ProviderResult(
                code,
                dataset,
                Outcome.OK,
                f"{report.companies_created} companies created, "
                f"{report.securities_created} listings created, "
                f"{report.securities_updated} listings confirmed",
                report.securities_created + report.securities_updated,  # rows processed
                retrieved_at=datetime.now(UTC),
            )
        if dataset is Dataset.NAME_HISTORY:
            names, symbols = NseNameHistoryProvider(self._client).fetch()
            for label, fetch in (("namechange.csv", names), ("symbolchange.csv", symbols)):
                if not fetch.ok:
                    return _failed(code, dataset, fetch, label)
            report = build_proposals(
                session, parse_name_changes(names.text), parse_symbol_changes(symbols.text)
            )
            added = apply_proposals(session, report.proposals)
            return ProviderResult(
                code,
                dataset,
                Outcome.OK if added or report.already_known else Outcome.NO_DATA,
                f"{added} aliases added, {report.already_known} already known",
                added + report.already_known,  # rows matched to our companies
                retrieved_at=datetime.now(UTC),
            )
        return ProviderResult(
            code, dataset, Outcome.UNSUPPORTED, f"NSE files do not provide {dataset.value}"
        )


class BseProvider:
    """BSE: the company list is a manual download; BSE's interactive API is not used."""

    spec = ProviderSpec(
        code="BSE_LIST",
        name="BSE security list (manual download) and static files",
        authority_tier=1,
        datasets=frozenset({Dataset.SECURITY_MASTER}),
        auth_requirement="None (the list is downloaded by a person in a browser)",
        access_method="Manual file import: python -m app.master_data.cli --bse FILE",
        rate_limits="n/a (manual)",
        coverage_note="All BSE-listed equities from the file the user downloads. Filings, announcements "
        "and results are NOT available (protected API, not used).",
        terms_note="BSE website terms apply to the downloaded file; own research use.",
        redistribution_policy="No redistribution",
    )

    def is_configured(self) -> bool:
        return True

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        if dataset is Dataset.SECURITY_MASTER:
            has = session.scalar(
                select(Security.id)
                .where(Security.company_id == company.id, Security.security_code.is_not(None))
                .limit(1)
            )
            return ProviderResult(
                self.spec.code,
                dataset,
                Outcome.OK if has else Outcome.NO_DATA,
                "BSE listing present in master data" if has else "no BSE listing in master data",
                rows=1 if has else 0,  # the evidence is the listing row itself
            )
        return ProviderResult(
            self.spec.code, dataset, Outcome.UNSUPPORTED, f"BSE does not provide {dataset.value}"
        )


# ---------------------------------------------------------------------------------------------
class MarketDataProvider:
    """Official end-of-day prices from the exchanges' static bhavcopy files (NSE first, then BSE)."""

    spec = ProviderSpec(
        code="MARKET_DATA",
        name="Exchange end-of-day price files (NSE, BSE)",
        authority_tier=1,
        datasets=frozenset({Dataset.MARKET_PRICES_EOD}),
        auth_requirement="None",
        access_method="Public static daily files; BSE needs 8 s spacing and 30 s backoff after HTTP 406",
        rate_limits="Self-imposed: NSE 1 request per 2 s, BSE 1 per 8 s",
        coverage_note="All NSE and BSE securities, unadjusted for corporate actions. NSE history is deep; "
        "BSE older files are often removed.",
        terms_note="Public files for own research use; exchange terms apply.",
        redistribution_policy="No redistribution",
    )

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def is_configured(self) -> bool:
        return True

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        if dataset is not Dataset.MARKET_PRICES_EOD:
            return ProviderResult(
                self.spec.code, dataset, Outcome.UNSUPPORTED, "only end-of-day prices"
            )
        end: date = params.get("to_date") or datetime.now(UTC).date() - timedelta(days=1)
        start: date = params.get("from_date") or end - timedelta(days=7)
        exchanges = params.get("exchanges") or ["NSE", "BSE"]
        stored, notes, days = 0, [], set()
        for exchange in exchanges:
            provider = BhavcopyProvider(self._client, exchange)
            day = start
            while day <= end:
                if day.weekday() < 5:
                    fetch = provider.fetch(day)
                    if fetch.ok:
                        report = store_prices(session, exchange, provider.parse(fetch), fetch)
                        stored += report.stored
                        days.add(day.isoformat())
                    elif fetch.status not in NO_FILE:
                        notes.append(f"{exchange} {day}: {fetch.status.value}")
                day += timedelta(days=1)
        if stored or days:
            return ProviderResult(
                self.spec.code,
                dataset,
                Outcome.OK,
                f"{stored} new price rows over {len(days)} days"
                + (f"; {'; '.join(notes[:3])}" if notes else ""),
                stored,
                sorted(days, reverse=True),
                retrieved_at=datetime.now(UTC),
            )
        return ProviderResult(
            self.spec.code,
            dataset,
            Outcome.FAILED if notes else Outcome.NO_DATA,
            "; ".join(notes[:3]) or "no price files for the period",
        )


# ---------------------------------------------------------------------------------------------
class CompanyIrAdapter:
    """Investor-relations links from a company's own website (candidates, not downloads)."""

    _KIND_TO_DATASET = {
        "annual_report": Dataset.ANNUAL_REPORTS,
        "financial_results": Dataset.QUARTERLY_RESULT_DOCS,
        "investor_presentation": Dataset.INVESTOR_PRESENTATIONS,
        "earnings_transcript": Dataset.EARNINGS_TRANSCRIPTS,
    }
    spec = ProviderSpec(
        code="COMPANY_IR",
        name="Company websites / investor-relations pages",
        authority_tier=2,
        datasets=frozenset(_KIND_TO_DATASET.values()),
        auth_requirement="None",
        access_method="Polite crawl of the home page and up to 3 investor-relations pages; robots.txt obeyed",
        rate_limits="Self-imposed: 1 request per second per host",
        coverage_note="Varies by company: some sites block automated access (HTTP 403) and layouts differ. "
        "Discovery yields candidate links; documents must be downloaded and checked separately.",
        terms_note="Each site's robots.txt and terms apply; access denials are never bypassed.",
        redistribution_policy="Company documents are public but copyrighted: store for analysis, cite, do not republish",
    )

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def is_configured(self) -> bool:
        return True

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        code = self.spec.code
        if dataset not in self.spec.datasets:
            return ProviderResult(
                code, dataset, Outcome.UNSUPPORTED, f"IR discovery has no {dataset.value}"
            )
        if not company.website:
            return ProviderResult(code, dataset, Outcome.NO_DATA, "company website unknown")
        discovery = CompanyIrProvider(self._client).discover(company.website)
        if not discovery.fetch.ok:
            return _failed(code, dataset, discovery.fetch, "company website")
        wanted = [
            link for link in discovery.links if self._KIND_TO_DATASET.get(link.kind) is dataset
        ]
        retrieved = discovery.fetch.retrieved_at or datetime.now(UTC)
        if wanted:
            archive_payload(
                session,
                company_id=company.id,
                provider_code=code,
                dataset=dataset.value,
                request_url=company.website,
                payload=[link.__dict__ for link in wanted],
                retrieved_at=retrieved,
            )
        docs = sum(1 for link in wanted if link.is_document)
        if not wanted:
            return ProviderResult(
                code,
                dataset,
                Outcome.NO_DATA,
                f"no {dataset.value} links found ({len(discovery.pages_fetched)} pages checked)",
                source_url=company.website,
                retrieved_at=retrieved,
            )
        periods = sorted({link.period_hint for link in wanted if link.period_hint}, reverse=True)
        return ProviderResult(
            code,
            dataset,
            Outcome.OK,
            f"{len(wanted)} candidate link(s), {docs} direct file(s); NOT downloaded",
            len(wanted),
            periods,
            company.website,
            retrieved,
            extra={"links": [link.url for link in wanted]},
        )


class RegulatorDataProvider:
    spec = ProviderSpec(
        code="REGULATOR_FEEDS",
        name="Regulator publication feeds (RBI, SEBI)",
        authority_tier=3,
        datasets=frozenset({Dataset.REGULATORY_PUBLICATIONS}),
        auth_requirement="None",
        access_method="Published RSS feeds",
        rate_limits="Self-imposed; feeds cached 30 minutes",
        coverage_note="RBI press releases and SEBI announcements only (recent items). IRDAI, TRAI, CERC, PPAC "
        "etc. are not yet integrated; IRDAI's robots.txt disallows automated access.",
        terms_note="Public feeds.",
        redistribution_policy="Link and cite; do not republish",
    )

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def is_configured(self) -> bool:
        return True

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        if dataset is not Dataset.REGULATORY_PUBLICATIONS:
            return ProviderResult(
                self.spec.code, dataset, Outcome.UNSUPPORTED, "only regulatory publications"
            )
        results = [
            RbiPressReleases(self._client).latest(),
            SebiAnnouncements(self._client).latest(),
        ]
        ok = [r for r in results if r.ok]
        if not ok:
            return _failed(self.spec.code, dataset, results[0].fetch, "regulator feeds")
        items = [i for r in ok for i in r.items]
        return ProviderResult(
            self.spec.code,
            dataset,
            Outcome.OK if items else Outcome.NO_DATA,
            f"{len(items)} recent items from {len(ok)}/2 feeds (not company-specific)",
            len(items),
            retrieved_at=datetime.now(UTC),
        )


class NewsAdapter:
    spec = ProviderSpec(
        code="NEWS_FEEDS",
        name="Publisher news feeds (Mint, Economic Times, Moneycontrol, BusinessLine)",
        authority_tier=5,
        datasets=frozenset({Dataset.NEWS}),
        auth_requirement="None",
        access_method="Published RSS feeds, filtered locally for the company name",
        rate_limits="Self-imposed; feeds cached 30 minutes",
        coverage_note="Recent window only (feeds hold current items). Headlines are leads, never proof.",
        terms_note="Public feeds; headlines and links only.",
        redistribution_policy="Link and cite; never copy article text",
    )

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def is_configured(self) -> bool:
        return True

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        if dataset is not Dataset.NEWS:
            return ProviderResult(self.spec.code, dataset, Outcome.UNSUPPORTED, "only news")
        names = [company.common_name, company.legal_name.removesuffix(" Limited")]
        result = CompanyNewsProvider(self._client).headlines(names)
        if result.feeds_ok == 0:
            return _failed(self.spec.code, dataset, result.fetches[0], "all news feeds")
        retrieved = datetime.now(UTC)
        if result.items:
            archive_payload(
                session,
                company_id=company.id,
                provider_code=self.spec.code,
                dataset=dataset.value,
                request_url="rss:publisher-feeds",
                payload=[
                    {
                        "title": i.title,
                        "link": i.link,
                        "publisher": i.source_code,
                        "published": i.published.isoformat() if i.published else None,
                    }
                    for i in result.items
                ],
                retrieved_at=retrieved,
            )
        return ProviderResult(
            self.spec.code,
            dataset,
            Outcome.OK if result.items else Outcome.NO_DATA,
            f"{len(result.items)} headline(s) from {result.feeds_ok}/{len(result.fetches)} feeds",
            len(result.items),
            retrieved_at=retrieved,
        )


class GovernmentDataProvider:
    """data.gov.in open-data resources (free API key). Generic: you name the resource."""

    spec = ProviderSpec(
        code="DATA_GOV_IN",
        name="data.gov.in (Government of India open data)",
        authority_tier=4,
        datasets=frozenset({Dataset.GOVERNMENT_STATISTICS}),
        auth_requirement="Free API key from data.gov.in (DATA_GOV_IN_API_KEY)",
        access_method="Documented REST API: api.data.gov.in/resource/<id>?api-key=...&format=json",
        rate_limits="Not documented here; self-imposed 1 request per second",
        coverage_note="Macro and sector statistics by resource id. Not company financials.",
        terms_note="Government Open Data License - India (GODL).",
        redistribution_policy="Permitted under GODL with attribution",
        env_vars=("DATA_GOV_IN_API_KEY",),
    )
    BASE = "https://api.data.gov.in/resource"

    def __init__(self, client: PoliteClient, api_key: str | None = None) -> None:
        self._client = client
        self._key_override = api_key

    def _key(self) -> str | None:
        key = (
            self._key_override
            if self._key_override is not None
            else get_settings().data_gov_in_api_key
        )
        return key.strip() if key and key.strip() else None

    def is_configured(self) -> bool:
        return self._key() is not None

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        code = self.spec.code
        if dataset is not Dataset.GOVERNMENT_STATISTICS:
            return ProviderResult(code, dataset, Outcome.UNSUPPORTED, "only government statistics")
        if not self.is_configured():
            return ProviderResult(
                code, dataset, Outcome.UNCONFIGURED, "No data.gov.in API key (DATA_GOV_IN_API_KEY)"
            )
        resource = params.get("resource_id")
        if not resource:
            return ProviderResult(code, dataset, Outcome.NO_DATA, "no resource_id requested")
        url = f"{self.BASE}/{resource}?api-key={self._key()}&format=json&limit={int(params.get('limit', 100))}"
        fetch = self._client.fetch(
            url, source=code, documented_api=True, secret_params=("api-key",)
        )
        if not fetch.ok or not fetch.content:
            return _failed(code, dataset, fetch, "data.gov.in")
        try:
            body = json.loads(fetch.content)
            records = body["records"]
        except (ValueError, KeyError, TypeError):
            return ProviderResult(
                code, dataset, Outcome.FAILED, "response did not match the documented shape"
            )
        archive_payload(
            session,
            company_id=None,
            provider_code=code,
            dataset=dataset.value,
            request_url=fetch.url,
            payload=body,
            retrieved_at=fetch.retrieved_at or datetime.now(UTC),
        )
        return ProviderResult(
            code,
            dataset,
            Outcome.OK if records else Outcome.NO_DATA,
            f"{len(records)} records",
            len(records),
            source_url=fetch.url,
            retrieved_at=fetch.retrieved_at,
        )


__all__ = [
    "BseProvider",
    "CompanyIrAdapter",
    "GovernmentDataProvider",
    "MarketDataProvider",
    "NewsAdapter",
    "NseProvider",
    "RegulatorDataProvider",
]
