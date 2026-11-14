# ruff: noqa: F811, E501
"""Working capital, financial health, repayment, returns and DuPont (research layer)."""

import json
from datetime import date
from decimal import Decimal as D

from app.research import health as hl
from app.research import readiness as rd
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

YEARS = [date(2023, 3, 31), date(2024, 3, 31), date(2025, 3, 31), date(2026, 3, 31)]
CRORE = 10_000_000
UNITS = {}
for code in (
    "WORKING_CAPITAL.receivable_days", "WORKING_CAPITAL.inventory_days", "WORKING_CAPITAL.payable_days",
    "WORKING_CAPITAL.operating_cycle_days", "WORKING_CAPITAL.cash_conversion_cycle_days",
):  # fmt: skip
    UNITS[code] = "days"
for code in (
    "LEVERAGE.debt_to_equity", "LEVERAGE.net_debt_to_equity", "LEVERAGE.net_debt_to_ebitda",
    "RATIO.interest_cover", "LIQUIDITY.current_ratio", "LIQUIDITY.cash_to_short_term_debt",
    "LIQUIDITY.cfo_to_debt", "LIQUIDITY.quick_ratio", "LEVERAGE.debt_to_ebitda",
    "RATIO.ebitda_interest_cover",
):  # fmt: skip
    UNITS[code] = "x"
for code in (
    "RETURN.roe_average",
    "RETURN.roe_closing",
    "RETURN.roce",
    "RETURN.roa_average",
    "RETURN.roa_closing",
):
    UNITS[code] = "percent"


def cr(*vals):
    return [None if v is None else v * CRORE for v in vals]


def put(session, co, metric, values, *, basis="CONSOLIDATED", years=YEARS):
    unit = UNITS.get(metric, "inr")
    for end, v in zip(years, values, strict=False):
        if v is not None:
            evidence(session, co, metric, v, end=end, basis=basis, unit=unit)


def store(session, co):
    return rd.load_store(session, co.id, set(hl.NAMES))


def company(session, series, name="Health Co"):
    co = mk_company(session, name)
    for metric, values in series.items():
        put(session, co, metric, values)
    return co


# ---- working capital ------------------------------------------------------------------------------------------


def test_the_cycles_are_reported_with_the_change_in_days_and_a_reading(seeded):
    co = company(
        seeded,
        {
            "WORKING_CAPITAL.receivable_days": [40, 45, 50, 55],
            "WORKING_CAPITAL.inventory_days": [60, 62, 64, 66],
            "WORKING_CAPITAL.payable_days": [50, 50, 50, 50],
            "WORKING_CAPITAL.operating_cycle_days": [100, 107, 114, 121],
            "WORKING_CAPITAL.cash_conversion_cycle_days": [50, 57, 64, 71],
        },
    )
    wc = hl.working_capital(store(seeded, co), "CONSOLIDATED")
    ccc = wc["days"]["cash_conversion_cycle_days"]
    assert D(ccc["change_days"]) == D(21) and ccc["direction"] == "LENGTHENING"
    assert wc["days"]["payable_days"]["direction"] == "STABLE"
    (obs,) = wc["observations"]
    assert "lengthened by 21 days between FY2023 and FY2026 (50 to 71 days)" in obs["text"]
    assert obs["kind"] == "INTERPRETATION" and len(obs["evidence_ids"]) == 2


def test_the_cycle_reading_thresholds():
    assert hl.read_cycle_change(D(5)) == "LENGTHENING"
    assert hl.read_cycle_change(D("4.9")) == "STABLE"
    assert hl.read_cycle_change(D(-5)) == "SHORTENING"


def test_a_services_company_shows_receivable_days_and_says_why_the_rest_are_missing(seeded):
    co = company(seeded, {"WORKING_CAPITAL.receivable_days": [70, 72, 75, 78]})
    wc = hl.working_capital(store(seeded, co), "CONSOLIDATED")
    assert wc["available"] and wc["days"]["receivable_days"]["available"]
    assert (
        not wc["days"]["inventory_days"]["available"]
        and "not calculated" in wc["days"]["inventory_days"]["reason"]
    )


def test_no_working_capital_balances_means_not_available_with_a_reason(seeded):
    wc = hl.working_capital(store(seeded, mk_company(seeded, "Empty Co")), "CONSOLIDATED")
    assert wc["available"] is False and "services company" in wc["reason"]


# ---- leverage, liquidity, repayment ----------------------------------------------------------------------------


def test_readings_follow_the_stated_thresholds():
    assert (
        hl.read_interest_cover(D("1.4")) == "STRETCHED"
        and hl.read_interest_cover(D(3)) == "COMFORTABLE"
    )
    assert (
        hl.read_current_ratio(D("0.9")) == "TIGHT"
        and hl.read_current_ratio(D("1.5")) == "COMFORTABLE"
    )
    assert hl.read_net_debt_ebitda(D(-1)) == "NET_CASH" and hl.read_net_debt_ebitda(D(3)) == "HIGH"
    assert (
        hl.read_net_debt_ebitda(D("0.5")) == "LOW" and hl.read_net_debt_ebitda(D(2)) == "MODERATE"
    )
    assert (
        hl.read_repayment_years(D(2)) == "COMFORTABLE"
        and hl.read_repayment_years(D(5)) == "STRETCHED"
    )


def test_leverage_and_liquidity_carry_readings_and_net_cash_is_labelled(seeded):
    co = company(
        seeded,
        {
            "RATIO.interest_cover": [8, 6, 3.5, 1.2],
            "LIQUIDITY.current_ratio": [2.0, 1.6, 1.1, 0.9],
            "LEVERAGE.net_debt": cr(-500, -300, 100, 400),
            "LEVERAGE.net_debt_to_ebitda": [-1.0, -0.5, 0.4, 2.5],
            "LEVERAGE.total_borrowings": cr(200, 300, 500, 800),
        },
    )
    lev = hl.leverage_liquidity(store(seeded, co), "CONSOLIDATED")
    assert [r["reading"] for r in lev["ratios"]["interest_cover"]["series"]] == [
        "COMFORTABLE",
        "COMFORTABLE",
        "COMFORTABLE",
        "STRETCHED",
    ]
    assert [r["reading"] for r in lev["ratios"]["current_ratio"]["series"]] == [
        "COMFORTABLE",
        "COMFORTABLE",
        "ADEQUATE",
        "TIGHT",
    ]
    assert [r["position"] for r in lev["net_debt"]["series"]] == [
        "NET_CASH",
        "NET_CASH",
        "NET_DEBT",
        "NET_DEBT",
    ]
    assert lev["ratios"]["net_debt_to_ebitda"]["series"][-1]["reading"] == "MODERATE"
    assert {o["key"] for o in lev["observations"]} == {
        "net_debt_to_ebitda",
        "interest_cover",
        "current_ratio",
    }
    assert all(o["evidence_ids"] and o["rule"] for o in lev["observations"])


def test_repayment_is_years_of_operating_cash_flow(seeded):
    co = company(
        seeded,
        {
            "LEVERAGE.total_borrowings": cr(600, 600, 0, 500),
            "CASH_FLOW.net_cash_from_operating_activities": cr(200, 100, 150, -20),
        },
    )
    rep = hl.repayment(store(seeded, co), "CONSOLIDATED", {})
    status = [(r["fy"], r["status"], r.get("years_to_repay")) for r in rep["years"]]
    assert status[0][1] == "OK" and D(status[0][2]) == D(3) and D(status[1][2]) == D(6)
    assert status[2][1] == "NO_BORROWINGS" and status[3][1] == "NO_OPERATING_CASH"
    assert rep["years"][0]["reading"] == "MODERATE" and rep["years"][1]["reading"] == "STRETCHED"
    assert "zero or negative" in rep["observations"][0]["text"]


def test_no_borrowings_is_stated_not_treated_as_missing(seeded):
    co = company(
        seeded,
        {
            "LEVERAGE.total_borrowings": cr(0, 0),
            "CASH_FLOW.net_cash_from_operating_activities": cr(100, 120),
        },
    )
    rep = hl.repayment(store(seeded, co), "CONSOLIDATED", {})
    assert "no borrowings" in rep["observations"][0]["text"]


def test_repayment_without_the_inputs_is_not_available(seeded):
    rep = hl.repayment(store(seeded, mk_company(seeded, "Bare Co")), "CONSOLIDATED", {})
    assert rep["available"] is False and "not stored" in rep["reason"]


# ---- DuPont ----------------------------------------------------------------------------------------------------


def dupont_company(session, name="DuPont Co", *, with_ebit=False):
    series = {
        "INCOME.attributable_profit": cr(100, 120, 150, 200),
        "INCOME.revenue_from_operations": cr(1000, 1100, 1250, 1500),
        "BALANCE_SHEET.total_assets": cr(900, 1000, 1100, 1300),
        "BALANCE.owners_equity": cr(400, 450, 520, 600),
    }
    if with_ebit:
        series["INCOME.profit_before_tax"] = cr(130, 160, 200, 270)
        series["INCOME.ebit"] = cr(150, 175, 215, 285)
    return company(session, series, name)


def test_dupont_factors_multiply_back_to_the_roe_on_average_balances(seeded):
    dp = hl.dupont(store(seeded, dupont_company(seeded)), "CONSOLIDATED", bank=False)
    assert dp["available"] and [r["fy"] for r in dp["years"]] == ["FY2024", "FY2025", "FY2026"]
    last = dp["years"][-1]
    # FY2026: average assets (1100+1300)/2 = 1200, average equity (520+600)/2 = 560
    assert D(last["net_margin"]) == D("13.33") and D(last["asset_turnover"]) == D("1.250")
    assert D(last["equity_multiplier"]) == D("2.143") and D(last["roe"]) == D("35.71")
    assert all(abs(D(r["product_check"])) < D("0.05") for r in dp["years"])
    assert all(len(r["evidence_ids"]) == 6 for r in dp["years"])


def test_the_dupont_driver_shares_add_up_and_name_the_leading_factor(seeded):
    dp = hl.dupont(store(seeded, dupont_company(seeded)), "CONSOLIDATED", bank=False)
    d = dp["driver"]
    assert (d["from_fy"], d["to_fy"]) == ("FY2024", "FY2026")
    assert abs(sum(d["shares_pct"].values()) - 100) <= 2
    assert d["leading"] in d["shares_pct"] and "log decomposition" in d["method"]


def test_the_five_step_form_appears_only_where_ebit_is_stored(seeded):
    plain = hl.dupont(store(seeded, dupont_company(seeded)), "CONSOLIDATED", bank=False)
    assert not any("five_step" in r for r in plain["years"])
    full = hl.dupont(
        store(seeded, dupont_company(seeded, "Five Co", with_ebit=True)), "CONSOLIDATED", bank=False
    )
    f = full["years"][-1]["five_step"]
    assert D(f["tax_burden"]) == D("0.741") and D(f["interest_burden"]) == D("0.947")
    assert D(f["ebit_margin"]) == D("19.00")


def test_dupont_needs_two_consecutive_balance_sheets(seeded):
    co = company(
        seeded,
        {
            "INCOME.attributable_profit": cr(100),
            "INCOME.revenue_from_operations": cr(1000),
            "BALANCE_SHEET.total_assets": cr(900),
            "BALANCE.owners_equity": cr(400),
        },
    )
    dp = hl.dupont(store(seeded, co), "CONSOLIDATED", bank=False)
    assert dp["available"] is False and "two consecutive year ends" in dp["reason"]


def test_a_bank_dupont_uses_total_income(seeded):
    co = company(
        seeded,
        {
            "INCOME.attributable_profit": cr(100, 130),
            "INCOME.total_income": cr(800, 1000),
            "BALANCE_SHEET.total_assets": cr(5000, 6000),
            "BALANCE.owners_equity": cr(500, 600),
        },
    )
    dp = hl.dupont(store(seeded, co), "CONSOLIDATED", bank=True)
    assert dp["sales_label"] == "total income" and D(dp["years"][0]["net_margin"]) == D("13.00")


def test_a_loss_makes_no_driver_decomposition(seeded):
    co = company(
        seeded,
        {
            "INCOME.attributable_profit": cr(-50, 100, 120),
            "INCOME.revenue_from_operations": cr(1000, 1100, 1200),
            "BALANCE_SHEET.total_assets": cr(900, 1000, 1100),
            "BALANCE.owners_equity": cr(400, 450, 500),
        },
    )
    dp = hl.dupont(store(seeded, co), "CONSOLIDATED", bank=False)
    assert dp["available"] and dp["driver"] is not None


# ---- the whole -------------------------------------------------------------------------------------------------


def test_a_bank_gets_returns_and_dupont_but_no_working_capital_or_net_debt(seeded):
    co = company(
        seeded,
        {
            "INCOME.attributable_profit": cr(100, 130),
            "INCOME.total_income": cr(800, 1000),
            "BALANCE_SHEET.total_assets": cr(5000, 6000),
            "BALANCE.owners_equity": cr(500, 600),
            "RETURN.roe_average": [15, 16],
        },
    )
    out = hl.build(store(seeded, co), [], "BANK", "CONSOLIDATED")
    assert out["kind"] == "BANK" and out["available"] and "working_capital" not in out
    assert "net debt" in out["note"] and set(out["peers"]) == {"roe", "roa"}


def test_the_whole_analysis_is_labelled_peer_group_and_never_industry(seeded):
    co = dupont_company(seeded)
    for metric, values in {
        "RETURN.roe_average": [20, 25, 30, 35],
        "LIQUIDITY.current_ratio": [1.5, 1.4, 1.3, 1.2],
    }.items():
        put(seeded, co, metric, values)
    out = hl.build(store(seeded, co), [], "IT_SERVICES", "CONSOLIDATED")
    assert (
        out["available"] and out["kind"] == "NON_FINANCIAL" and out["benchmark"]["industry"] is None
    )
    assert set(out["peers"]) == set(hl.PEER_COMPARED_NONBANK)
    assert all(not b["available"] for b in out["peers"].values())
    assert "industry cagr" not in json.dumps(out).lower()


def test_peer_comparison_of_a_ratio_needs_three_peers(seeded):
    own = company(seeded, {"RETURN.roe_average": [10, 12, 14, 18]})
    group = [
        company(seeded, {"RETURN.roe_average": [9, 10, 11, 10 + i * 2]}, f"R{i}") for i in range(4)
    ]  # FY2026: 10, 12, 14, 16
    own_all = {"roe": hl.cf._ratio(store(seeded, own), hl.BY_KEY["roe"], "CONSOLIDATED")}
    block = hl.peers_block(
        store(seeded, own),
        [(p, store(seeded, p)) for p in group],
        ("roe",),
        "CONSOLIDATED",
        own_all,
    )["roe"]
    assert block["available"] and block["kind"] == "pct"
    assert D(block["peer_group_median"]) == D(13) and D(block["difference_pp"]) == D(5)


def test_the_pdf_shows_working_capital_and_health(seeded):
    from tests.test_report_context import _axis, _flat

    co = dupont_company(seeded)
    for metric, values in {
        "WORKING_CAPITAL.receivable_days": [40, 45, 50, 55],
        "WORKING_CAPITAL.cash_conversion_cycle_days": [50, 57, 64, 71],
        "RATIO.interest_cover": [8, 6, 3.5, 4],
        "RETURN.roe_average": [20, 25, 30, 35],
    }.items():
        put(seeded, co, metric, values)
    content = _axis()
    content["analysis"]["health"] = json.loads(
        json.dumps(hl.build(store(seeded, co), [], "IT_SERVICES", "CONSOLIDATED"))
    )
    flat = _flat(content)
    assert (
        "Working capital and financial health" in flat
        and "DuPont analysis of return on equity" in flat
    )
    assert "Receivable days" in flat and "Not available" in flat


def test_an_empty_health_analysis_leaves_no_section():
    from tests.test_report_context import _axis, _flat

    content = _axis()
    content["analysis"]["health"] = {"available": False, "reason": "none stored"}
    assert "Working capital and financial health" not in _flat(content)
