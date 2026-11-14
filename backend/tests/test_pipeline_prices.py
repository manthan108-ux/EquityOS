# ruff: noqa: F811
"""Market-price refresh inside the pipeline: catch-up, holidays, refusals, runs, calculation, alerts."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from app.acquisition.http import FetchResult
from app.models import EodPrice, FetchStatus, PipelineEvent, PipelineRun
from app.pipeline import metrics, prices, runner
from app.providers.exchange_files import (
    BhavcopyProvider,
    parse_nse_bhavcopy,
    store_prices,
)
from tests.test_evidence_fetch import FakeClient
from tests.test_providers import (
    NSE_BHAV,
    fetch_result,
    seeded,  # noqa: F401  (reference data and the test universe)
)

HEADER = NSE_BHAV.splitlines()[0]
TODAY = date(2026, 11, 14)  # a Saturday: the newest expected file is Friday 13th


def bhav(day: date, close: str = "1330.00") -> bytes:
    d = f"{day.day:02d}-{day:%b}-{day.year}"
    row = f"RELIANCE, EQ, {d}, 1313.40, 1295.00, 1340.00, 1295.00, {close}, {close}, 1311.70, 100, 5.0, 10, 50, 50.00"
    return f"{HEADER}\n{row}\n".encode()


def url(day: date) -> str:
    return BhavcopyProvider(None, "NSE").url(day)


@pytest.fixture
def stale(seeded):
    """Prices up to Friday 6 November are stored, as in the live database."""
    store_prices(seeded, "NSE", parse_nse_bhavcopy(NSE_BHAV), fetch_result())
    seeded.commit()
    return seeded


def served(*days: date, **special) -> FakeClient:
    pages = {url(d): bhav(d) for d in days}
    pages.update({url(d): v for d, v in special.items()} if special else {})
    return FakeClient(pages)


def stored_days(session):
    return sorted(session.scalars(select(EodPrice.trade_date)).all())


# ---- the calendar ---------------------------------------------------------------------------------


def test_the_newest_expected_file_is_the_last_weekday_before_today():
    assert prices.last_expected_trading_day(date(2026, 11, 14)) == date(2026, 11, 13)  # Saturday
    assert prices.last_expected_trading_day(date(2026, 11, 16)) == date(2026, 11, 13)  # Monday
    assert prices.last_expected_trading_day(date(2026, 11, 17)) == date(2026, 11, 16)


def test_weekdays_between_skips_weekends():
    assert prices.weekdays_between(date(2026, 11, 6), date(2026, 11, 13)) == [
        date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)
    ]  # fmt: skip


def test_staleness():
    assert prices.is_stale(None, TODAY)
    assert prices.is_stale(date(2026, 11, 6), TODAY)
    assert not prices.is_stale(date(2026, 11, 13), TODAY)


# ---- catching up -----------------------------------------------------------------------------------


def test_missing_trading_days_are_fetched_and_stored(stale):
    days = [date(2026, 11, d) for d in (9, 10, 11, 12, 13)]
    result = prices.refresh_prices(stale, served(*days), today=TODAY)
    assert result.days_stored == days and result.rows_stored == 5
    assert (result.latest_before, result.latest_after) == (date(2026, 11, 6), date(2026, 11, 13))
    assert result.changed and not result.failed and not result.no_file
    assert stored_days(stale)[-1] == date(2026, 11, 13)


def test_a_holiday_with_no_file_is_not_an_error(stale):
    holiday = date(2026, 11, 12)
    days = [date(2026, 11, d) for d in (9, 10, 11, 13)]
    result = prices.refresh_prices(stale, served(*days), today=TODAY)
    assert result.no_file == [holiday] and not result.failed
    assert result.latest_after == date(2026, 11, 13)


def test_current_prices_fetch_nothing(stale):
    prices.refresh_prices(
        stale, served(*[date(2026, 11, d) for d in (9, 10, 11, 12, 13)]), today=TODAY
    )
    client = FakeClient({})
    again = prices.refresh_prices(stale, client, today=TODAY)
    assert again.skipped and "current" in again.skipped and client.requests == []


def test_a_refusing_site_stops_the_catch_up_and_keeps_what_was_stored(stale):
    denied = FetchResult(
        url(date(2026, 11, 10)), FetchStatus.ACCESS_DENIED, 403, error="HTTP 403: access denied"
    )
    client = served(date(2026, 11, 9), date(2026, 11, 11))
    client.pages[url(date(2026, 11, 10))] = denied
    result = prices.refresh_prices(stale, client, today=TODAY)
    assert result.days_stored == [date(2026, 11, 9)]
    assert list(result.failed) == ["2026-11-10"] and "403" in result.failed["2026-11-10"]
    assert url(date(2026, 11, 11)) not in client.requests, "it stops; it does not work around"
    assert stored_days(stale)[-1] == date(2026, 11, 9)


def test_a_long_gap_is_closed_over_several_runs(stale):
    all_days = prices.weekdays_between(date(2026, 11, 6), date(2026, 11, 13))
    result = prices.refresh_prices(stale, served(*all_days), today=TODAY, max_days=2)
    assert len(result.days_stored) == 2 and result.latest_after == date(2026, 11, 10)
    rest = prices.refresh_prices(stale, served(*all_days), today=TODAY, max_days=20)
    assert rest.latest_after == date(2026, 11, 13)


def test_stored_prices_are_never_overwritten(stale):
    day = date(2026, 11, 9)
    prices.refresh_prices(
        stale, FakeClient({url(day): bhav(day, "1300.00")}), today=date(2026, 11, 10)
    )
    first = stale.scalar(select(EodPrice.close).where(EodPrice.trade_date == day))
    # the same day offered again with another close: the first value is kept
    report = store_prices(
        stale, "NSE", parse_nse_bhavcopy(bhav(day, "999.00").decode()), fetch_result()
    )
    assert report.stored == 0 and report.already_stored == 1
    assert stale.scalar(select(EodPrice.close).where(EodPrice.trade_date == day)) == first


def test_an_empty_database_is_not_filled_by_a_refresh(seeded):
    result = prices.refresh_prices(seeded, FakeClient({}), today=TODAY)
    assert result.skipped and "no prices are stored yet" in result.skipped


# ---- inside a run -----------------------------------------------------------------------------------


def run_with(session, client, **options):
    run = runner.create_run(session, stages=("readiness",), batch_size=1, options=options)
    run.companies = []
    session.commit()
    runner.claim_run(session, run.id)
    real_close = session.close
    session.close = lambda: None
    try:
        done = runner.execute(lambda: session, run.id, client=client, close_sessions=False)
    finally:
        session.close = real_close
    return run, done


def test_a_run_refreshes_prices_first_and_says_so(stale, monkeypatch):
    monkeypatch.setattr(
        prices,
        "datetime",
        type("D", (), {"now": staticmethod(lambda tz=None: datetime(2026, 11, 14, 9, tzinfo=UTC))}),
    )
    days = prices.weekdays_between(date(2026, 11, 6), date(2026, 11, 13))
    run, done = run_with(stale, served(*days))
    assert done.summary["price_refresh"]["latest_after"] == "2026-11-13"
    assert len(done.summary["price_refresh"]["days_stored"]) == 5
    messages = [
        e.message
        for e in stale.scalars(select(PipelineEvent).where(PipelineEvent.run_id == run.id))
    ]
    assert any(m.startswith("price refresh: 5 day(s) stored") for m in messages)


def test_a_run_can_skip_the_price_refresh_and_never_fails_because_of_it(stale, monkeypatch):
    client = FakeClient({})
    _, done = run_with(stale, client, refresh_prices=False)
    assert "price_refresh" not in done.summary and client.requests == []

    def boom(*a, **k):
        raise RuntimeError("the exchange site exploded")

    monkeypatch.setattr(prices, "refresh_prices", boom)
    run, done = run_with(stale, client)
    assert (
        stale.get(PipelineRun, run.id).status == "COMPLETED" and "price_refresh" not in done.summary
    )


# ---- the calculation notices new prices ------------------------------------------------------------------


def test_a_new_price_makes_the_calculation_run_again_and_only_then(seeded, monkeypatch):
    from app.calc import engine as calc_engine
    from tests.research_helpers import complete_company
    from tests.test_pipeline_stages import IT, go

    co = complete_company(seeded, "Priced Co", *IT)
    calls = []
    real = calc_engine.calculate_company
    monkeypatch.setattr(
        calc_engine, "calculate_company", lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    )
    for _ in range(3):
        go(seeded, co, ("evidence", "calculate"))
    settled = len(calls)
    for _ in range(3):
        go(seeded, co, ("evidence", "calculate"))
    assert len(calls) == settled, "unchanged prices: no recalculation"
    # a security of this company gets a new end-of-day price
    from app.models import Exchange, Security

    nse = seeded.scalar(select(Exchange).where(Exchange.code == "NSE"))
    sec = Security(company_id=co.id, exchange_id=nse.id, symbol="PRICEDCO", isin="INE000PRC001")
    seeded.add(sec)
    seeded.flush()
    seeded.add(
        EodPrice(
            security_id=sec.id, trade_date=date(2026, 11, 13), series="EQ",
            open=1, high=2, low=1, close=2, last=2, prev_close=1, volume=1,
            source_url="https://example.test/f.csv", file_sha256="0" * 64,
            retrieved_at=datetime(2026, 11, 14, tzinfo=UTC),
        )
    )  # fmt: skip
    seeded.commit()
    go(seeded, co, ("evidence", "calculate"))
    assert len(calls) == settled + 1, "a new price recalculates once"
    go(seeded, co, ("evidence", "calculate"))
    assert len(calls) == settled + 1


# ---- monitoring ---------------------------------------------------------------------------------------------


def test_old_prices_raise_an_alert(stale):
    now = datetime(2026, 11, 14, 9, tzinfo=UTC)
    report = metrics.health(stale, now=now)
    stale_alert = [a for a in report["alerts"] if a["code"] == "PRICES_STALE"]
    assert stale_alert and "06/11/2026" in stale_alert[0]["message"]
    prices.refresh_prices(
        stale, served(*prices.weekdays_between(date(2026, 11, 6), date(2026, 11, 13))), today=TODAY
    )
    assert "PRICES_STALE" not in {a["code"] for a in metrics.health(stale, now=now)["alerts"]}


def test_the_row_count_after_a_refresh_matches_the_days(stale):
    days = prices.weekdays_between(date(2026, 11, 6), date(2026, 11, 13))
    before = stale.scalar(select(func.count()).select_from(EodPrice))
    prices.refresh_prices(stale, served(*days), today=TODAY)
    assert stale.scalar(select(func.count()).select_from(EodPrice)) == before + len(days)
