"""Provider registry: what each provider claims, whether it is usable, and what is PROVEN.

Declared support (spec.datasets) is a claim. Verified support comes only from
`dataset_verifications`: a recorded, successful live retrieval. The registry reports both so
nothing is presented as working before it has actually worked.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.acquisition.http import PoliteClient
from app.docintel.provider import DocumentExtractionProvider
from app.models import (
    AcquisitionLog,
    DatasetVerification,
    DocumentExtraction,
    ExtractionStatus,
    FetchStatus,
    FinancialFact,
    Source,
)
from app.providers import manual, upstox
from app.providers.adapters import (
    BseProvider,
    CompanyIrAdapter,
    GovernmentDataProvider,
    MarketDataProvider,
    NewsAdapter,
    NseProvider,
    RegulatorDataProvider,
)
from app.providers.base import DataProvider, Dataset, ProviderSpec

# Acquisition-log source codes that belong to each provider (for "last successful retrieval").
LOG_SOURCES: dict[str, tuple[str, ...]] = {
    "NSE_ARCHIVES": ("NSE_ARCHIVES",),
    "BSE_LIST": ("BSE_BHAVCOPY",),
    "MARKET_DATA": ("NSE_ARCHIVES", "BSE_BHAVCOPY"),
    "COMPANY_IR": ("COMPANY_IR",),
    "REGULATOR_FEEDS": ("RBI_RSS", "SEBI_RSS"),
    "NEWS_FEEDS": ("MINT_RSS", "ET_RSS", "MONEYCONTROL_RSS", "BUSINESSLINE_RSS"),
    "UPSTOX_FUNDAMENTALS": ("UPSTOX_FUNDAMENTALS",),
    "DATA_GOV_IN": ("DATA_GOV_IN",),
    "MANUAL": ("MANUAL_UPLOAD", "MANUAL_IMPORT"),
}


def build_providers(client: PoliteClient) -> list[DataProvider]:
    """All providers, in no particular order (the router decides priority)."""
    return [
        upstox.UpstoxFundamentalsProvider(client),
        MarketDataProvider(client),
        NseProvider(client),
        BseProvider(),
        CompanyIrAdapter(client),
        RegulatorDataProvider(client),
        NewsAdapter(client),
        GovernmentDataProvider(client),
        DocumentExtractionProvider(),
        manual.ManualProvider(),
    ]


@dataclass
class ProviderStatus:
    spec: ProviderSpec
    configured: bool
    declared: list[Dataset]
    verified: list[Dataset]  # a live retrieval succeeded and was recorded
    failing: list[Dataset]  # most recent verification attempt failed
    unverified: list[Dataset]  # declared but never proven
    last_success_at: datetime | None
    last_failure: str | None
    notes: list[str] = field(default_factory=list)


def sync_registry(session: Session, providers: list[DataProvider]) -> int:
    """Write provider specs into the `sources` table (insert or update)."""
    count = 0
    for provider in providers:
        spec = provider.spec
        row = session.scalar(select(Source).where(Source.code == spec.code))
        if row is None:
            row = Source(code=spec.code, name=spec.name, authority_tier=spec.authority_tier)
            session.add(row)
        row.name = spec.name
        row.authority_tier = spec.authority_tier
        row.terms_note = spec.terms_note
        row.auth_requirement = spec.auth_requirement
        row.access_method = spec.access_method
        row.rate_limits = spec.rate_limits
        row.declared_datasets = sorted(d.value for d in spec.datasets)
        row.coverage_note = spec.coverage_note
        row.redistribution_policy = spec.redistribution_policy
        count += 1
    session.flush()
    return count


def verified_datasets(session: Session, provider_code: str) -> tuple[set[str], set[str]]:
    """(datasets with at least one successful live retrieval, datasets whose LATEST attempt failed).

    A dataset can be in both sets: it has been proven to work, but the most recent attempt (for some
    company) failed. Coverage varies by company, so both facts are reported.
    """
    ever_ok: set[str] = set()
    latest_failed: set[str] = set()
    rows = session.execute(
        select(DatasetVerification.dataset, DatasetVerification.ok)
        .where(DatasetVerification.provider_code == provider_code)
        .order_by(DatasetVerification.verified_at, DatasetVerification.id)
    ).all()
    for dataset, ok in rows:  # later rows overwrite the "latest" state
        if ok:
            ever_ok.add(dataset)
            latest_failed.discard(dataset)
        else:
            latest_failed.add(dataset)
    return ever_ok, latest_failed


def _extraction_success(session: Session) -> tuple[datetime | None, str | None]:
    """Document extraction fetches nothing: its history is its extraction runs."""
    ok = session.scalar(
        select(func.max(DocumentExtraction.finished_at)).where(
            DocumentExtraction.status == ExtractionStatus.COMPLETED
        )
    )
    bad = session.scalar(
        select(DocumentExtraction)
        .where(
            DocumentExtraction.status.in_([ExtractionStatus.FAILED, ExtractionStatus.UNSUPPORTED])
        )
        .order_by(DocumentExtraction.started_at.desc())
        .limit(1)
    )
    failure = f"{bad.status.value}: {bad.error or ''}".strip(": ") if bad else None
    return ok, failure


def last_success(session: Session, provider: DataProvider) -> tuple[datetime | None, str | None]:
    if provider.spec.code == "DOCUMENT_EXTRACTION":
        return _extraction_success(session)
    codes = LOG_SOURCES.get(provider.spec.code, (provider.spec.code,))
    ok_statuses = (FetchStatus.OK, FetchStatus.CACHED)
    ok = session.scalar(
        select(func.max(AcquisitionLog.started_at)).where(
            AcquisitionLog.source_code.in_(codes), AcquisitionLog.status.in_(ok_statuses)
        )
    )
    facts = session.scalar(
        select(func.max(FinancialFact.retrieved_at)).where(FinancialFact.provider_code.in_(codes))
    )
    best = max([t for t in (ok, facts) if t is not None], default=None)
    bad = session.scalar(
        select(AcquisitionLog)
        .where(AcquisitionLog.source_code.in_(codes), AcquisitionLog.status.not_in(ok_statuses))
        .order_by(AcquisitionLog.started_at.desc())
        .limit(1)
    )
    failure = f"{bad.status.value}: {bad.error or ''}".strip(": ") if bad else None
    return best, failure


def provider_statuses(session: Session, providers: list[DataProvider]) -> list[ProviderStatus]:
    out = []
    for provider in providers:
        spec = provider.spec
        ok, failed = verified_datasets(session, spec.code)
        declared = sorted(spec.datasets, key=lambda d: d.value)
        when, failure = last_success(session, provider)
        notes = []
        if not provider.is_configured():
            notes.append(f"Not configured: set {', '.join(spec.env_vars)} in .env")
        out.append(
            ProviderStatus(
                spec=spec,
                configured=provider.is_configured(),
                declared=declared,
                verified=[d for d in declared if d.value in ok],
                failing=[d for d in declared if d.value in failed],
                unverified=[d for d in declared if d.value not in ok],
                last_success_at=when,
                last_failure=failure,
                notes=notes,
            )
        )
    return out
