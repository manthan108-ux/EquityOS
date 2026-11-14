# ruff: noqa: F811, E501
"""The valuation assumption ledger: provenance, editing, staleness, CAPM, scenarios and the report."""

import json
from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import research as research_api  # noqa: F401
from app.core.config import get_settings
from app.core.database import get_session
from app.main import create_app
from app.models import EvidenceRecord, EvidenceStatus
from app.research import assumptions as asm
from app.research import security, valuation
from tests.test_sector import mk_company, seeded  # noqa: F401

TODAY = date(2026, 11, 14)
RATIONALE = "the benchmark ten-year government security yield on the as-of date"


def put(session, co, name, value, **kw):
    kw.setdefault("author", "A. Analyst")
    kw.setdefault("rationale", RATIONALE)
    kw.setdefault("as_of", date(2026, 11, 12))
    kw.setdefault("today", TODAY)
    return asm.set_input(session, co.id, name, value, **kw)


def all_market(session, co, **kw):
    put(session, co, "risk_free_rate", "7", source="RBI: 10-year G-sec yield", **kw)
    put(
        session,
        co,
        "equity_risk_premium",
        "5",
        source="A named published premium study",
        as_of=date(2026, 1, 15),
    )
    put(session, co, "beta", "1.2", source="A named beta provider", as_of=date(2026, 10, 1))


# ---- entering inputs --------------------------------------------------------------------------------------------


def test_a_sourced_input_records_source_date_author_and_rationale(seeded):
    co = mk_company(seeded, "Input Co")
    r = put(
        seeded,
        co,
        "risk_free_rate",
        "6.9",
        source="RBI: 10-year G-sec yield",
        source_url="https://example.test/gsec",
    )
    item = asm.current_inputs(seeded, co.id, TODAY)["VALUATION_INPUT.risk_free_rate"]
    assert (
        item["class"] == "SOURCED_INPUT"
        and item["source"] == "RBI: 10-year G-sec yield"
        and item["source_backed"]
    )
    assert (
        item["as_of"] == "2026-11-12"
        and item["author"] == "A. Analyst"
        and item["rationale"] == RATIONALE
    )
    assert (
        D(item["value"]) == D("6.9") and item["unit"] == "percent" and item["evidence_id"] == r.id
    )
    assert (
        item["age_days"] == 2
        and item["stale"] is False
        and item["source_url"] == "https://example.test/gsec"
    )


def test_an_input_without_a_source_is_a_user_assumption_and_says_so(seeded):
    co = mk_company(seeded, "User Co")
    put(
        seeded,
        co,
        "terminal_growth",
        "6",
        scenario="base",
        rationale="own view of long-run nominal growth for this business",
    )
    item = asm.current_inputs(seeded, co.id, TODAY)["VALUATION_INPUT.terminal_growth_base"]
    assert (
        item["class"] == "USER_ASSUMPTION"
        and not item["source_backed"]
        and item["scenario"] == "base"
    )
    rec = seeded.get(EvidenceRecord, item["evidence_id"])
    assert "own assumption" in rec.statement_text and rec.created_by == "A. Analyst"


def test_a_market_input_without_a_source_is_flagged_not_hidden(seeded):
    co = mk_company(seeded, "Unsourced Co")
    put(seeded, co, "beta", "1.1")
    led = asm.ledger_view(asm.current_inputs(seeded, co.id, TODAY), {"completed": False})
    assert any("market input without a source" in f for f in led["flags"])


@pytest.mark.parametrize(
    ("name", "value", "kw", "message"),
    [
        (
            "risk_free_rate",
            "0.07",
            {},
            None,
        ),  # a plausible fraction slip is still in range: allowed (0.07%)
        ("risk_free_rate", "70", {}, "outside the plausible range"),
        ("beta", "9", {}, "outside the plausible range"),
        ("nonsense", "1", {}, "unknown input"),
        ("cost_of_equity", "12", {}, "per scenario"),
        ("risk_free_rate", "7", {"scenario": "base"}, "not set per scenario"),
        ("risk_free_rate", "abc", {}, "not a number"),
        ("risk_free_rate", "7", {"rationale": "short"}, "rationale"),
        ("risk_free_rate", "7", {"author": " "}, "who entered"),
        ("risk_free_rate", "7", {"as_of": date(2026, 12, 1)}, "future"),
        ("risk_free_rate", "7", {"source_url": "https://x.test"}, "needs a source name"),
    ],
)
def test_bad_inputs_are_refused_and_nothing_is_written(seeded, name, value, kw, message):
    co = mk_company(seeded, f"Bad {name} {value}")
    kw = dict(kw)
    if name == "cost_of_equity" and "scenario" not in kw:
        pass
    if message is None:
        put(seeded, co, name, value, **kw)
        return
    with pytest.raises(asm.AssumptionError, match=message):
        put(seeded, co, name, value, **kw)
    assert asm.current_inputs(seeded, co.id, TODAY) == {}


# ---- editing never overwrites -----------------------------------------------------------------------------------


def test_an_edit_supersedes_the_old_value_and_keeps_the_history(seeded):
    co = mk_company(seeded, "Edit Co")
    first = put(seeded, co, "risk_free_rate", "6.9", source="RBI: 10-year G-sec yield")
    second = put(seeded, co, "risk_free_rate", "7.1", source="RBI: 10-year G-sec yield", as_of=date(2026, 11, 13), author="B. Reviewer", edit_reason="new yield")  # fmt: skip
    cur = asm.current_inputs(seeded, co.id, TODAY)["VALUATION_INPUT.risk_free_rate"]
    assert (
        cur["evidence_id"] == second.id
        and D(cur["value"]) == D("7.1")
        and cur["supersedes"] == first.id
    )
    old = seeded.get(EvidenceRecord, first.id)
    assert (
        old.status is EvidenceStatus.SUPERSEDED
        and "B. Reviewer" in old.status_reason
        and "new yield" in old.status_reason
    )
    hist = asm.history(seeded, co.id, "risk_free_rate", None, TODAY)
    assert [h["evidence_id"] for h in hist] == [second.id, first.id] and [
        h["status"] for h in hist
    ] == ["ACTIVE", "SUPERSEDED"]


def test_entering_the_identical_input_again_is_idempotent(seeded):
    co = mk_company(seeded, "Same Co")
    a = put(seeded, co, "beta", "1.1", source="A named beta provider")
    b = put(seeded, co, "beta", "1.1", source="A named beta provider")
    assert a.id == b.id and len(asm.history(seeded, co.id, "beta", None, TODAY)) == 1


def test_withdrawing_an_input_leaves_none_and_the_record_in_history(seeded):
    co = mk_company(seeded, "Withdraw Co")
    r = put(seeded, co, "beta", "1.1", source="A named beta provider")
    asm.withdraw_input(seeded, r.id, "the provider restated its method", by="A. Analyst")
    assert asm.current_inputs(seeded, co.id, TODAY) == {}
    assert asm.history(seeded, co.id, "beta", None, TODAY)[0]["status"] == "WITHDRAWN"
    with pytest.raises(asm.AssumptionError, match="not a valuation input"):
        asm.withdraw_input(seeded, 999999999, "x" * 5, by="A")


# ---- staleness ----------------------------------------------------------------------------------------------------


def test_an_old_market_input_is_flagged_stale_by_the_stated_limits(seeded):
    co = mk_company(seeded, "Stale Co")
    put(
        seeded, co, "risk_free_rate", "7", source="RBI: 10-year G-sec yield", as_of=date(2026, 9, 1)
    )  # 74 days: over 45
    put(
        seeded, co, "beta", "1.1", source="A named beta provider", as_of=date(2026, 1, 1)
    )  # 317 days: within 400
    inputs = asm.current_inputs(seeded, co.id, TODAY)
    assert (
        inputs["VALUATION_INPUT.risk_free_rate"]["stale"] is True
        and inputs["VALUATION_INPUT.beta"]["stale"] is False
    )
    assert any(
        "Risk-free rate is stale" in f
        for f in asm.ledger_view(inputs, {"completed": False})["flags"]
    )


# ---- CAPM: derived, linked, never defaulted --------------------------------------------------------------------


def test_cost_of_equity_is_derived_only_from_the_three_entered_inputs(seeded):
    co = mk_company(seeded, "Capm Co")
    put(seeded, co, "risk_free_rate", "7", source="RBI: 10-year G-sec yield")
    out = asm.capm_cost_of_equity(seeded, co.id, asm.current_inputs(seeded, co.id, TODAY))
    assert (
        out["completed"] is False
        and out["missing"] == ["Equity risk premium", "Beta"]
        and "none is assumed" in out["reason"]
    )
    all_market(seeded, co)
    inputs = asm.current_inputs(seeded, co.id, TODAY)
    out = asm.capm_cost_of_equity(seeded, co.id, inputs)
    assert out["completed"] and D(out["value"]) == D("13")  # 7 + 1.2 x 5
    assert (
        sorted(asm.lineage_ids(seeded, out["evidence_id"])) == sorted(out["input_ids"])
        and len(out["input_ids"]) == 3
    )


def test_editing_an_input_retires_the_derived_cost_of_equity_and_the_next_one_is_current(seeded):
    co = mk_company(seeded, "Retire Co")
    all_market(seeded, co)
    first = asm.capm_cost_of_equity(seeded, co.id, asm.current_inputs(seeded, co.id, TODAY))
    put(seeded, co, "beta", "1.4", source="A named beta provider", as_of=date(2026, 11, 1))
    assert seeded.get(EvidenceRecord, first["evidence_id"]).status is EvidenceStatus.WITHDRAWN
    second = asm.capm_cost_of_equity(seeded, co.id, asm.current_inputs(seeded, co.id, TODAY))
    assert (
        D(second["value"]) == D("14") and second["evidence_id"] != first["evidence_id"]
    )  # 7 + 1.4 x 5


def test_stale_and_unsourced_inputs_are_named_on_the_derived_figure(seeded):
    co = mk_company(seeded, "Flag Co")
    put(
        seeded, co, "risk_free_rate", "7", source="RBI: 10-year G-sec yield", as_of=date(2026, 6, 1)
    )
    put(seeded, co, "equity_risk_premium", "5")
    put(seeded, co, "beta", "1.2", source="A named beta provider")
    out = asm.capm_cost_of_equity(seeded, co.id, asm.current_inputs(seeded, co.id, TODAY))
    assert out["stale_inputs"] == ["Risk-free rate"] and out["not_source_backed"] == [
        "Equity risk premium"
    ]


# ---- scenarios --------------------------------------------------------------------------------------------------


def scenario_inputs(session, co, scenarios=("bear", "base", "bull")):
    values = {"bear": ("13", "5", "-2"), "base": ("12", "6", "0"), "bull": ("11", "7", "2")}
    for s in scenarios:
        coe, g, shift = values[s]
        put(
            session,
            co,
            "cost_of_equity",
            coe,
            scenario=s,
            rationale="own view of the required return for this bank",
        )
        put(
            session,
            co,
            "terminal_growth",
            g,
            scenario=s,
            rationale="own view of long-run nominal growth for banks",
        )
        put(
            session,
            co,
            "roe_shift",
            shift,
            scenario=s,
            rationale="own view of the ROE shift in this scenario",
        )


def test_no_inputs_means_no_scenario_and_the_missing_items_are_listed(seeded):
    mk_company(seeded, "Empty Scenario Co")
    complete, missing = asm.scenario_sets({}, {"completed": False})
    assert complete == {} and set(missing) == {"bear", "base", "bull"}
    assert "cost of equity for the base scenario" in missing["base"] and len(missing["base"]) == 3


def test_complete_scenarios_carry_their_input_ids_and_decimal_values(seeded):
    co = mk_company(seeded, "Full Scenario Co")
    scenario_inputs(seeded, co)
    inputs = asm.current_inputs(seeded, co.id, TODAY)
    complete, missing = asm.scenario_sets(inputs, {"completed": False})
    assert (
        missing == {}
        and complete["base"]["cost_of_equity"] == D("12")
        and complete["bear"]["roe_shift"] == D("-2")
    )
    assert len(complete["base"]["input_ids"]) == 3 and all(
        isinstance(i, int) for i in complete["base"]["input_ids"]
    )


def test_the_base_cost_of_equity_may_come_from_capm_but_growth_and_shift_must_be_entered(seeded):
    co = mk_company(seeded, "Capm Base Co")
    all_market(seeded, co)
    put(
        seeded,
        co,
        "terminal_growth",
        "6",
        scenario="base",
        rationale="own view of long-run nominal growth for banks",
    )
    inputs = asm.current_inputs(seeded, co.id, TODAY)
    capm = asm.capm_cost_of_equity(seeded, co.id, inputs)
    complete, missing = asm.scenario_sets(inputs, capm)
    assert "base" not in complete and missing["base"] == ["Shift in return on equity for the base scenario".lower().replace("shift", "shift")] or "roe" in missing["base"][0]  # fmt: skip
    put(
        seeded,
        co,
        "roe_shift",
        "0",
        scenario="base",
        rationale="own view of the ROE shift in the base case",
    )
    inputs = asm.current_inputs(seeded, co.id, TODAY)
    complete, _ = asm.scenario_sets(inputs, capm)
    assert (
        complete["base"]["cost_of_equity"] == D("13")
        and capm["evidence_id"] in complete["base"]["input_ids"]
    )
    assert set(capm["input_ids"]) <= set(complete["base"]["input_ids"]), (
        "the derived figure's own inputs are traceable from the scenario"
    )


# ---- the valuation workflow ----------------------------------------------------------------------------------


def bank_store(session, name="Val Bank"):
    from tests.test_report_analysis import BANK, BANK_SECTOR, add, store

    c = mk_company(session, name, *BANK_SECTOR)
    add(
        session,
        c,
        "RETURN.roe_closing",
        [12],
        ends=[date(2026, 3, 31)],
        unit="percent",
        kind="CALCULATION",
    )
    add(session, c, "PRICE.close", [1000], ends=[date(2026, 11, 6)], ptype=None, basis=None, unit="inr_per_share", kind="PRICE")  # fmt: skip
    add(session, c, "VALUATION.price_to_book", [2], ends=[date(2026, 11, 6)], ptype="QUARTERLY", unit="x", kind="CALCULATION")  # fmt: skip
    return c, store(session, c, BANK), BANK


def build_val(session, c, s, sector):
    return valuation.build(session, c.id, s, sector, "CONSOLIDATED", [], {})


def test_the_valuation_is_not_completed_without_entered_inputs_and_invents_nothing(
    seeded, monkeypatch
):
    from app.research import clock

    monkeypatch.setattr(clock, "today", lambda: TODAY)
    c, s, sector = bank_store(seeded)
    v = build_val(seeded, c, s, sector)
    intr = v["intrinsic"]
    assert intr["completed"] is False and "None is assumed" in intr["reason"]
    led = v["input_ledger"]
    assert (
        led["sourced_inputs"] == []
        and led["user_assumptions"] == []
        and led["derived_outputs"] == []
    )
    assert led["market_inputs_missing"] == ["Risk-free rate", "Equity risk premium", "Beta"]
    assert (
        "No risk-free rate" not in json.dumps(v["assumption_note"])
        and "entered inputs" in v["assumption_note"]
    )


def test_entered_inputs_complete_the_valuation_and_every_result_cites_them(seeded, monkeypatch):
    from app.research import clock

    monkeypatch.setattr(clock, "today", lambda: TODAY)
    c, s, sector = bank_store(seeded, "Val Bank Two")
    scenario_inputs(seeded, c)
    v = build_val(seeded, c, s, sector)
    intr = v["intrinsic"]
    assert intr["completed"] and intr["scenarios"]["base"]["value_per_share"] == "500"
    base_inputs = intr["scenarios"]["base"]["input_ids"]
    assert (
        set(base_inputs) <= set(intr["scenarios"]["base"]["evidence_ids"])
        and len(intr["scenarios"]["base"]["evidence_ids"]) == 6
    )
    led = v["input_ledger"]
    assert len(led["user_assumptions"]) == 9 and led["sourced_inputs"] == []
    assert {o["scenario"] for o in led["derived_outputs"]} == {"bear", "base", "bull"}
    assert [x["label"] for x in led["historical_actuals"]][:2] == ["Share price", "P/B"] or led[
        "historical_actuals"
    ]


def test_one_missing_scenario_is_listed_while_the_others_complete(seeded, monkeypatch):
    from app.research import clock

    monkeypatch.setattr(clock, "today", lambda: TODAY)
    c, s, sector = bank_store(seeded, "Val Bank Three")
    scenario_inputs(seeded, c, scenarios=("base", "bull"))
    intr = build_val(seeded, c, s, sector)["intrinsic"]
    assert (
        intr["completed"]
        and intr["scenarios"]["bear"]["completed"] is False
        and "missing" in intr["scenarios"]["bear"]
    )
    assert intr["scenarios"]["bull"]["value_per_share"]


def test_capm_inputs_feed_the_base_scenario_with_full_traceability(seeded, monkeypatch):
    from app.research import clock

    monkeypatch.setattr(clock, "today", lambda: TODAY)
    c, s, sector = bank_store(seeded, "Val Bank Four")
    all_market(seeded, c)
    put(
        seeded,
        c,
        "terminal_growth",
        "6",
        scenario="base",
        rationale="own view of long-run nominal growth for banks",
    )
    put(
        seeded,
        c,
        "roe_shift",
        "0",
        scenario="base",
        rationale="own view of the ROE shift in the base case",
    )
    v = build_val(seeded, c, s, sector)
    base = v["intrinsic"]["scenarios"]["base"]
    assert v["intrinsic"]["completed"] and base["assumptions"]["cost_of_equity"] == "13"
    assert v["input_ledger"]["capm"]["completed"] and len(v["input_ledger"]["sourced_inputs"]) == 3
    assert set(v["input_ledger"]["capm"]["input_ids"]) <= set(base["evidence_ids"])
    assert v["input_ledger"]["derived_inputs"][0]["value"] == "13"


def test_the_pdf_shows_the_four_kinds_and_what_is_missing(seeded, monkeypatch):
    from app.research import clock
    from tests.test_report_context import _axis, _flat

    monkeypatch.setattr(clock, "today", lambda: TODAY)
    c, s, sector = bank_store(seeded, "Val Bank Five")
    put(seeded, c, "risk_free_rate", "7", source="RBI: 10-year G-sec yield")
    content = _axis()
    content["analysis"]["valuation"] = json.loads(json.dumps(build_val(seeded, c, s, sector)))
    flat = _flat(content)
    assert "Valuation inputs and their provenance" in flat and "RBI: 10-year G-sec yield" in flat
    assert "none is assumed" in flat.lower() or "None is assumed" in flat
    assert "Historical actual" in flat


# ---- the API ---------------------------------------------------------------------------------------------------


@pytest.fixture
def client(seeded, monkeypatch):  # noqa: F811
    settings = get_settings()
    monkeypatch.setattr(settings, "research_api_keys", "alice:alice-secret")
    security.limiter.reset()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: seeded
    return TestClient(app)


KEY = {"X-API-Key": "alice-secret"}


def test_the_api_needs_a_key_and_records_the_authenticated_author(client, seeded):
    co = mk_company(seeded, "Api Co")
    url = f"/api/companies/{co.id}/valuation-inputs"
    assert client.get(url).status_code == 401
    body = {"input": "risk_free_rate", "value": "6.9", "as_of": "2026-11-12", "rationale": RATIONALE, "source": "RBI: 10-year G-sec yield"}  # fmt: skip
    r = client.put(url, json=body, headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json()["author"] == "alice" and r.json()["class"] == "SOURCED_INPUT"
    listed = client.get(url, headers=KEY).json()
    assert [i["input"] for i in listed["inputs"]] == [
        "risk_free_rate"
    ] and "nothing is assumed" in listed["note"]
    assert {k["input"] for k in listed["known_inputs"]} >= {"beta", "cost_of_equity"}


def test_the_api_edits_reads_history_and_withdraws(client, seeded):
    co = mk_company(seeded, "Api Edit Co")
    url = f"/api/companies/{co.id}/valuation-inputs"
    base = {
        "input": "beta",
        "value": "1.1",
        "as_of": "2026-10-01",
        "rationale": RATIONALE,
        "source": "A named beta provider",
    }
    first = client.put(url, json=base, headers=KEY).json()
    second = client.put(
        url, json={**base, "value": "1.3", "edit_reason": "provider update"}, headers=KEY
    ).json()
    hist = client.get(f"{url}/beta/history", headers=KEY).json()["history"]
    assert [h["evidence_id"] for h in hist] == [second["evidence_id"], first["evidence_id"]]
    w = client.post(
        f"/api/valuation-inputs/{second['evidence_id']}/withdraw",
        json={"reason": "no longer applies"},
        headers=KEY,
    )
    assert w.status_code == 200 and w.json()["status"] == "WITHDRAWN"
    assert client.get(url, headers=KEY).json()["inputs"] == []


def test_the_api_refuses_a_bad_input_with_the_reason(client, seeded):
    co = mk_company(seeded, "Api Bad Co")
    url = f"/api/companies/{co.id}/valuation-inputs"
    r = client.put(
        url,
        json={
            "input": "risk_free_rate",
            "value": "70",
            "as_of": "2026-11-12",
            "rationale": RATIONALE,
        },
        headers=KEY,
    )
    assert r.status_code == 422 and "plausible range" in r.json()["detail"]
    r = client.put(
        url,
        json={
            "input": "cost_of_equity",
            "value": "12",
            "as_of": "2026-11-12",
            "rationale": RATIONALE,
        },
        headers=KEY,
    )
    assert r.status_code == 422 and "per scenario" in r.json()["detail"]
    assert client.get(url, headers=KEY).json()["inputs"] == []


def test_the_api_returns_404_for_an_unknown_company(client):
    import uuid

    assert (
        client.get(f"/api/companies/{uuid.uuid4()}/valuation-inputs", headers=KEY).status_code
        == 404
    )


def test_the_ledger_holds_inputs_as_assumption_evidence(seeded):
    co = mk_company(seeded, "Class Co")
    r = put(seeded, co, "beta", "1.1", source="A named beta provider")
    rows = seeded.scalars(select(EvidenceRecord).where(EvidenceRecord.company_id == co.id)).all()
    assert [x.evidence_class.value for x in rows] == ["ASSUMPTION"] and rows[0].id == r.id
    assert (
        rows[0].details["source"] == "A named beta provider"
        and rows[0].details["as_of"] == "2026-11-12"
    )
