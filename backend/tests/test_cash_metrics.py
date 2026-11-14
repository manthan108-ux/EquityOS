# ruff: noqa: F811
"""Cash conversion, free cash flow, capex ratios and accruals in the calculation engine."""

from decimal import Decimal as D

from tests.test_calc_db import (  # noqa: F401
    calc,
    co,
    doc,
    fact,
    gaps,
    near,
    non_financial_year,
    run,
    value,
)
from tests.test_evidence_db import CR, FY25, FY26


def year(session, co, d, end, *, cfo, capex=None, profit=150, revenue=1000, dep=None, ebitda=None):
    non_financial_year(
        session, co, d, end, revenue=revenue, other=10, pbt=profit + 50, tax=50, profit=profit,
        cap=100, reserves=400, assets=2000, eps=10,
    )  # fmt: skip
    fact(session, co, "CASH_FLOW", "net_cash_from_operating_activities", cfo, end=end, document=d)
    if capex is not None:
        fact(session, co, "CASH_FLOW", "capital_expenditure", capex, end=end, document=d)
    if dep is not None:
        fact(session, co, "INCOME", "depreciation_and_amortisation", dep, end=end, document=d)
        fact(session, co, "INCOME", "finance_costs", 10, end=end, document=d)


def test_cash_conversion_and_free_cash_flow_from_stored_lines(session, co):
    d = doc(session, co)
    year(session, co, d, FY26, cfo=180, capex=-60, dep=40)
    run(session, co)
    assert value(session, co, "CASH.free_cash_flow", FY26) == D(120) * CR
    assert near(value(session, co, "CASH.cfo_to_net_profit", FY26), D("1.2"))
    assert near(value(session, co, "CASH.cfo_margin", FY26), 18)
    assert near(value(session, co, "CASH.fcf_margin", FY26), 12)
    assert near(value(session, co, "CASH.fcf_to_net_profit", FY26), D("0.8"))
    assert near(value(session, co, "CASH.capex_to_revenue", FY26), 6)
    assert near(value(session, co, "CASH.capex_to_cfo", FY26), D(60) / 180 * 100)
    assert near(value(session, co, "CASH.capex_to_depreciation", FY26), D("1.5"))


def test_cash_conversion_against_ebitda(session, co):
    d = doc(session, co)
    year(session, co, d, FY26, cfo=180, capex=-60, dep=40)  # EBITDA = pbt 200 + 10 + 40 = 250
    run(session, co)
    assert near(value(session, co, "CASH.cfo_to_ebitda", FY26), D(180) / 250)


def test_without_a_capex_line_there_is_no_free_cash_flow_and_no_estimate(session, co):
    d = doc(session, co)
    year(session, co, d, FY26, cfo=180)
    report = run(session, co)
    for code in ("CASH.free_cash_flow", "CASH.fcf_margin", "CASH.capex_to_revenue"):
        assert calc(session, co, code, FY26) is None
        assert gaps(session, report, code), code
    assert near(value(session, co, "CASH.cfo_to_net_profit", FY26), D("1.2"))


def test_a_loss_gives_no_cash_to_profit_ratio_but_still_has_accruals(session, co):
    d = doc(session, co)
    year(session, co, d, FY26, cfo=40, capex=-30, profit=-20)
    run(session, co)
    assert calc(session, co, "CASH.cfo_to_net_profit", FY26) is None
    assert calc(session, co, "CASH.fcf_to_net_profit", FY26) is None
    assert value(session, co, "ACCRUAL.accruals", FY26) == D(-60) * CR


def test_accruals_are_profit_less_operating_cash_flow(session, co):
    d = doc(session, co)
    year(session, co, d, FY25, cfo=100, profit=140)
    year(session, co, d, FY26, cfo=90, profit=150)
    run(session, co)
    assert value(session, co, "ACCRUAL.accruals", FY26) == D(60) * CR
    # average assets (2000 and 2000): (150 - 90) / 2000
    assert near(value(session, co, "ACCRUAL.accrual_ratio", FY26), 3)


def test_accrual_ratio_falls_back_to_closing_assets_and_says_so(session, co):
    d = doc(session, co)
    year(session, co, d, FY26, cfo=90, profit=150)
    run(session, co)
    assert near(value(session, co, "ACCRUAL.accrual_ratio", FY26), 3)


def test_banks_have_no_industrial_cash_metrics(session, co):
    from app.calc.catalogue import BY_CODE

    for code in (
        "CASH.cfo_margin", "CASH.cfo_to_ebitda", "CASH.fcf_margin", "CASH.capex_to_cfo",
        "ACCRUAL.accruals", "ACCRUAL.accrual_ratio",
    ):  # fmt: skip
        assert BY_CODE[code].profile == "NONFIN"
