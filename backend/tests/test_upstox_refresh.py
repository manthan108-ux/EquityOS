"""Universe-wide Upstox refresh: identity check, classification, incremental runs, circuit breaker."""

# ruff: noqa: F811
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.acquisition.http import FetchResult, FetchStatus
from app.models import Company, ProviderRefreshState
from app.providers import upstox_refresh as ur
from app.providers.base import Dataset, Outcome, ProviderResult
from app.seeds.loader import seed_reference_data, seed_test_universe


class FakeUpstox:
    """Stands in for UpstoxFundamentalsProvider: no network."""

    def __init__(self, profile_text="Reliance Industries Limited is a conglomerate", outcomes=None):
        self.profile_text = profile_text
        self.outcomes = outcomes or {}
        self.acquired: list[tuple[str, str]] = []

    def _get(self, isin, endpoint, query=""):
        body = {"status": "success", "data": {"company_profile": self.profile_text}}
        return FetchResult("u", FetchStatus.OK, 200, b"{}"), body, "u"

    def acquire(self, session, ref, dataset):
        self.acquired.append((ref.common_name, dataset.value))
        outcome, message = self.outcomes.get(dataset, (Outcome.OK, "ok"))
        return ProviderResult(
            "UPSTOX_FUNDAMENTALS",
            dataset,
            outcome,
            message,
            rows=5 if outcome is Outcome.OK else 0,
            periods=["FY2026"],
        )


@pytest.fixture
def companies(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return list(session.scalars(select(Company).order_by(Company.common_name)))


def reliance(companies):
    return next(c for c in companies if c.common_name == "Reliance Industries")


def test_a_company_with_every_dataset_is_refreshed_and_dated(session, companies):
    c, fake = reliance(companies), FakeUpstox()
    out = ur.refresh_company(session, fake, c)
    assert out.status == ur.REFRESHED and out.new_rows == 5 * len(ur.DATASETS)
    state = session.get(ProviderRefreshState, ("UPSTOX_FUNDAMENTALS", c.id))
    assert state.last_success_at and state.next_due_at > datetime.now(UTC) + timedelta(days=29)
    assert state.datasets["INCOME_ANNUAL"]["latest_period"] == "FY2026"
    assert Dataset.COMPETITORS.value not in state.datasets  # known-broken endpoint is not requested


def test_missing_statements_make_it_partial_and_name_what_is_missing(session, companies):
    c = reliance(companies)
    fake = FakeUpstox(
        outcomes={
            Dataset.CASH_FLOW: (Outcome.NO_DATA, "empty"),
            Dataset.KEY_RATIOS: (Outcome.NO_DATA, ""),
        }
    )
    out = ur.refresh_company(session, fake, c)
    assert out.status == ur.PARTIAL and "CASH_FLOW" in out.note and "KEY_RATIOS" in out.note


def test_a_company_upstox_knows_nothing_about_is_unavailable_not_an_error(session, companies):
    c = reliance(companies)
    fake = FakeUpstox(outcomes={d: (Outcome.NO_DATA, "empty") for d in ur.DATASETS})
    assert ur.refresh_company(session, fake, c).status == ur.UNAVAILABLE


def test_a_profile_describing_another_company_stops_everything_for_that_company(session, companies):
    c = reliance(companies)
    fake = FakeUpstox(profile_text="Tata Consultancy Services Ltd is an IT company")
    out = ur.refresh_company(session, fake, c)
    assert (
        out.status == ur.MAPPING_FAILED and fake.acquired == []
    )  # nothing requested, nothing stored


def test_failures_back_off_exponentially_and_are_due_again_only_later(session, companies):
    c = reliance(companies)
    fake = FakeUpstox(outcomes={d: (Outcome.FAILED, "HTTP 500") for d in ur.DATASETS})
    now = datetime.now(UTC)
    ur.refresh_company(session, fake, c, now=now)
    s = session.get(ProviderRefreshState, ("UPSTOX_FUNDAMENTALS", c.id))
    assert (
        s.status == ur.ERROR
        and s.consecutive_failures == 1
        and s.next_due_at == now + timedelta(days=1)
    )
    ur.refresh_company(session, fake, c, now=now)
    assert session.get(
        ProviderRefreshState, ("UPSTOX_FUNDAMENTALS", c.id)
    ).next_due_at == now + timedelta(days=2)


def test_only_due_companies_are_selected_so_unchanged_data_is_not_requested_again(
    session, companies
):
    c = reliance(companies)
    ur.refresh_company(session, FakeUpstox(), c)
    due = {x.id for x in ur.due_companies(session, 100)}
    assert c.id not in due and len(due) == len({x.id for x in companies}) - 1
    later = datetime.now(UTC) + timedelta(days=31)
    assert c.id in {x.id for x in ur.due_companies(session, 100, now=later)}


def test_a_crashed_attempt_is_picked_up_again_not_lost(session, companies):
    c = reliance(companies)
    session.add(
        ProviderRefreshState(
            provider_code="UPSTOX_FUNDAMENTALS",
            company_id=c.id,
            status="PENDING",
            next_due_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    session.commit()
    assert c.id in {x.id for x in ur.due_companies(session, 100)}


def test_repeated_rate_limit_errors_stop_the_run_and_progress_is_kept(session, companies):
    fake = FakeUpstox(
        profile_text=" ".join(c.legal_name for c in companies),
        outcomes={d: (Outcome.FAILED, "RATE_LIMITED") for d in ur.DATASETS},
    )
    run = ur.run_batch(session, fake, companies)
    assert run.stopped and len(run.outcomes) == ur.BREAKER < len(companies)
    assert session.scalar(select(ProviderRefreshState).limit(1)) is not None  # resumable


def test_one_companys_crash_does_not_stop_the_batch(session, companies):
    class Boom(FakeUpstox):
        def acquire(self, session, ref, dataset):
            if ref.common_name == companies[0].common_name:
                raise RuntimeError("boom")
            return super().acquire(session, ref, dataset)

    run = ur.run_batch(
        session, Boom(profile_text=" ".join(c.legal_name for c in companies)), companies[:3]
    )
    assert not run.stopped and len(run.outcomes) == 3 and run.outcomes[0].status == ur.ERROR


def test_the_summary_reports_every_category(session, companies):
    ur.refresh_company(session, FakeUpstox(), reliance(companies))
    s = ur.summary(session)
    assert (
        s["refreshed"] == 1
        and s["processed"] == 1
        and s["not_yet_processed"] == s["eligible_companies"] - 1
    )
    assert s["last_successful_refresh"] and {
        "mapping_failures",
        "provider_errors",
        "partially_refreshed",
    } <= set(s)
