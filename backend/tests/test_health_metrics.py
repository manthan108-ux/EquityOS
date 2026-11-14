# ruff: noqa: F811
"""Working-capital days, cycles, quick ratio, net debt and repayment cover in the calculation engine."""

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
from tests.test_evidence_db import CR, FY26


def year(session, co, d, *, revenue=3650, profit=300, goods=None, balance=None, cfo=None, **kw):
    non_financial_year(
        session, co, d, FY26, revenue=revenue, other=10, pbt=profit + 100, tax=100, profit=profit,
        cap=100, reserves=900, assets=4000, eps=10,
    )  # fmt: skip
    for item, v in (goods or {}).items():
        fact(session, co, "INCOME", item, v, end=FY26, document=d)
    for item, v in (balance or {}).items():
        fact(session, co, "BALANCE_SHEET", item, v, end=FY26, document=d)
    if cfo is not None:
        fact(
            session,
            co,
            "CASH_FLOW",
            "net_cash_from_operating_activities",
            cfo,
            end=FY26,
            document=d,
        )
    for item, v in kw.items():
        fact(session, co, "INCOME", item, v, end=FY26, document=d)


GOODS = {"cost_of_materials_consumed": 2000, "changes_in_inventories": 0}  # cost of goods 2,000


def test_days_are_measured_on_closing_balances_and_a_365_day_year(session, co):
    d = doc(session, co)
    year(
        session, co, d, goods=GOODS,
        balance={
            "current_trade_receivables": 300, "inventories": 500, "current_trade_payables": 400,
        },
    )  # fmt: skip
    run(session, co)
    assert near(value(session, co, "WORKING_CAPITAL.receivable_days", FY26), 30)
    assert near(value(session, co, "WORKING_CAPITAL.inventory_days", FY26), D(500) / 2000 * 365)
    assert near(value(session, co, "WORKING_CAPITAL.payable_days", FY26), D(400) / 2000 * 365)


def test_the_cycles_add_and_subtract_the_days_exactly(session, co):
    d = doc(session, co)
    year(
        session, co, d, goods=GOODS,
        balance={
            "current_trade_receivables": 300, "inventories": 500, "current_trade_payables": 400,
        },
    )  # fmt: skip
    run(session, co)
    r = value(session, co, "WORKING_CAPITAL.receivable_days", FY26)
    i = value(session, co, "WORKING_CAPITAL.inventory_days", FY26)
    p = value(session, co, "WORKING_CAPITAL.payable_days", FY26)
    assert near(value(session, co, "WORKING_CAPITAL.operating_cycle_days", FY26), r + i)
    assert near(value(session, co, "WORKING_CAPITAL.cash_conversion_cycle_days", FY26), r + i - p)


def test_payables_printed_as_two_lines_are_added(session, co):
    d = doc(session, co)
    year(
        session, co, d, goods=GOODS,
        balance={"trade_payables_msme": 50, "trade_payables_others": 350},
    )  # fmt: skip
    run(session, co)
    assert near(value(session, co, "WORKING_CAPITAL.payable_days", FY26), D(400) / 2000 * 365)


def test_a_services_company_has_receivable_days_but_no_inventory_or_payable_days(session, co):
    d = doc(session, co)
    year(session, co, d, balance={"current_trade_receivables": 730})
    report = run(session, co)
    assert near(value(session, co, "WORKING_CAPITAL.receivable_days", FY26), 73)
    for code in ("WORKING_CAPITAL.inventory_days", "WORKING_CAPITAL.cash_conversion_cycle_days"):
        assert calc(session, co, code, FY26) is None
        assert gaps(session, report, code), code


def test_quick_ratio_excludes_inventories(session, co):
    d = doc(session, co)
    year(
        session, co, d,
        balance={"total_current_assets": 1500, "inventories": 500, "total_current_liabilities": 800},
    )  # fmt: skip
    run(session, co)
    assert near(value(session, co, "LIQUIDITY.quick_ratio", FY26), D("1.25"))
    assert near(value(session, co, "LIQUIDITY.current_ratio", FY26), D("1.875"))


def test_net_debt_deducts_cash_bank_balances_and_investments_and_records_which(session, co):
    d = doc(session, co)
    year(
        session, co, d,
        balance={
            "borrowings": 1000, "cash_and_cash_equivalents": 200,
            "bank_balances_other_than_cash_equivalents": 100, "current_investments": 300,
        },
    )  # fmt: skip
    run(session, co)
    assert value(session, co, "LEVERAGE.net_debt", FY26) == D(400) * CR
    record = calc(session, co, "LEVERAGE.net_debt", FY26)
    assert "bank" in record.expression and "investments" in record.expression


def test_net_debt_falls_back_to_what_is_printed(session, co):
    d = doc(session, co)
    year(session, co, d, balance={"borrowings": 1000, "cash_and_cash_equivalents": 200})
    run(session, co)
    assert value(session, co, "LEVERAGE.net_debt", FY26) == D(800) * CR


def test_a_net_cash_company_has_negative_net_debt(session, co):
    d = doc(session, co)
    year(session, co, d, balance={"borrowings": 100, "cash_and_cash_equivalents": 600})
    run(session, co)
    assert value(session, co, "LEVERAGE.net_debt", FY26) == D(-500) * CR
    assert near(value(session, co, "LEVERAGE.net_debt_to_equity", FY26), D("-0.5"))


def test_net_debt_to_ebitda_and_coverage_use_the_stored_ebitda(session, co):
    d = doc(session, co)
    # pbt 400 + finance 50 + depreciation 150 = EBITDA 600
    year(
        session, co, d, finance_costs=50, depreciation_and_amortisation=150,
        balance={"borrowings": 1500, "cash_and_cash_equivalents": 300},
        cfo=450,
    )  # fmt: skip
    run(session, co)
    assert near(value(session, co, "LEVERAGE.net_debt_to_ebitda", FY26), 2)
    assert near(value(session, co, "LEVERAGE.debt_to_ebitda", FY26), D("2.5"))
    assert near(value(session, co, "RATIO.ebitda_interest_cover", FY26), 12)
    assert near(value(session, co, "RATIO.interest_cover", FY26), 9)
    assert near(value(session, co, "LIQUIDITY.cfo_to_debt", FY26), D("0.3"))


def test_short_term_debt_cover_counts_cash_and_liquid_investments(session, co):
    d = doc(session, co)
    year(
        session, co, d,
        balance={
            "cash_and_cash_equivalents": 200, "current_investments": 100, "current_borrowings": 150,
        },
    )  # fmt: skip
    run(session, co)
    assert near(value(session, co, "LIQUIDITY.cash_to_short_term_debt", FY26), 2)


def test_no_current_borrowings_means_no_short_term_cover_not_infinity(session, co):
    d = doc(session, co)
    year(session, co, d, balance={"cash_and_cash_equivalents": 200})
    run(session, co)
    assert calc(session, co, "LIQUIDITY.cash_to_short_term_debt", FY26) is None


def test_these_are_not_calculated_for_banks():
    from app.calc.catalogue import BY_CODE

    for code in (
        "WORKING_CAPITAL.receivable_days", "WORKING_CAPITAL.cash_conversion_cycle_days",
        "LIQUIDITY.quick_ratio", "LEVERAGE.net_debt", "LEVERAGE.net_debt_to_ebitda",
        "RATIO.ebitda_interest_cover", "LIQUIDITY.cash_to_short_term_debt",
    ):  # fmt: skip
        assert BY_CODE[code].profile == "NONFIN", code


def test_borrowings_printed_as_non_current_and_current_are_added(session, co):
    d = doc(session, co)
    year(
        session, co, d,
        balance={"non_current_borrowings": 700, "current_borrowings": 300, "cash_and_cash_equivalents": 200},
    )  # fmt: skip
    run(session, co)
    assert value(session, co, "LEVERAGE.total_borrowings", FY26) == D(1000) * CR
    assert value(session, co, "LEVERAGE.net_debt", FY26) == D(800) * CR
    assert near(value(session, co, "LEVERAGE.debt_to_equity", FY26), 1)


def test_a_single_borrowings_line_is_used_as_printed(session, co):
    d = doc(session, co)
    year(session, co, d, balance={"borrowings": 400})
    run(session, co)
    assert value(session, co, "LEVERAGE.total_borrowings", FY26) == D(400) * CR
    assert near(value(session, co, "LEVERAGE.debt_to_equity", FY26), D("0.4"))


def test_employee_cost_ratio_is_employee_benefits_over_revenue(session, co):
    d = doc(session, co)
    year(session, co, d, revenue=1000, employee_benefits_expense=550)
    run(session, co)
    assert near(value(session, co, "MARGIN.employee_cost_ratio", FY26), 55)
