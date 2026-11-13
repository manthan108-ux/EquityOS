"""Provider industry labels: parsing, identity check, resumable backfill, refresh hook."""

# ruff: noqa: F811
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.acquisition.http import FetchResult, FetchStatus
from app.models import Company, CompanyClassification
from app.providers import classification as cl
from app.providers import upstox_refresh as ur
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.test_upstox_refresh import FakeUpstox

PROFILE = {
    "status": "success",
    "data": {
        "company_profile": "Reliance Industries Limited is engaged in refining",
        "sector": "Refineries",
        "sector_market_cap_inr": {"value": 1805375.49, "unit": "crore", "formatted": "x"},
    },
}


class Provider:
    """Stands in for the Upstox provider's profile endpoint."""

    def __init__(self, bodies=None, status=FetchStatus.OK):
        self.bodies, self.status, self.calls = bodies or {}, status, []

    def _get(self, isin, endpoint, query=""):
        self.calls.append(isin)
        ok = self.status is FetchStatus.OK
        return FetchResult("u", self.status, 200 if ok else 429), self.bodies.get(isin), "u"


@pytest.fixture
def cos(session):
    seed_reference_data(session)
    seed_test_universe(session)
    session.commit()
    return {c.common_name: c for c in session.scalars(select(Company))}


def all_profiles(cos, session, sector="Refineries"):
    from app.models import Security

    out = {}
    for c in cos.values():
        isin = session.scalar(select(Security.isin).where(Security.company_id == c.id).limit(1))
        out[isin] = {
            "data": {
                "company_profile": f"{c.legal_name} is a company.",
                "sector": sector,
                "sector_market_cap_inr": {"value": 100.5, "unit": "crore"},
            }
        }
    return out


def test_a_profile_yields_the_label_and_the_group_market_cap():
    assert cl.parse_profile(PROFILE) == ("Refineries", Decimal("1805375.49"))


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"data": {}},
        {"data": {"sector": ""}},
        {"data": {"sector": "12345"}},
        {"data": {"sector": "x" * 500}},
        {"data": {"sector": 7}},
        {"data": "oops"},
    ],
)
def test_missing_or_odd_sectors_are_not_stored(body):
    assert cl.parse_profile(body) is None


def test_an_unusable_market_cap_is_dropped_but_the_label_kept():
    body = {
        "data": {"sector": "Cement", "sector_market_cap_inr": {"value": "abc", "unit": "crore"}}
    }
    assert cl.parse_profile(body) == ("Cement", None)
    body["data"]["sector_market_cap_inr"] = {"value": 5, "unit": "billion"}  # not crore: ignored
    assert cl.parse_profile(body) == ("Cement", None)


def test_storing_twice_updates_one_row_with_source_and_date(session, cos):
    c = cos["Reliance Industries"]
    cl.store(session, c, PROFILE)
    cl.store(session, c, {"data": {"sector": "Oil Exploration"}})
    session.commit()
    rows = session.scalars(select(CompanyClassification)).all()
    assert len(rows) == 1 and rows[0].label == "Oil Exploration" and rows[0].retrieved_at
    assert "Upstox" in rows[0].source and rows[0].group_market_cap_crore is None


def test_backfill_stores_labels_and_skips_what_it_already_has(session, cos):
    provider = Provider(all_profiles(cos, session))
    first = cl.backfill(session, provider, cl.missing(session, 10))
    assert first.stored == len(cos) and not first.skipped
    assert cl.missing(session, 10) == []  # nothing left: the run is resumable and idempotent
    calls = len(provider.calls)
    cl.backfill(session, provider, cl.missing(session, 10))
    assert len(provider.calls) == calls  # no repeated requests


def test_a_profile_that_names_another_company_is_not_stored(session, cos):
    bodies = all_profiles(cos, session)
    for b in bodies.values():
        b["data"]["company_profile"] = "Some Totally Different Corporation makes widgets"
    run = cl.backfill(session, Provider(bodies), cl.missing(session, 10))
    assert run.stored == 0 and run.skipped == {"profile does not name this company": len(cos)}
    assert not session.scalars(select(CompanyClassification)).all()


def test_repeated_refusals_stop_the_run_and_keep_progress(session, cos):
    provider = Provider(status=FetchStatus.RATE_LIMITED)
    run = cl.backfill(session, provider, cl.missing(session, 10))
    assert run.stopped and len(provider.calls) == ur.BREAKER and run.stored == 0


def test_the_summary_counts_groups(session, cos):
    cl.backfill(session, Provider(all_profiles(cos, session)), cl.missing(session, 10))
    s = cl.summary(session)
    assert s["classified"] == len(cos) and s["distinct_labels"] == 1
    assert s["largest_groups"][0] == {"label": "Refineries", "companies": len(cos)}


def test_the_fundamentals_refresh_records_the_label_it_already_fetched(session, cos):
    reliance = cos["Reliance Industries"]
    fake = FakeUpstox()

    def get(isin, endpoint, query=""):
        return FetchResult("u", FetchStatus.OK, 200, b"{}"), PROFILE, "u"

    fake._get = get
    out = ur.refresh_company(session, fake, reliance)
    assert out.status == ur.REFRESHED
    row = session.get(CompanyClassification, (reliance.id, "UPSTOX_SECTOR"))
    assert row.label == "Refineries" and row.group_market_cap_crore == Decimal("1805375.49")


def test_one_failed_store_does_not_stop_the_batch(session, cos, monkeypatch):
    real = cl.store
    calls = []

    def flaky(sess, company, body, retrieved_at=None):
        calls.append(company.common_name)
        if len(calls) == 1:
            raise RuntimeError("database hiccup")
        return real(sess, company, body, retrieved_at)

    monkeypatch.setattr(cl, "store", flaky)
    run = cl.backfill(session, Provider(all_profiles(cos, session)), cl.missing(session, 10))
    assert run.stored == len(cos) - 1 and run.skipped == {"error RuntimeError": 1}
    assert len(cl.missing(session, 10)) == 1  # the one that failed is picked up on the next run
