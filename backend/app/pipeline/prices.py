"""Keep end-of-day market prices current as part of the pipeline.

Prices come from the exchange's official daily file (one per trading day for every security), so
refreshing them is a run-level step, not a per-company one: the days since the newest stored price
are fetched through the shared polite client, and each day is stored by the existing, idempotent
`store_prices` (a stored price is never overwritten). A day with no file (a market holiday, or a
file not published yet) is not an error. Everything price-based (valuation, technical indicators,
the verdict) then picks the new prices up through the `calculate` stage, whose fingerprint
includes the company's latest price date.

Never more than `MAX_DAYS` days are fetched in one go, so a long gap is closed over a few runs.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EodPrice, FetchStatus, Security

log = logging.getLogger("equityos.pipeline")
MAX_DAYS = 20
NO_FILE = (FetchStatus.NOT_FOUND, FetchStatus.INVALID_CONTENT)  # a holiday, or not published
STALE_AFTER_DAYS = 5  # calendar days without a new price before an alert is raised


@dataclass
class PriceRefresh:
    latest_before: date | None
    latest_after: date | None
    days_stored: list[date] = field(default_factory=list)
    rows_stored: int = 0
    no_file: list[date] = field(default_factory=list)  # holidays, or not published yet
    failed: dict[str, str] = field(default_factory=dict)  # day -> why
    skipped: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.days_stored)

    def as_dict(self) -> dict:
        return {
            "latest_before": str(self.latest_before) if self.latest_before else None,
            "latest_after": str(self.latest_after) if self.latest_after else None,
            "days_stored": [str(d) for d in self.days_stored],
            "rows_stored": self.rows_stored,
            "no_file": [str(d) for d in self.no_file],
            "failed": self.failed,
            "skipped": self.skipped,
        }


def latest_price_date(session: Session, company_id=None) -> date | None:
    query = select(func.max(EodPrice.trade_date))
    if company_id is not None:
        query = query.join(Security, Security.id == EodPrice.security_id).where(
            Security.company_id == company_id
        )
    return session.scalar(query)


def weekdays_between(after: date, through: date) -> list[date]:
    out, day = [], after + timedelta(days=1)
    while day <= through:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def last_expected_trading_day(today: date) -> date:
    """The newest weekday strictly before today (today's file is not out until evening)."""
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def is_stale(latest: date | None, today: date) -> bool:
    return latest is None or latest < last_expected_trading_day(today)


def refresh_prices(
    session: Session,
    client,
    *,
    today: date | None = None,
    max_days: int = MAX_DAYS,
    exchange: str = "NSE",
) -> PriceRefresh:
    """Fetch and store the official end-of-day file for each missing weekday. Never raises for a
    network or file problem: those are recorded in the result."""
    from app.providers.exchange_files import BhavcopyProvider, store_prices

    today = today or datetime.now(UTC).date()
    latest = latest_price_date(session)
    result = PriceRefresh(latest, latest)
    if latest is None:
        result.skipped = "no prices are stored yet; load history with the acquisition command first"
        return result
    if not is_stale(latest, today):
        result.skipped = f"prices are current (newest {latest})"
        return result
    provider = BhavcopyProvider(client, exchange)
    days = weekdays_between(latest, last_expected_trading_day(today))[:max_days]
    for day in days:
        fetch = provider.fetch(day)
        if fetch.status in NO_FILE:
            result.no_file.append(day)
            continue
        if not fetch.ok:
            result.failed[str(day)] = f"{fetch.status.value}: {fetch.error or 'no detail'}"[:160]
            break  # the site is refusing or down: stop, try again next run
        try:
            report = store_prices(session, exchange, provider.parse(fetch), fetch)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal
            session.rollback()
            result.failed[str(day)] = f"{type(exc).__name__}: {exc}"[:160]
            log.exception("could not store prices for %s", day)
            break
        if report.stored:
            result.days_stored.append(day)
            result.rows_stored += report.stored
        else:
            result.no_file.append(day)
    result.latest_after = latest_price_date(session)
    return result
