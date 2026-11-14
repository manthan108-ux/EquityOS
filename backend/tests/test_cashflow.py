# ruff: noqa: F811, E501
"""Cash flow and earnings quality: CFO/PAT, free cash flow, capex, accruals, divergences, peers."""

import json
from datetime import date
from decimal import Decimal as D

from app.research import cashflow as cf
from app.research import readiness as rd
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

YEARS = [date(2023, 3, 31), date(2024, 3, 31), date(2025, 3, 31), date(2026, 3, 31)]
CRORE = 10_000_000
CFO = "CASH_FLOW.net_cash_from_operating_activities"
PAT = "INCOME.attributable_profit"
RATIO_UNITS = {
    "CASH.cfo_to_net_profit": "x",
    "CASH.cfo_to_ebitda": "x",
    "CASH.fcf_to_net_profit": "x",
    "CASH.capex_to_depreciation": "x",
    "CASH.cfo_margin": "percent",
    "CASH.fcf_margin": "percent",
    "CASH.capex_to_revenue": "percent",
    "ACCRUAL.accrual_ratio": "percent",
}


def cr(*vals):
    return [None if v is None else v * CRORE for v in vals]


def put(session, co, metric, values, *, basis="CONSOLIDATED", years=YEARS):
    unit = RATIO_UNITS.get(metric, "inr")
    for end, v in zip(years, values, strict=False):
        if v is not None:
            evidence(session, co, metric, v, end=end, basis=basis, unit=unit)


def store(session, co):
    return rd.load_store(session, co.id, set(cf.NAMES))


def company(session, series, name="Cash Co"):
    co = mk_company(session, name)
    for metric, values in series.items():
        put(session, co, metric, values)
    return co


def test_operating_cash_flow_series_growth_and_cagr(seeded):
    co = company(seeded, {CFO: cr(100, 120, 150, 180)})
    g = cf._flow(store(seeded, co), cf.CFO, "CONSOLIDATED")
    yoy = [None if r["yoy_pct"] is None else D(r["yoy_pct"]) for r in g["series"]]
    assert yoy == [None, D(20), D(25), D(20)]
    assert D(g["cagr"]["pct"]) == D("21.6") and all(r["evidence_id"] for r in g["series"])


def test_capex_is_shown_as_a_positive_spend(seeded):
    co = company(seeded, {"CASH_FLOW.capital_expenditure": cr(-50, -60, -75, -90)})
    g = cf._flow(store(seeded, co), cf.CAPEX, "CONSOLIDATED", spend=True)
    assert [D(r["value"]) for r in g["series"]] == [D(v * CRORE) for v in (50, 60, 75, 90)]
    assert D(g["series"][-1]["yoy_pct"]) == D(20) and "cagr" not in g


def test_a_missing_line_says_why_and_is_not_zero(seeded):
    co = mk_company(seeded, "No Capex Co")
    g = cf._flow(store(seeded, co), cf.CAPEX, "CONSOLIDATED", spend=True)
    assert not g["available"] and "does not print the line" in g["reason"]


def test_conversion_is_read_by_year_and_over_the_years(seeded):
    co = company(seeded, {PAT: cr(100, 100, 100, 100), CFO: cr(120, 90, 60, 30)})
    c = cf.conversion(store(seeded, co), "CONSOLIDATED")
    assert [r["reading"] for r in c["years"]] == ["STRONG", "ADEQUATE", "WEAK", "POOR"]
    assert [D(r["ratio"]) for r in c["years"]] == [D("1.2"), D("0.9"), D("0.6"), D("0.3")]
    assert D(c["cumulative"]["ratio"]) == D("0.75") and c["cumulative"]["reading"] == "WEAK"
    assert len(c["cumulative"]["evidence_ids"]) == 8


def test_a_loss_year_has_no_ratio_and_a_gap(seeded):
    co = company(seeded, {PAT: cr(-20, 50), CFO: cr(10, 40)})
    c = cf.conversion(store(seeded, co), "CONSOLIDATED")
    assert c["years"][0]["ratio"] is None and "loss" in c["years"][0]["note"]
    assert D(c["years"][0]["gap"]) == D(-30 * CRORE)
    assert D(c["cumulative"]["ratio"]) == D("1.67")


def test_no_overlapping_years_means_no_conversion(seeded):
    co = company(seeded, {PAT: cr(100, None, None, None), CFO: cr(None, 90, None, None)})
    assert not cf.conversion(store(seeded, co), "CONSOLIDATED")["available"]


def test_the_readings_follow_the_stated_thresholds():
    assert cf.reading_for_accruals(D(-2)) == "CASH_BACKED"
    assert cf.reading_for_accruals(D("4.9")) == "NORMAL"
    assert cf.reading_for_accruals(D(5)) == "ELEVATED"
    assert cf.reading_for_accruals(D("9.9")) == "ELEVATED"
    assert cf.reading_for_accruals(D(10)) == "HIGH"
    assert cf.reading_for_conversion(D(1)) == "STRONG"
    assert cf.reading_for_conversion(D("0.49")) == "POOR"


def test_accruals_carry_the_reading_and_the_rule(seeded):
    co = company(
        seeded,
        {"ACCRUAL.accrual_ratio": [1.0, 4.0, 7.0, 12.0], "ACCRUAL.accruals": cr(10, 40, 70, 120)},
    )
    a = cf.accruals(store(seeded, co), "CONSOLIDATED")
    assert [r["reading"] for r in a["ratio"]["series"]] == ["NORMAL", "NORMAL", "ELEVATED", "HIGH"]
    assert "cash-backed" in a["ratio"]["rules"] and a["amount"]["available"]


def test_divergences_name_the_year_and_cite_the_evidence(seeded):
    co = company(seeded, {PAT: cr(100, 120, 140, 160), CFO: cr(110, 100, 40, -5)})
    found = cf.divergences(store(seeded, co), "CONSOLIDATED")
    kinds = {(d["fy"], d["kind"]) for d in found}
    assert ("FY2024", "DIVERGING_TRENDS") in kinds
    assert ("FY2025", "CASH_LAGS_PROFIT") in kinds
    assert ("FY2026", "PROFIT_WITHOUT_CASH") in kinds
    assert all(d["evidence_ids"] for d in found)


def test_cash_outpacing_profit_is_also_reported(seeded):
    co = company(seeded, {PAT: cr(100, 80), CFO: cr(90, 130)})
    kinds = [d["kind"] for d in cf.divergences(store(seeded, co), "CONSOLIDATED")]
    assert kinds == ["CASH_OUTPACES_PROFIT"]


def test_steady_cash_generation_raises_no_flag(seeded):
    co = company(seeded, {PAT: cr(100, 110, 120, 130), CFO: cr(110, 120, 135, 150)})
    assert cf.divergences(store(seeded, co), "CONSOLIDATED") == []


def test_working_capital_lines_are_listed_as_printed_for_the_latest_year(seeded):
    co = company(
        seeded,
        {
            CFO: cr(100, 120),
            "CASH_FLOW.change_in_trade_receivables": cr(-5, -30),
            "CASH_FLOW.income_taxes_paid": cr(-20, -25),
        },
    )
    wc = cf.working_capital(store(seeded, co), "CONSOLIDATED")
    assert {i["line"] for i in wc["items"]} == {"Trade receivables", "Income taxes paid"}
    assert "need not add up" in wc["note"] and wc["evidence_ids"]


def test_without_those_lines_there_is_no_working_capital_block(seeded):
    co = company(seeded, {CFO: cr(100, 120)})
    assert cf.working_capital(store(seeded, co), "CONSOLIDATED") is None


def test_the_other_accounting_basis_is_never_mixed_in(seeded):
    co = mk_company(seeded, "Basis Co")
    put(seeded, co, CFO, cr(100, 110), basis="STANDALONE", years=YEARS[2:])
    put(seeded, co, CFO, cr(900, 950, 990, 999), basis="CONSOLIDATED")
    g = cf._flow(store(seeded, co), cf.CFO, "STANDALONE")
    assert len(g["series"]) == 2 and g["basis"] == "STANDALONE"


def test_the_peer_comparison_needs_three_peers_and_uses_the_same_year(seeded):
    ratio = "CASH.cfo_to_net_profit"
    own = company(seeded, {ratio: [0.8, 0.9, 1.0, 1.2]})
    group = [
        company(seeded, {ratio: [0.7, 0.8, 0.9 + i * 0.1, 0.9 + i * 0.1]}, f"P{i}")
        for i in range(4)
    ]
    own_ratio = cf._ratio(store(seeded, own), cf.BY_KEY["cfo_to_net_profit"], "CONSOLIDATED")
    stores = [(p, store(seeded, p)) for p in group]
    block = cf.peer_ratio(
        store(seeded, own), stores, "cfo_to_net_profit", "CONSOLIDATED", own_ratio
    )
    assert block["available"] and block["kind"] == "x" and block["benchmark"] == "PEER_GROUP"
    assert D(block["company"]) == D("1.2") and D(block["peer_group_median"]) == D("1.05")
    few = cf.peer_ratio(
        store(seeded, own), stores[:2], "cfo_to_net_profit", "CONSOLIDATED", own_ratio
    )
    assert not few["available"] and "fewer than 3" in few["reason"]


def test_a_bank_has_no_cash_flow_quality_analysis_and_says_why(seeded):
    co = company(seeded, {CFO: cr(100, 120, 150, 180)})
    out = cf.build(store(seeded, co), [], "BANK", "CONSOLIDATED")
    assert out["available"] is False and out["kind"] == "BANK" and "deposits" in out["reason"]


def test_the_whole_analysis_hangs_together_and_is_labelled(seeded):
    co = company(
        seeded,
        {
            CFO: cr(100, 120, 150, 180),
            PAT: cr(90, 100, 110, 130),
            "CASH_FLOW.capital_expenditure": cr(-40, -50, -60, -70),
            "CASH.free_cash_flow": cr(60, 70, 90, 110),
            "CASH.capex_to_depreciation": [1.2, 1.3, 1.4, 1.5],
            "CASH.cfo_to_net_profit": [1.11, 1.2, 1.36, 1.38],
            "ACCRUAL.accrual_ratio": [-0.5, -1.0, -2.0, -2.5],
            "ACCRUAL.accruals": cr(-10, -20, -40, -50),
        },
    )
    out = cf.build(store(seeded, co), [], "IT_SERVICES", "CONSOLIDATED")
    assert out["available"] and out["kind"] == "NON_FINANCIAL" and out["basis"] == "CONSOLIDATED"
    assert out["benchmark"]["kind"] == "PEER_GROUP" and out["benchmark"]["industry"] is None
    keys = {o["key"] for o in out["observations"]}
    assert {"cumulative_conversion", "accruals", "free_cash_flow", "capex_vs_depreciation"} <= keys
    for o in out["observations"]:
        assert o["kind"] == "INTERPRETATION" and o["evidence_ids"] and o["rule"]
    assert out["divergences"] == [] and out["peers"]["cfo_to_net_profit"]["available"] is False
    cum = next(o for o in out["observations"] if o["key"] == "cumulative_conversion")
    assert "strong cash conversion" in cum["text"]
    assert "industry cagr" not in json.dumps(out).lower()


def test_a_company_with_no_cash_flow_data_reports_nothing_rather_than_zeros(seeded):
    out = cf.build(store(seeded, mk_company(seeded, "Empty Co")), [], "IT_SERVICES", "CONSOLIDATED")
    assert out["available"] is False and out["observations"] == [] and out["divergences"] == []


def test_the_pdf_shows_cash_flow_and_earnings_quality(seeded):
    from tests.test_report_context import _axis, _flat

    co = company(
        seeded,
        {
            CFO: cr(100, 120, 150, 180),
            PAT: cr(90, 100, 110, 130),
            "CASH_FLOW.capital_expenditure": cr(-40, -50, -60, -70),
            "CASH.free_cash_flow": cr(60, 70, 90, 110),
            "CASH.cfo_to_net_profit": [1.11, 1.2, 1.36, 1.38],
            "ACCRUAL.accrual_ratio": [-0.5, -1.0, -2.0, -2.5],
        },
    )
    content = _axis()
    content["analysis"]["cashflow"] = json.loads(
        json.dumps(cf.build(store(seeded, co), [], "IT_SERVICES", "CONSOLIDATED"))
    )
    flat = _flat(content)
    assert "Cash flow and earnings quality" in flat and "Profit and cash by year" in flat
    assert "Operating cash flow / net profit" in flat and "peer group" in flat
    assert "Not available" in flat  # the ratios with no stored inputs are listed with reasons


def test_a_bank_or_empty_cash_flow_analysis_leaves_no_section():
    from tests.test_report_context import _axis, _flat

    for payload in (
        {"available": False, "kind": "BANK", "reason": "a bank's cash flow is not read this way"},
        {"available": False, "reason": "none stored"},
    ):
        content = _axis()
        content["analysis"]["cashflow"] = payload
        assert "Cash flow and earnings quality" not in _flat(content)


def test_a_total_over_a_gap_in_the_years_is_not_a_total(seeded):
    ends = [date(2023, 3, 31), date(2026, 3, 31)]
    co = mk_company(seeded, "Gap Total Co")
    put(seeded, co, PAT, cr(100, 100), years=ends)
    put(seeded, co, CFO, cr(3000, 100), years=ends)
    c = cf.conversion(store(seeded, co), "CONSOLIDATED")
    assert c["available"] and "cumulative" not in c, "two years three years apart are not a run"


def test_the_cumulative_total_uses_the_latest_unbroken_run(seeded):
    co = company(seeded, {PAT: cr(100, None, 100, 100), CFO: cr(900, 50, 90, 90)})
    c = cf.conversion(store(seeded, co), "CONSOLIDATED")
    assert (c["cumulative"]["from_fy"], c["cumulative"]["to_fy"]) == ("FY2025", "FY2026")
    assert D(c["cumulative"]["ratio"]) == D("0.9")


def test_a_different_reading_on_the_other_basis_is_shown_but_never_substituted(seeded):
    co = mk_company(seeded, "Finance Arm Co")
    put(seeded, co, PAT, cr(100, 100, 100), basis="CONSOLIDATED", years=YEARS[1:])
    put(seeded, co, CFO, cr(-300, -200, 10), basis="CONSOLIDATED", years=YEARS[1:])
    put(seeded, co, PAT, cr(80, 90, 100), basis="STANDALONE", years=YEARS[1:])
    put(seeded, co, CFO, cr(100, 110, 120), basis="STANDALONE", years=YEARS[1:])
    out = cf.build(store(seeded, co), [], "AUTO", "CONSOLIDATED")
    assert out["conversion"]["cumulative"]["reading"] == "POOR" and out["basis"] == "CONSOLIDATED"
    other = out["other_basis"]
    assert other["basis"] == "STANDALONE" and other["reading"] == "STRONG"
    assert "lending" in other["note"] and "unchanged" in other["note"] and other["evidence_ids"]


def test_no_cross_check_when_both_bases_read_the_same(seeded):
    co = mk_company(seeded, "Same Reading Co")
    for basis in ("CONSOLIDATED", "STANDALONE"):
        put(seeded, co, PAT, cr(100, 100), basis=basis, years=YEARS[2:])
        put(seeded, co, CFO, cr(110, 120), basis=basis, years=YEARS[2:])
    assert cf.build(store(seeded, co), [], "AUTO", "CONSOLIDATED")["other_basis"] is None
