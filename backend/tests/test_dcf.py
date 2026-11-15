# ruff: noqa: F811, E501
"""Discounted cash flow valuation: the arithmetic, the scenarios, the inputs and the guards."""

import json
from datetime import date
from decimal import Decimal as D

import pytest

from app.research import assumptions as asm
from app.research import dcf, verdict
from app.research import readiness as rd
from app.research.sectors import sector_for
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

TODAY = date(2026, 11, 14)
CRORE = 10_000_000
FY = date(2026, 3, 31)
RAT = "own view of this input for this company, reasoned in the note"


# ---- the arithmetic, against closed forms -------------------------------------------------------------------------


def flat_case(**kw):
    a = {"revenue_growth": D(5), "terminal_growth": D(5), "operating_margin": D(20), "tax_rate": D(25), "capex_to_revenue": D(6), "depreciation_to_revenue": D(5), "working_capital_to_revenue": D(10)}  # fmt: skip
    a.update({k: D(str(v)) for k, v in kw.items()})
    return a


def test_fcff_and_present_values_match_a_hand_calculation():
    res = dcf.project(D(1000), flat_case(), D(10))
    # fcff_t = rev_{t-1} x (1.05 x (0.2 x 0.75 + 0.05 - 0.06) - 0.10 x 0.05) = 142 x 1.05^(t-1)
    for r in res["rows"]:
        t = r["year"]
        assert abs(r["fcff"] - D(142) * D("1.05") ** (t - 1)) < D("0.0001")
        assert abs(r["pv"] - r["fcff"] / D("1.1") ** t) < D("0.0001")
    pv = sum(D(142) * D("1.05") ** (t - 1) / D("1.1") ** t for t in range(1, 6))
    tv = D(142) * D("1.05") ** 5 / D("0.05")
    assert abs(res["pv_explicit"] - pv) < D("0.0001")
    assert abs(res["terminal_value"] - tv) < D("0.0001")
    assert abs(res["enterprise_value"] - (pv + tv / D("1.1") ** 5)) < D("0.001")


def test_growth_fades_linearly_to_the_long_run_rate_and_the_terminal_year_uses_it():
    res = dcf.project(D(1000), flat_case(revenue_growth=13, terminal_growth=5), D(10))
    assert [r["growth_pct"] for r in res["rows"]] == [D(13), D(11), D(9), D(7), D(5)]
    assert res["terminal_row"]["growth_pct"] == D(5)


def test_no_tax_is_charged_on_a_loss_and_working_capital_follows_the_change_in_revenue():
    res = dcf.project(D(1000), flat_case(operating_margin=-10), D(10))
    first = res["rows"][0]
    assert first["ebit"] < 0 and first["tax"] == 0 and first["nopat"] == first["ebit"]
    assert abs(first["change_in_working_capital"] - D("0.10") * (first["revenue"] - D(1000))) < D(
        "0.0001"
    )


def test_a_discount_rate_at_or_below_growth_is_refused():
    with pytest.raises(ValueError, match="must exceed"):
        dcf.project(D(1000), flat_case(terminal_growth=6), D(6))


# ---- the ledger inputs ----------------------------------------------------------------------------------------------


def put(session, co, name, value, scenario=None, **kw):
    kw.setdefault("author", "A. Analyst")
    kw.setdefault("rationale", RAT)
    kw.setdefault("as_of", date(2026, 11, 12))
    kw.setdefault("today", TODAY)
    return asm.set_input(session, co.id, name, value, scenario=scenario, **kw)


BASE = {"revenue_growth": 10, "terminal_growth": 5, "operating_margin": 20, "tax_rate": 25, "capex_to_revenue": 6, "depreciation_to_revenue": 5, "working_capital_to_revenue": 10}  # fmt: skip


def enter(session, co, scenario="base", wacc=10, **over):
    vals = {**BASE, **over}
    for name, v in vals.items():
        put(session, co, name, v, scenario)
    if wacc is not None:
        put(session, co, "wacc", wacc, scenario)


def company(session, name="Dcf Co", *, nci=None, net_debt=500, shares=100, with_price=True):
    co = mk_company(session, name)
    rows = {
        "INCOME.revenue_from_operations": (1000 * CRORE, "inr", "ANNUAL"),
        "LEVERAGE.net_debt": (net_debt * CRORE, "inr", "ANNUAL"),
        "BALANCE.shares_outstanding": (shares * CRORE, "ratio", "ANNUAL"),
    }
    if nci is not None:
        rows["BALANCE_SHEET.non_controlling_interests"] = (nci * CRORE, "inr", "ANNUAL")
    for metric, (v, unit, pt) in rows.items():
        evidence(session, co, metric, v, end=FY, basis="CONSOLIDATED", unit=unit, ptype=pt)
    if with_price:
        evidence(
            session,
            co,
            "PRICE.close",
            100,
            end=date(2026, 11, 6),
            basis=None,
            unit="inr_per_share",
            ptype=None,
        )
    return co


def build(session, co, monkeypatch, sector="IT_SERVICES"):
    from app.research import clock

    monkeypatch.setattr(clock, "today", lambda: TODAY)
    store = rd.load_store(session, co.id, set(dcf.NAMES))
    return dcf.build(session, co.id, store, sector_for(sector), "CONSOLIDATED", TODAY)


def test_without_entered_inputs_nothing_is_valued_and_the_missing_items_are_named(
    seeded, monkeypatch
):
    co = company(seeded)
    d = build(seeded, co, monkeypatch)
    assert d["applicable"] and d["completed"] is False and d["scenarios_complete"] == []
    base = d["scenarios"]["base"]
    assert base["completed"] is False
    for need in ("revenue growth in the first forecast year for the base scenario", "operating (ebit) margin for the base scenario", "tax rate for the base scenario"):  # fmt: skip
        assert any(need in m for m in base["missing"]), need
    assert any("cost of equity" in m for m in base["missing"]) and any(
        "cost of debt" in m for m in base["missing"]
    )
    assert d["sensitivity"] is None and "PUT /api/companies" in d["how_to_enter"]


def test_entered_inputs_give_the_value_and_a_full_bridge(seeded, monkeypatch):
    co = company(seeded, net_debt=500, shares=100)
    enter(seeded, co)
    d = build(seeded, co, monkeypatch)
    base = d["scenarios"]["base"]
    assert d["completed"] and base["completed"] and base["discount_rate"]["source"] == "ENTERED"
    # independent reconstruction from the same inputs
    res = dcf.project(D(1000 * CRORE), {k: D(v) for k, v in BASE.items()}, D(10))
    ev = res["enterprise_value"]
    assert abs(D(base["enterprise_value"]) - ev) <= D(1)
    assert abs(D(base["bridge"]["equity_value"]) - (ev - 500 * CRORE)) <= D(1)
    assert abs(D(base["value_per_share"]) - (ev - 500 * CRORE) / (100 * CRORE)) <= D("0.5")
    assert (
        base["vs_price_pct"] is not None
        and len(base["projection"]) == 5
        and base["projection"][0]["fy"] == "FY2027"
    )
    assert base["terminal"]["year"] == "FY2032"


def test_every_scenario_input_carries_its_provenance_and_the_result_cites_the_evidence(
    seeded, monkeypatch
):
    co = company(seeded)
    enter(seeded, co)
    base = build(seeded, co, monkeypatch)["scenarios"]["base"]
    a = base["assumptions"]["operating_margin"]
    assert a["author"] == "A. Analyst" and a["as_of"] == "2026-11-12" and a["rationale"] == RAT and a["class"] == "USER_ASSUMPTION"  # fmt: skip
    ids = set(base["evidence_ids"])
    assert {x["evidence_id"] for x in base["assumptions"].values()} <= ids
    assert base["discount_rate"]["input_ids"][0] in ids


def test_scenarios_are_independent_and_incomplete_ones_are_listed(seeded, monkeypatch):
    co = company(seeded)
    enter(seeded, co, "base", wacc=10)
    enter(seeded, co, "bull", wacc=9, revenue_growth=14, operating_margin=24)
    d = build(seeded, co, monkeypatch)
    assert (
        d["scenarios_complete"] == ["base", "bull"] and d["scenarios"]["bear"]["completed"] is False
    )
    assert D(d["scenarios"]["bull"]["value_per_share"]) > D(
        d["scenarios"]["base"]["value_per_share"]
    )


def test_editing_an_input_changes_the_value_and_keeps_the_history(seeded, monkeypatch):
    co = company(seeded)
    enter(seeded, co)
    v1 = D(build(seeded, co, monkeypatch)["scenarios"]["base"]["value_per_share"])
    put(seeded, co, "operating_margin", 15, "base", edit_reason="lower margin view")
    v2 = D(build(seeded, co, monkeypatch)["scenarios"]["base"]["value_per_share"])
    assert v2 < v1
    hist = asm.history(seeded, co.id, "operating_margin", "base", TODAY)
    assert [h["status"] for h in hist] == ["ACTIVE", "SUPERSEDED"] and D(hist[1]["value"]) == D(20)


# ---- the discount rate -----------------------------------------------------------------------------------------


def test_wacc_is_built_from_cost_of_equity_cost_of_debt_tax_and_market_weights(seeded, monkeypatch):
    co = company(seeded, net_debt=500, shares=100)  # market cap = 100 x 100 shares = 10,000 crore
    evidence(
        seeded,
        co,
        "LEVERAGE.total_borrowings",
        2500 * CRORE,
        end=FY,
        basis="CONSOLIDATED",
        unit="inr",
        ptype="ANNUAL",
    )
    enter(seeded, co, wacc=None)
    put(seeded, co, "cost_of_equity", 12, "base")
    put(seeded, co, "cost_of_debt", 8, source="Company filing: weighted average borrowing cost")
    d = build(seeded, co, monkeypatch)["scenarios"]["base"]
    # E = 10,000, D = 2,500: 0.8 x 12 + 0.2 x 8 x (1 - 0.25) = 9.6 + 1.2 = 10.8
    assert (
        d["completed"]
        and d["discount_rate"]["source"] == "DERIVED"
        and D(d["discount_rate"]["value"]) == D("10.80")
    )
    assert d["discount_rate"]["weights"] == {"equity_pct": "80", "debt_pct": "20"}
    assert len(d["discount_rate"]["input_ids"]) == 6


def test_the_base_cost_of_equity_may_come_from_capm_of_sourced_inputs(seeded, monkeypatch):
    co = company(seeded)
    evidence(
        seeded,
        co,
        "LEVERAGE.total_borrowings",
        2500 * CRORE,
        end=FY,
        basis="CONSOLIDATED",
        unit="inr",
        ptype="ANNUAL",
    )
    enter(seeded, co, wacc=None)
    put(seeded, co, "risk_free_rate", 7, source="RBI: 10-year G-sec yield")
    put(
        seeded,
        co,
        "equity_risk_premium",
        5,
        source="A named premium study",
        as_of=date(2026, 1, 15),
    )
    put(seeded, co, "beta", "1.2", source="A named beta provider", as_of=date(2026, 10, 1))
    put(seeded, co, "cost_of_debt", 8, source="Company filing")
    d = build(seeded, co, monkeypatch)["scenarios"]["base"]
    assert d["completed"] and D(d["discount_rate"]["value"]) == D("13") * D("0.8") + D(8) * D(
        "0.2"
    ) * D("0.75")  # 11.6
    bear = build(seeded, co, monkeypatch)["scenarios"]["bear"]
    assert bear["completed"] is False and any(
        "cost of equity for the bear scenario" in m for m in bear["missing"]
    )


def test_without_market_weights_a_built_discount_rate_is_not_assumed(seeded, monkeypatch):
    co = company(seeded, with_price=False)
    enter(seeded, co, wacc=None)
    put(seeded, co, "cost_of_equity", 12, "base")
    put(seeded, co, "cost_of_debt", 8)
    base = build(seeded, co, monkeypatch)["scenarios"]["base"]
    assert base["completed"] is False and any("share price" in m for m in base["missing"]) and any("borrowings" in m for m in base["missing"])  # fmt: skip


def test_a_discount_rate_at_or_below_long_run_growth_makes_no_value(seeded, monkeypatch):
    co = company(seeded)
    enter(seeded, co, wacc=5, terminal_growth=5)
    base = build(seeded, co, monkeypatch)["scenarios"]["base"]
    assert base["completed"] is False and "terminal value is undefined" in base["reason"]


# ---- guards and flags ------------------------------------------------------------------------------------------


def test_missing_actuals_are_named_not_defaulted(seeded, monkeypatch):
    co = mk_company(seeded, "No Actuals Co")
    enter(seeded, co)
    base = build(seeded, co, monkeypatch)["scenarios"]["base"]
    assert base["completed"] is False
    for need in ("the last annual revenue", "shares outstanding", "net debt"):
        assert any(f"stored {need}" in m for m in base["missing"])


def test_flags_say_when_most_value_is_terminal_when_capex_trails_depreciation_and_when_nci_is_unknown(
    seeded, monkeypatch
):
    co = company(seeded)
    enter(seeded, co, wacc=8, terminal_growth=6, capex_to_revenue=3, depreciation_to_revenue=5)
    base = build(seeded, co, monkeypatch)["scenarios"]["base"]
    text = " ".join(base["flags"])
    assert "terminal value is" in text and "capital expenditure is below depreciation" in text
    assert "non-controlling interests are not stored" in text
    with_nci = company(seeded, "Nci Co", nci=300)
    enter(seeded, with_nci)
    b2 = build(seeded, with_nci, monkeypatch)["scenarios"]["base"]
    assert b2["bridge"]["non_controlling_interests"] == str(300 * CRORE)
    assert not any("non-controlling" in f for f in b2["flags"])


def test_net_debt_from_another_date_than_revenue_is_flagged(seeded, monkeypatch):
    co = mk_company(seeded, "Dates Co")
    evidence(
        seeded,
        co,
        "INCOME.revenue_from_operations",
        1000 * CRORE,
        end=FY,
        basis="CONSOLIDATED",
        unit="inr",
        ptype="ANNUAL",
    )
    evidence(
        seeded,
        co,
        "LEVERAGE.net_debt",
        500 * CRORE,
        end=date(2025, 3, 31),
        basis="CONSOLIDATED",
        unit="inr",
        ptype="ANNUAL",
    )
    evidence(
        seeded,
        co,
        "BALANCE.shares_outstanding",
        100 * CRORE,
        end=FY,
        basis="CONSOLIDATED",
        unit="ratio",
        ptype="ANNUAL",
    )
    enter(seeded, co)
    base = build(seeded, co, monkeypatch)["scenarios"]["base"]
    assert any("net debt is as at 2025-03-31" in f for f in base["flags"])


# ---- sensitivity -----------------------------------------------------------------------------------------------


def test_the_sensitivity_grids_centre_on_the_base_case_and_never_divide_by_a_non_positive_spread(
    seeded, monkeypatch
):
    co = company(seeded)
    enter(seeded, co, wacc=6, terminal_growth=5)
    d = build(seeded, co, monkeypatch)
    s = d["sensitivity"]
    centre = s["wacc_vs_growth"][2]["cells"][2]["value_per_share"]
    assert centre == d["scenarios"]["base"]["value_per_share"] == s["base_value_per_share"]
    assert s["margin_vs_growth"][2]["cells"][2]["value_per_share"] == centre
    assert s["wacc_vs_growth"][0]["cells"][4]["value_per_share"] is None, (
        "WACC 4% with growth 6% has no value"
    )
    first, last = (D(s["wacc_vs_growth"][i]["cells"][2]["value_per_share"]) for i in (2, 4))
    assert first > last, "a higher discount rate lowers the value"
    assert "not inputs" in s["note"]


# ---- banks ------------------------------------------------------------------------------------------------------


def test_a_bank_is_not_valued_by_fcff_and_the_reason_is_given(seeded, monkeypatch):
    co = company(seeded, "Bank Co")
    d = build(seeded, co, monkeypatch, sector="BANK")
    assert d["applicable"] is False and d["kind"] == "BANK"
    assert (
        "raw material" in d["reason"] and "residual-income" in d["reason"] and "scenarios" not in d
    )


# ---- integration with the valuation, the verdict and the report ----------------------------------------------------


def test_the_valuation_carries_the_dcf_and_the_verdict_counts_it_as_a_method(seeded, monkeypatch):
    co = company(seeded)
    enter(seeded, co)
    d = build(seeded, co, monkeypatch)
    valuation = {"intrinsic": {"completed": False}, "relative": {"completed": False}, "dcf": d}
    assert verdict._values(valuation, "base") == [D(d["scenarios"]["base"]["value_per_share"])]
    assert verdict._values(valuation, "bear") == [], "an incomplete scenario contributes nothing"
    assert verdict._methods_disagree({"intrinsic": {}, "relative": {}, "dcf": d}, D("100")) is False


def test_methods_on_opposite_sides_of_the_price_still_disagree_with_three_methods():
    def block(v):
        return {"completed": True, "scenarios": {"base": {"value_per_share": v}}}

    assert verdict._methods_disagree({"intrinsic": block("150"), "relative": block("120"), "dcf": block("80")}, D("100")) is True  # fmt: skip
    assert verdict._methods_disagree({"intrinsic": block("150"), "relative": block("120"), "dcf": block("110")}, D("100")) is False  # fmt: skip


def test_the_pdf_shows_the_dcf_with_assumptions_projection_and_sensitivity(seeded, monkeypatch):
    from app.research import valuation
    from tests.test_report_context import _axis, _flat

    co = company(seeded)
    enter(seeded, co, "base")
    enter(seeded, co, "bull", wacc=9, revenue_growth=14)
    d = build(seeded, co, monkeypatch)
    content = _axis()
    val = content["analysis"]["valuation"]
    val["dcf"] = json.loads(json.dumps(d))
    flat = _flat(content)
    assert (
        "Discounted cash flow (FCFF)" in flat
        and "Base-case assumptions and their provenance" in flat
    )
    assert (
        "Base-case projection" in flat
        and "Sensitivity: value per share" in flat
        and "not completed" in flat
    )
    assert valuation  # the module imports cleanly alongside


def test_the_pdf_says_why_a_bank_has_no_dcf(seeded, monkeypatch):
    from tests.test_report_context import _axis, _flat

    co = company(seeded, "Pdf Bank Co")
    content = _axis()
    content["analysis"]["valuation"]["dcf"] = json.loads(
        json.dumps(build(seeded, co, monkeypatch, sector="BANK"))
    )
    assert "not applied to a bank" in _flat(content)
