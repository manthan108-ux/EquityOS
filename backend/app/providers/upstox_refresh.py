"""Universe-wide, resumable refresh of Upstox fundamentals.

One company at a time, through the existing provider (`UpstoxFundamentalsProvider.acquire`), so every
value still goes through the same parsing, unit/period/basis handling and persistence as before.
This module only adds what a long run needs:

* ordering and batching (a run takes the next N companies that are due; it can be stopped and resumed);
* incremental refresh (a company refreshed recently is skipped; failures back off exponentially);
* an identity check (the profile text Upstox returns for the ISIN must mention the company);
* a circuit breaker (repeated rate-limit / auth failures stop the run instead of hammering the API);
* a record per company of what was refreshed, what Upstox does not have, and why anything failed.

Upstox's fundamentals terms and rate limits are undocumented, so the run is deliberately gentle
(the shared client spaces requests per host) and the default batch is small.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.text import normalize_text
from app.docintel.entity import company_names
from app.models import Company, ProviderRefreshState, Security
from app.providers.base import CompanyRef, Dataset, Outcome
from app.providers.upstox import SOURCE_CODE, UpstoxFundamentalsProvider

log = logging.getLogger("equityos.upstox_refresh")

# COMPETITORS is declared but returns CLIENT_ERROR for every company tried (docs/DATA_SOURCES.md).
DATASETS = (
    Dataset.COMPANY_PROFILE,
    Dataset.INCOME_ANNUAL,
    Dataset.INCOME_QUARTERLY,
    Dataset.BALANCE_SHEET,
    Dataset.CASH_FLOW,
    Dataset.SHAREHOLDING,
    Dataset.KEY_RATIOS,
    Dataset.CORPORATE_ACTIONS,
)
CORE = (Dataset.INCOME_ANNUAL, Dataset.BALANCE_SHEET, Dataset.CASH_FLOW)
REFRESHED, PARTIAL, UNAVAILABLE, NO_ISIN, MAPPING_FAILED, ERROR = (
    "REFRESHED",
    "PARTIAL",
    "UNAVAILABLE",
    "NO_ISIN",
    "MAPPING_FAILED",
    "ERROR",
)
# how long before a company is looked at again
FRESH_DAYS = {REFRESHED: 30, PARTIAL: 14, UNAVAILABLE: 60, NO_ISIN: 90, MAPPING_FAILED: 90}
ERROR_BACKOFF_DAYS = (1, 2, 4, 7)
_BLOCKERS = ("RATE_LIMITED", "ACCESS_DENIED", "UDAPI100050", "UDAPI100051", "UNAUTHORIZED")
BREAKER = 3  # consecutive provider-wide failures that stop a run


@dataclass
class CompanyOutcome:
    name: str
    status: str
    datasets: dict = field(default_factory=dict)
    note: str = ""
    new_rows: int = 0


@dataclass
class RefreshRun:
    outcomes: list[CompanyOutcome] = field(default_factory=list)
    stopped: str | None = None  # why the run ended early (a blocker), else None


def company_ref(session: Session, company: Company) -> CompanyRef:
    securities = list(session.scalars(select(Security).where(Security.company_id == company.id)))
    nse = next((s for s in securities if s.security_code is None), None)
    bse = next((s for s in securities if s.security_code), None)
    pick = nse or bse or (securities[0] if securities else None)
    return CompanyRef(
        id=company.id,
        legal_name=company.legal_name,
        common_name=company.common_name,
        isin=pick.isin if pick else None,
        nse_symbol=nse.symbol if nse else None,
        bse_code=bse.security_code if bse else None,
        website=company.website,
    )


def identity_matches(profile_text: str, names: set[str]) -> bool:
    """The profile Upstox returns for this ISIN must name the company we think it is."""
    text = f" {normalize_text(profile_text)} "
    return any(f" {n} " in text for n in names if len(n) >= 4)


def _classify(results: dict) -> tuple[str, str]:
    ok = {d for d, r in results.items() if r["outcome"] == Outcome.OK.value}
    failed = {d for d, r in results.items() if r["outcome"] == Outcome.FAILED.value}
    if not ok and not failed:
        return UNAVAILABLE, "Upstox returned nothing for this company"
    if not ok:
        return ERROR, "every request failed: " + "; ".join(
            f"{d} {results[d]['message']}" for d in sorted(failed)
        )[:300]
    if all(d.value in ok for d in CORE) and not failed:
        return REFRESHED, ""
    missing = sorted(d.value for d in DATASETS if d.value not in ok)
    return PARTIAL, "not available / failed: " + ", ".join(missing)


def _due_at(status: str, failures: int, now: datetime) -> datetime:
    if status == ERROR:
        days = ERROR_BACKOFF_DAYS[min(failures, len(ERROR_BACKOFF_DAYS)) - 1]
    else:
        days = FRESH_DAYS[status]
    return now + timedelta(days=days)


def refresh_company(
    session: Session,
    provider: UpstoxFundamentalsProvider,
    company: Company,
    *,
    now: datetime | None = None,
) -> CompanyOutcome:
    now = now or datetime.now(UTC)
    ref = company_ref(session, company)
    state = session.get(ProviderRefreshState, (SOURCE_CODE, company.id))
    if state is None:
        # PENDING until the attempt finishes; due immediately, so a crashed run is picked up again
        state = ProviderRefreshState(
            provider_code=SOURCE_CODE,
            company_id=company.id,
            status="PENDING",
            consecutive_failures=0,
            next_due_at=now,
        )
        session.add(state)
    state.last_attempt_at = now

    def finish(status: str, results: dict, note: str, rows: int = 0) -> CompanyOutcome:
        state.status, state.datasets = status, results
        if status in (REFRESHED, PARTIAL):
            state.last_success_at, state.consecutive_failures = now, 0
        elif status == ERROR:
            state.consecutive_failures = (state.consecutive_failures or 0) + 1
        state.next_due_at = _due_at(status, state.consecutive_failures or 1, now)
        session.commit()
        return CompanyOutcome(company.common_name, status, results, note, rows)

    if not ref.isin:
        return finish(NO_ISIN, {}, "no ISIN: Upstox is keyed by ISIN")

    # identity: does the profile for this ISIN describe this company?
    pres, pbody, _ = provider._get(ref.isin, "profile")
    if pres.ok and isinstance(pbody, dict):
        text = ((pbody.get("data") or {}).get("company_profile")) or ""
        if text and not identity_matches(text, company_names(session, company.id)):
            return finish(
                MAPPING_FAILED,
                {"profile": {"outcome": "REJECTED", "message": text[:120]}},
                "the Upstox profile for this ISIN does not name this company; nothing stored",
            )

    if pres.ok and isinstance(pbody, dict):
        from app.providers import classification  # local: it imports this module

        classification.store(session, company, pbody)  # the provider's sector label, kept dated

    results: dict = {}
    rows = 0
    for dataset in DATASETS:
        r = provider.acquire(session, ref, dataset)
        session.commit()
        results[dataset.value] = {
            "outcome": r.outcome.value,
            "message": r.message[:160],
            "rows": r.rows,
            "latest_period": r.periods[0] if r.periods else None,
        }
        rows += r.rows
    status, note = _classify(results)
    return finish(status, results, note, rows)


def _provider_wide_failure(outcome: CompanyOutcome) -> bool:
    if outcome.status != ERROR:
        return False
    text = " ".join(r["message"] for r in outcome.datasets.values())
    return any(b in text for b in _BLOCKERS)


def due_companies(session: Session, limit: int, now: datetime | None = None) -> list[Company]:
    """NSE-listed companies first, never-tried before due-again, in a stable order."""
    now = now or datetime.now(UTC)
    q = (
        select(Company)
        .join(Security, Security.company_id == Company.id)
        .outerjoin(
            ProviderRefreshState,
            (ProviderRefreshState.company_id == Company.id)
            & (ProviderRefreshState.provider_code == SOURCE_CODE),
        )
        .where(Security.isin.is_not(None))
        .where(
            ProviderRefreshState.company_id.is_(None) | (ProviderRefreshState.next_due_at <= now)
        )
        .group_by(Company.id, ProviderRefreshState.next_due_at)
        .order_by(
            func.bool_or(Security.security_code.is_(None)).desc(),  # NSE listing first
            ProviderRefreshState.next_due_at.asc().nullsfirst(),
            Company.common_name,
        )
        .limit(limit)
    )
    return list(session.scalars(q))


def run_batch(
    session: Session,
    provider: UpstoxFundamentalsProvider,
    companies: list[Company],
    *,
    on_outcome=None,
) -> RefreshRun:
    run = RefreshRun()
    streak = 0
    for company in companies:
        try:
            out = refresh_company(session, provider, company)
        except Exception as exc:  # noqa: BLE001 - one company must not stop the batch
            session.rollback()
            log.exception("Upstox refresh failed for %s", company.common_name)
            out = CompanyOutcome(
                company.common_name, ERROR, note=f"{type(exc).__name__}: {exc}"[:200]
            )
        run.outcomes.append(out)
        if on_outcome:
            on_outcome(out)
        streak = streak + 1 if _provider_wide_failure(out) else 0
        if streak >= BREAKER:
            run.stopped = (
                f"{BREAKER} companies in a row failed with a rate-limit / authorisation error; "
                "stopped to protect the quota. Resume later - progress is saved."
            )
            break
    return run


def summary(session: Session) -> dict:
    rows = dict(
        session.execute(
            select(ProviderRefreshState.status, func.count())
            .where(ProviderRefreshState.provider_code == SOURCE_CODE)
            .group_by(ProviderRefreshState.status)
        ).all()
    )
    universe = (
        session.scalar(
            select(func.count(func.distinct(Security.company_id))).where(Security.isin.is_not(None))
        )
        or 0
    )
    last = session.scalar(
        select(func.max(ProviderRefreshState.last_success_at)).where(
            ProviderRefreshState.provider_code == SOURCE_CODE
        )
    )
    attempted = sum(rows.values())
    return {
        "eligible_companies": universe,
        "processed": attempted,
        "not_yet_processed": max(universe - attempted, 0),
        "refreshed": rows.get(REFRESHED, 0),
        "partially_refreshed": rows.get(PARTIAL, 0),
        "unsupported_or_unavailable": rows.get(UNAVAILABLE, 0),
        "no_isin": rows.get(NO_ISIN, 0),
        "mapping_failures": rows.get(MAPPING_FAILED, 0),
        "provider_errors": rows.get(ERROR, 0),
        "last_successful_refresh": last.isoformat() if last else None,
    }
