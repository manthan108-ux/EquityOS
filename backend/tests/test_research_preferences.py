"""Research preferences: basis, reporting period, optional items (Phase 11)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api import research as research_api
from app.core.config import get_settings
from app.core.database import get_session
from app.main import create_app
from app.research import clock, orchestrator, security
from app.research import preferences as pf
from app.research import readiness as rd
from app.research import report as rp
from app.research import requirements as rq
from tests.research_helpers import (
    FY25,
    FY26,
    PRICE_DAY,  # noqa: F401
    QUARTER,
    TODAY,
    complete_company,
    evidence,
)
from tests.test_sector import mk_company, seeded  # noqa: F401

IT = ("it_services", "it_services_and_consulting")
BANK = ("banking", "private_sector_bank")
Q1_FY27 = date(2026, 6, 30)


# ---- the preference object ------------------------------------------------------------------------


def test_the_defaults_are_consolidated_latest_quarter_and_no_optional_items():
    d = pf.DEFAULT
    assert (d.basis, d.period_mode, d.include_optional) == ("CONSOLIDATED", "LATEST_QUARTER", False)
    assert d.fiscal_year is None and d.target_end() is None and not d.strict_period


def test_chosen_periods_map_to_their_period_end():
    assert pf.Preferences(period_mode="FISCAL_YEAR", fiscal_year=2026).target_end() == FY26
    quarters = {"Q1": (6, 30, 2026), "Q2": (9, 30, 2026), "Q3": (12, 31, 2026), "Q4": (3, 31, 2027)}
    for q, (m, d, y) in quarters.items():
        got = pf.Preferences(period_mode="QUARTER", fiscal_year=2027, quarter=q).target_end()
        assert got == date(y, m, d)


@pytest.mark.parametrize(
    "bad",
    [
        {"basis": "GROUP"},
        {"period_mode": "WHENEVER"},
        {"period_mode": "FISCAL_YEAR"},  # a year is needed
        {"period_mode": "QUARTER", "fiscal_year": 2027},  # a quarter is needed
        {"period_mode": "QUARTER", "fiscal_year": 2027, "quarter": "Q5"},
        {"period_mode": "FISCAL_YEAR", "fiscal_year": 1800},
        {"period_mode": "LATEST_QUARTER", "fiscal_year": 2027},
        {"surprise": 1},
        {"include_optional": "yes"},
        {"period_mode": "FISCAL_YEAR", "fiscal_year": "abc"},
    ],
)
def test_invalid_preferences_are_refused(bad):
    with pytest.raises(pf.PreferenceError):
        pf.Preferences.from_dict(bad)


def test_preferences_round_trip_through_json():
    p = pf.Preferences(basis="STANDALONE", period_mode="QUARTER", fiscal_year=2027, quarter="Q2")
    assert pf.Preferences.from_dict(p.to_dict()) == p
    assert pf.Preferences.from_dict(None) == pf.DEFAULT


# ---- period selection -----------------------------------------------------------------------------


def two_periods(session, name="Two periods"):
    """Revenue for FY2026 (annual) and for Q2 FY2027 and Q1 FY2027 (quarters), consolidated."""
    c = complete_company(session, name, *IT, skip=("revenue", "latest_quarter"))
    evidence(session, c, "INCOME.revenue_from_operations", 1000, end=FY26, ptype="ANNUAL")
    evidence(session, c, "INCOME.revenue_from_operations", 250, end=QUARTER, ptype="QUARTERLY")
    evidence(session, c, "INCOME.revenue_from_operations", 240, end=Q1_FY27, ptype="QUARTERLY")
    return c


def revenue(session, c, **kw):
    return rd.evaluate(session, c, today=TODAY, prefs=pf.Preferences(**kw)).by_id("revenue")


def test_latest_quarter_is_the_default_and_picks_the_newest_quarter(seeded):  # noqa: F811
    c = two_periods(seeded)
    s = rd.evaluate(seeded, c, today=TODAY).by_id("revenue")
    assert s.period_type == "QUARTERLY" and s.period_end == QUARTER and s.period_note is None


def test_latest_annual_picks_the_year(seeded):  # noqa: F811
    s = revenue(seeded, two_periods(seeded), period_mode="LATEST_ANNUAL")
    assert s.period_type == "ANNUAL" and s.period_end == FY26


def test_a_chosen_quarter_is_exact(seeded):  # noqa: F811
    c = two_periods(seeded)
    q1 = revenue(seeded, c, period_mode="QUARTER", fiscal_year=2027, quarter="Q1")
    assert q1.period_end == Q1_FY27 and str(q1.value) == "240.000000"
    q3 = revenue(seeded, c, period_mode="QUARTER", fiscal_year=2027, quarter="Q3")
    assert q3.status == rd.MISSING and "nothing is stored for the period you chose" in q3.reason
    assert q3.value is None  # the newer or older quarter is not substituted


def test_a_chosen_year_is_exact_and_never_replaced_by_another(seeded):  # noqa: F811
    c = two_periods(seeded)
    fy26 = revenue(seeded, c, period_mode="FISCAL_YEAR", fiscal_year=2026)
    assert fy26.period_type == "ANNUAL" and fy26.period_end == FY26
    fy25 = revenue(seeded, c, period_mode="FISCAL_YEAR", fiscal_year=2025)
    assert fy25.status == rd.MISSING and "FY2025" in fy25.reason


def test_a_historical_selection_is_not_judged_stale(seeded):  # noqa: F811
    c = complete_company(seeded, "History", *IT, skip=("revenue",))
    evidence(seeded, c, "INCOME.revenue_from_operations", 700, end=date(2023, 3, 31))
    assert revenue(seeded, c, period_mode="LATEST_ANNUAL").status != rd.AVAILABLE  # too old now
    old = revenue(seeded, c, period_mode="FISCAL_YEAR", fiscal_year=2023)
    assert old.status == rd.AVAILABLE and old.period_end == date(2023, 3, 31)


def test_the_annual_fallback_is_labelled_not_silent(seeded):  # noqa: F811
    c = complete_company(seeded, "Annual only", *IT, skip=("latest_quarter",))
    ready = rd.evaluate(seeded, c, today=TODAY)
    s = ready.by_id("revenue")
    assert s.period_type == "ANNUAL" and "no quarter within" in s.period_note
    assert any("use the latest ANNUAL period" in r for r in ready.reasons)
    found = next(x for x in ready.as_dict()["requirements"] if x["id"] == "revenue")["found"]
    assert found["period_note"]


def test_a_stale_quarter_is_not_used_when_the_quarter_mode_is_a_chosen_one(seeded):  # noqa: F811
    c = complete_company(seeded, "Annual", *IT, skip=("revenue",))
    evidence(seeded, c, "INCOME.revenue_from_operations", 1000, end=FY25, ptype="ANNUAL")
    s = revenue(seeded, c, period_mode="QUARTER", fiscal_year=2026, quarter="Q4")
    assert s.status == rd.MISSING  # an annual figure is not a quarter


# ---- basis ----------------------------------------------------------------------------------------


def test_consolidated_is_requested_by_default_and_standalone_on_demand(seeded):  # noqa: F811
    c = complete_company(seeded, "Both", *IT, skip=("net_profit",))
    evidence(seeded, c, "INCOME.net_profit", 5, basis="CONSOLIDATED")
    evidence(seeded, c, "INCOME.net_profit", 4, ptype="ANNUAL", basis="STANDALONE")
    default = rd.evaluate(seeded, c, today=TODAY).by_id("net_profit")
    assert default.basis == "CONSOLIDATED" and str(default.value) == "5.000000"
    own = rd.evaluate(seeded, c, today=TODAY, prefs=pf.Preferences(basis="STANDALONE"))
    assert own.by_id("net_profit").basis == "STANDALONE"
    assert str(own.by_id("net_profit").value) == "4.000000"


def test_an_unavailable_basis_is_explained_with_the_other_basis_named(seeded):  # noqa: F811
    c = complete_company(seeded, "Only standalone", *IT, skip=("net_profit",))
    evidence(seeded, c, "INCOME.net_profit", 4, basis="STANDALONE")
    ready = rd.evaluate(seeded, c, today=TODAY)
    miss = next(m for m in ready.as_dict()["missing"]["CRITICAL"] if m["id"] == "net_profit")
    assert "consolidated basis you chose" in miss["reason"]
    assert "unless you change the basis preference" in miss["reason"]
    assert miss["other_basis_available"]["basis"] == "STANDALONE"
    assert miss["basis_required"].startswith("consolidated")


def test_bank_level_ratios_are_not_filtered_by_basis(seeded):  # noqa: F811
    c = complete_company(seeded, "Ratio bank", *BANK)
    for pref in ("CONSOLIDATED", "STANDALONE"):
        ready = rd.evaluate(seeded, c, today=TODAY, prefs=pf.Preferences(basis=pref))
        for rid in ("nim", "gross_npa", "net_npa", "capital_adequacy"):
            assert ready.by_id(rid).ok, (pref, rid)


def test_a_bank_with_only_standalone_statements_is_asked_for_a_decision(seeded):  # noqa: F811
    c = complete_company(seeded, "Standalone bank", *BANK, skip=("net_profit",))
    evidence(seeded, c, "INCOME.net_profit", 4, basis="STANDALONE")
    default = rd.evaluate(seeded, c, today=TODAY)
    assert default.eligibility == rd.ASSISTED and not default.by_id("net_profit").ok
    own = rd.evaluate(seeded, c, today=TODAY, prefs=pf.Preferences(basis="STANDALONE"))
    assert own.by_id("net_profit").ok


# ---- key figures: dimensions and alternatives -----------------------------------------------------


def test_the_whole_bank_nim_is_preferred_and_a_domestic_one_is_labelled(seeded):  # noqa: F811
    c = complete_company(seeded, "Two NIMs", *BANK, skip=("nim",))
    evidence(
        seeded, c, "RATIOS.net_interest_margin", 3.11, end=QUARTER, ptype="QUARTERLY",
        basis=None, kind="DISCLOSURE", unit="percent",
    )  # fmt: skip
    first = rd.evaluate(seeded, c, today=TODAY).by_id("nim")
    assert first.ok and first.dimension == ""
    only = complete_company(seeded, "Domestic NIM", *BANK, skip=("nim",))
    r = evidence(
        seeded, only, "RATIOS.net_interest_margin", 3.11, end=QUARTER, ptype="QUARTERLY",
        basis=None, kind="DISCLOSURE", unit="percent",
    )  # fmt: skip
    r.dimension = "domestic"
    seeded.flush()
    got = rd.evaluate(seeded, only, today=TODAY).by_id("nim")
    assert got.ok and got.dimension == "domestic"
    dumped = next(
        x
        for x in rd.evaluate(seeded, only, today=TODAY).as_dict()["requirements"]
        if x["id"] == "nim"
    )
    assert dumped["found"]["dimension"] == "domestic"


def test_gross_advances_and_total_deposits_from_a_release_satisfy_the_bank_requirements(seeded):  # noqa: F811
    c = complete_company(seeded, "Release bank", *BANK, skip=("advances", "deposits"))
    for metric in ("RATIOS.gross_advances", "RATIOS.total_deposits"):
        evidence(
            seeded,
            c,
            metric,
            100,
            end=QUARTER,
            ptype=None,
            basis=None,
            kind="DISCLOSURE",
            unit="inr",
        )
    ready = rd.evaluate(seeded, c, today=TODAY)
    assert ready.by_id("advances").metric == "RATIOS.gross_advances"
    assert ready.by_id("deposits").metric == "RATIOS.total_deposits"
    assert ready.by_id("advances").req.alternative  # labelled as gross, not the net balance figure


# ---- optional items -------------------------------------------------------------------------------


def test_optional_items_appear_only_when_asked_for(seeded):  # noqa: F811
    c = complete_company(seeded, "Optional", *IT)
    off = rd.evaluate(seeded, c, today=TODAY)
    on = rd.evaluate(seeded, c, today=TODAY, prefs=pf.Preferences(include_optional=True))
    assert all(s.req.importance != rq.OPTIONAL for s in off.statuses)
    assert any(s.req.importance == rq.OPTIONAL for s in on.statuses)
    assert on.as_dict()["preferences"]["include_optional"] is True


# ---- sessions keep their choices ------------------------------------------------------------------


def test_a_new_session_records_its_preferences_and_a_log_entry(seeded):  # noqa: F811
    c = complete_company(seeded, "Session", *IT)
    sess = orchestrator.create_session(seeded, c, "alice", "Session")
    assert orchestrator.session_prefs(sess) == pf.DEFAULT
    assert len(sess.preference_log) == 1 and sess.preference_log[0]["from"] is None


def test_changing_preferences_is_logged_and_the_old_choice_is_kept(seeded):  # noqa: F811
    c = complete_company(seeded, "Log", *IT)
    chosen = pf.Preferences(period_mode="FISCAL_YEAR", fiscal_year=2026)
    sess = orchestrator.create_session(seeded, c, "alice", "Log", prefs=chosen)
    assert orchestrator.set_preferences(seeded, sess, pf.Preferences(basis="STANDALONE"), "alice")
    assert not orchestrator.set_preferences(
        seeded, sess, pf.Preferences(basis="STANDALONE"), "alice"
    )
    log = sess.preference_log
    assert [e["to"]["period_mode"] for e in log] == ["FISCAL_YEAR", "LATEST_QUARTER"]
    assert log[1]["from"]["fiscal_year"] == 2026  # the earlier selection is preserved
    assert orchestrator.status(seeded, sess)["preference_log"] == log


def test_reopening_a_session_uses_its_own_period_not_the_latest(seeded):  # noqa: F811
    c = two_periods(seeded, "Reopen")
    prefs = pf.Preferences(period_mode="QUARTER", fiscal_year=2027, quarter="Q1")
    sess = orchestrator.create_session(seeded, c, "alice", "Reopen", prefs=prefs)
    orchestrator.run(seeded, sess, today=TODAY)
    ready = sess.readiness
    revenue_found = next(x for x in ready["requirements"] if x["id"] == "revenue")["found"]
    assert revenue_found["period_end"] == Q1_FY27.isoformat()
    assert ready["preferences"]["quarter"] == "Q1"


def test_reports_record_the_preferences_they_were_made_under(seeded):  # noqa: F811
    c = complete_company(seeded, "Report prefs", *IT)
    sess = orchestrator.create_session(
        seeded, c, "alice", "Report prefs", prefs=pf.Preferences(period_mode="LATEST_ANNUAL")
    )
    orchestrator.run(seeded, sess, today=TODAY)
    rpt = rp.generate(seeded, sess, rp.AVAILABLE_EVIDENCE, "alice", today=TODAY)
    assert rpt.content["meta"]["preferences"]["period_mode"] == "LATEST_ANNUAL"
    orchestrator.set_preferences(
        seeded, sess, pf.Preferences(period_mode="LATEST_QUARTER"), "alice"
    )
    newer = rp.generate(seeded, sess, rp.AVAILABLE_EVIDENCE, "alice", today=TODAY)
    assert newer.version == rpt.version + 1  # a changed choice is a new version...
    assert rpt.content["meta"]["preferences"]["period_mode"] == "LATEST_ANNUAL"  # ...old untouched


# ---- API ------------------------------------------------------------------------------------------

ALICE = {"X-API-Key": "alice-secret"}
BOB = {"X-API-Key": "bob-secret"}


@pytest.fixture
def client(seeded, monkeypatch, tmp_path):  # noqa: F811
    settings = get_settings()
    monkeypatch.setattr(settings, "research_api_keys", "alice:alice-secret,bob:bob-secret")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(research_api, "INLINE", True)
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    security.limiter.reset()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: seeded
    return TestClient(app)


def test_a_session_can_be_started_with_preferences(client, seeded):  # noqa: F811
    c = two_periods(seeded, "Api prefs")
    body = {
        "company_id": str(c.id),
        "preferences": {"period_mode": "FISCAL_YEAR", "fiscal_year": 2026, "basis": "CONSOLIDATED"},
    }
    r = client.post("/api/research/sessions", json=body, headers=ALICE)
    assert r.status_code == 201
    assert r.json()["preferences"]["period_mode"] == "FISCAL_YEAR"
    sid = r.json()["id"]
    ready = client.get(f"/api/research/sessions/{sid}/readiness", headers=ALICE).json()
    assert ready["preferences"]["fiscal_year"] == 2026
    rev = next(x for x in ready["requirements"] if x["id"] == "revenue")
    assert rev["found"]["period_type"] == "ANNUAL"


def test_invalid_preferences_are_rejected_by_the_api(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Api bad", *IT)
    r = client.post(
        "/api/research/sessions",
        json={"company_id": str(c.id), "preferences": {"period_mode": "QUARTER"}},
        headers=ALICE,
    )
    assert r.status_code == 422 and "fiscal year" in r.json()["detail"]


def test_preferences_can_be_changed_and_the_log_is_readable(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Api change", *IT)
    sid = client.post(
        "/api/research/sessions", json={"company_id": str(c.id)}, headers=ALICE
    ).json()["id"]
    got = client.get(f"/api/research/sessions/{sid}/preferences", headers=ALICE).json()
    assert got["current"]["basis"] == "CONSOLIDATED" and "STANDALONE" in got["options"]["basis"]
    r = client.put(
        f"/api/research/sessions/{sid}/preferences",
        json={"basis": "STANDALONE", "period_mode": "LATEST_ANNUAL"},
        headers=ALICE,
    )
    assert r.status_code == 200 and r.json()["preferences"]["basis"] == "STANDALONE"
    log = client.get(f"/api/research/sessions/{sid}/preferences", headers=ALICE).json()["log"]
    assert len(log) == 2 and log[1]["from"]["basis"] == "CONSOLIDATED"
    missing = client.get(f"/api/research/sessions/{sid}/missing", headers=ALICE).json()
    # standalone was asked for, but this company only has consolidated figures: nothing is borrowed
    assert missing["missing"]["CRITICAL"]
    assert any("standalone basis you chose" in m["reason"] for m in missing["missing"]["CRITICAL"])
    assert (
        client.put(
            f"/api/research/sessions/{sid}/preferences", json={"basis": "BOTH"}, headers=ALICE
        ).status_code
        == 422
    )


def test_other_users_cannot_read_or_change_a_sessions_preferences(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Api private", *IT)
    sid = client.post(
        "/api/research/sessions", json={"company_id": str(c.id)}, headers=ALICE
    ).json()["id"]
    assert client.get(f"/api/research/sessions/{sid}/preferences", headers=BOB).status_code == 404
    assert (
        client.put(
            f"/api/research/sessions/{sid}/preferences", json={"basis": "STANDALONE"}, headers=BOB
        ).status_code
        == 404
    )


def test_a_company_with_data_only_on_the_other_basis_is_not_called_blocked(seeded):  # noqa: F811
    c = mk_company(seeded, "Standalone only", *BANK)
    evidence(seeded, c, "INCOME.total_income", 10, basis="STANDALONE")
    evidence(seeded, c, "INCOME.net_profit", 3, basis="STANDALONE")
    default = rd.evaluate(seeded, c, today=TODAY)
    assert default.eligibility == rd.ASSISTED  # not BLOCKED: the data exists
    assert any("only on the standalone basis" in r for r in default.reasons)
    own = rd.evaluate(seeded, c, today=TODAY, prefs=pf.Preferences(basis="STANDALONE"))
    assert own.by_id("net_profit").ok
    empty = mk_company(seeded, "Truly empty", *BANK)
    assert rd.evaluate(seeded, empty, today=TODAY).eligibility == rd.BLOCKED
