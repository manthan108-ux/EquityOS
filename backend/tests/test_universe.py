"""The universe batch: ordering, isolated steps, skipping fresh work, recovery after a failure."""

# ruff: noqa: F811
import pytest
from sqlalchemy import select

from app.models import (
    Company,
    CompanyEnrichment,
    CompanyWebsiteRegistry,
    ProviderRefreshState,
)
from app.providers import universe
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.test_enrich import GOOD, LEADERS, Router
from tests.test_upstox_refresh import FakeUpstox
from tests.test_website_discovery import FakeClient, page

HOME = (
    "<html><head><title>Reliance Industries Limited</title></head><body>"
    "<p>Reliance Industries Limited is a conglomerate.</p>"
    '<a href="/about-us">About us</a>'
    + "<p>Filler text for the group. </p>" * 12
    + "</body></html>"
)
SITE = "https://www.relianceindustries.com"
PAGES = {SITE: HOME, SITE + "/about-us": LEADERS}


ALL_NAMES = "Reliance Industries HDFC Bank ICICI Bank Infosys TCS Tata Consultancy Services Limited"


class Upstox(FakeUpstox):
    def __init__(self, **kw):
        super().__init__(profile_text=ALL_NAMES, **kw)

    def is_configured(self):
        return True


class Unconfigured(Upstox):
    def is_configured(self):
        return False


@pytest.fixture
def cos(session):
    seed_reference_data(session)
    seed_test_universe(session)
    for c in session.scalars(select(Company)):
        c.website = c.investor_relations_url = None
    session.commit()
    return {c.common_name: c for c in session.scalars(select(Company))}


def run(session, company, **kw):
    kw.setdefault("steps", universe.STEPS)
    kw.setdefault("client", FakeClient(dict(PAGES)))
    kw.setdefault("llm", Router(GOOD))
    kw.setdefault("upstox", Upstox())
    return universe.run_company(session, company, **kw)


# ---- ordering ---------------------------------------------------------------------------------------


def test_companies_without_a_registered_website_come_first(session, cos):
    cos["TCS"].website = "https://www.tcs.com"
    session.add(CompanyWebsiteRegistry(company_id=cos["TCS"].id, status="MANUAL"))
    session.commit()
    names = [c.common_name for c in universe.pick_batch(session, 10)]
    assert len(names) == len(cos) and names[-1] == "TCS"
    assert names[:-1] == sorted(names[:-1])  # stable name order inside each group


def test_failed_websites_in_back_off_do_not_block_untried_companies(session, cos):
    """Regression: the same failed companies were returned first on every run."""
    from datetime import UTC, datetime, timedelta

    names = sorted(cos)
    for n in names[:2]:  # failed just now, retry in a week, but Upstox data already stored
        session.add(
            CompanyWebsiteRegistry(
                company_id=cos[n].id,
                status="FAILED",
                next_retry_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        session.add(
            ProviderRefreshState(
                provider_code="UPSTOX_FUNDAMENTALS", company_id=cos[n].id, status="REFRESHED"
            )
        )
    session.commit()
    picked = [c.common_name for c in universe.pick_batch(session, 10)]
    assert picked[:3] == names[2:] and picked[3:] == names[:2]
    # once the window opens they return - after the never-tried companies, before settled ones
    later = datetime.now(UTC) + timedelta(days=8)
    assert [c.common_name for c in universe.pick_batch(session, 10, now=later)] == names[
        2:
    ] + names[:2]


def test_the_batch_size_is_respected_and_isinless_companies_are_not_picked(session, cos):
    assert len(universe.pick_batch(session, 2)) == 2
    session.add(
        Company(legal_name="No Isin Limited", common_name="No Isin", master_data_source="TEST")
    )
    session.commit()
    assert "No Isin" not in [c.common_name for c in universe.pick_batch(session, 20)]


# ---- one company, every step -------------------------------------------------------------------------


def test_every_step_runs_and_its_result_is_persisted(session, cos):
    co = cos["Reliance Industries"]
    client, upstox = FakeClient(dict(PAGES)), Upstox()
    out = run(session, co, client=client, upstox=upstox)
    assert list(out.steps) == list(universe.STEPS)
    assert out.steps["website"].startswith("VERIFIED")
    assert co.website == SITE
    assert out.steps["upstox"].startswith("REFRESHED")
    assert session.get(ProviderRefreshState, ("UPSTOX_FUNDAMENTALS", co.id)).status == "REFRESHED"
    assert out.steps["documents"].split(" ")[0]  # a fetch report status, not an error
    assert out.steps["enrich"].startswith("stored")
    assert session.scalars(select(CompanyEnrichment)).all()
    assert out.steps["evidence"] == "validated"
    assert session.get(CompanyWebsiteRegistry, co.id).status == "VERIFIED"
    assert not any(v.startswith("error") for v in out.steps.values())


def test_only_the_requested_steps_run(session, cos):
    out = run(session, cos["Reliance Industries"], steps=("website",))
    assert list(out.steps) == ["website"]
    assert not session.scalars(select(ProviderRefreshState)).all()


# ---- isolation --------------------------------------------------------------------------------------


def test_a_failing_step_is_recorded_and_the_rest_still_run(session, cos, monkeypatch):
    from app.acquisition import website_discovery as wd

    def broken(*a, **k):
        raise RuntimeError("discovery crashed")

    monkeypatch.setattr(wd, "discover", broken)
    co = cos["Reliance Industries"]
    out = run(session, co)
    assert out.steps["website"] == "error: RuntimeError"
    assert out.steps["upstox"].startswith("REFRESHED")  # later steps carried on
    assert out.steps["documents"].startswith("skipped (no verified website)")
    assert out.steps["evidence"] == "validated"
    assert session.get(ProviderRefreshState, ("UPSTOX_FUNDAMENTALS", co.id)).status == "REFRESHED"


def test_a_database_error_in_one_step_does_not_poison_the_session(session, cos, monkeypatch):
    from app.research import enrich

    def bad(session_, *a, **k):
        session_.add(CompanyWebsiteRegistry(company_id=cos["TCS"].id, status="x" * 500))
        session_.flush()

    monkeypatch.setattr(enrich, "enrich_company", bad)
    co = cos["Reliance Industries"]
    out = run(session, co)
    assert out.steps["enrich"].startswith("error")
    assert out.steps["evidence"] == "validated"  # the session was rolled back and still works


# ---- skipping ---------------------------------------------------------------------------------------


def test_missing_prerequisites_skip_steps_with_a_reason(session, cos):
    out = run(
        session, cos["Reliance Industries"], upstox=Unconfigured(), llm=None, client=FakeClient({})
    )
    assert "Upstox not configured" in out.steps["upstox"]
    assert "no verified website" in out.steps["documents"]
    assert "no website or no language model" in out.steps["enrich"]
    assert out.steps["website"].split(" ")[0] in {"FAILED", "NO_CANDIDATE", "AMBIGUOUS"}
    session.get(CompanyWebsiteRegistry, cos["Reliance Industries"].id)
    assert cos["Reliance Industries"].website is None  # nothing unverified is ever registered


def test_a_second_run_repeats_no_work(session, cos):
    co = cos["Reliance Industries"]
    upstox, llm = Upstox(), Router(GOOD)
    run(session, co, upstox=upstox, llm=llm)
    acquired, llm_calls = len(upstox.acquired), len(llm.calls)
    second = run(session, co, upstox=upstox, llm=llm, client=FakeClient(dict(PAGES)))
    assert len(upstox.acquired) == acquired  # Upstox not asked again
    assert second.steps["upstox"].startswith("skipped (fresh until")
    assert len(llm.calls) == llm_calls  # enrichment is not repeated within 30 days
    assert "already registered" in second.steps["website"]
    assert second.steps["documents"].split(" ")[0]  # fetch's own cool-down decides


# ---- the batch --------------------------------------------------------------------------------------


def test_a_batch_reports_each_company_and_reaches_all_of_them(session, cos):
    seen = []
    rep = universe.run_batch(
        session,
        list(cos.values())[:3],
        steps=("upstox", "evidence"),
        client=FakeClient({}),
        upstox=Upstox(),
        on_company=seen.append,
    )
    assert len(rep.companies) == 3 == len(seen)
    assert {c.steps["upstox"].split(" ")[0] for c in rep.companies} == {"REFRESHED"}


def test_a_stopped_batch_resumes_where_it_stopped(session, cos, monkeypatch):
    """The second run skips what the first finished and completes what it did not."""
    from app.acquisition import website_discovery as wd

    real = wd.discover
    victim = sorted(cos)[1]

    def flaky(session_, company, *a, **k):
        if company.common_name == victim:
            raise ConnectionError("network dropped")
        return real(session_, company, *a, **k)

    upstox = Upstox()
    monkeypatch.setattr(wd, "discover", flaky)
    companies = [cos[n] for n in sorted(cos)[:3]]
    first = universe.run_batch(
        session, companies, steps=("website", "upstox"), client=FakeClient({}), upstox=upstox
    )
    assert first.companies[1].steps["website"] == "error: ConnectionError"
    done_first = len(upstox.acquired)
    monkeypatch.setattr(wd, "discover", real)
    second = universe.run_batch(
        session, companies, steps=("website", "upstox"), client=FakeClient({}), upstox=upstox
    )
    assert not second.companies[1].steps["website"].startswith("error")
    assert len(upstox.acquired) == done_first  # Upstox work from run one was kept, not redone
    assert all(c.steps["upstox"].startswith("skipped") for c in second.companies)


def test_unknown_state_after_a_crash_leaves_no_half_written_rows(session, cos):
    class Boom(Upstox):
        def acquire(self, session, ref, dataset):
            raise RuntimeError("provider exploded")

    co = cos["TCS"]
    out = run(session, co, upstox=Boom(), steps=("upstox", "evidence"))
    assert out.steps["upstox"] == "error: RuntimeError"
    assert session.get(ProviderRefreshState, ("UPSTOX_FUNDAMENTALS", co.id)) is None
    assert out.steps["evidence"] == "validated"


# ---- coverage ---------------------------------------------------------------------------------------


def test_coverage_is_read_from_stored_state_without_network(session, cos):
    run(session, cos["Reliance Industries"])
    cov = universe.coverage(session)
    assert cov["websites"] and cov["upstox"]["refreshed"] == 1
    assert cov["companies_with_verified_external_quotes"] == 1
    assert isinstance(cov["document_fetch_runs_by_status"], dict)
    assert page("x")  # helper import kept for fixtures above
