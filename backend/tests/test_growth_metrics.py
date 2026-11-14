# ruff: noqa: F811
"""Gross profit, EBITDA growth and CAGRs in the calculation engine."""

from datetime import date
from decimal import Decimal as D

import pytest

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

FY23, FY24 = date(2023, 3, 31), date(2024, 3, 31)
YEARS = (FY23, FY24, FY25, FY26)


def year(session, co, d, end, *, revenue, pbt, profit, finance=None, dep=None, goods=None):
    non_financial_year(
        session, co, d, end, revenue=revenue, other=10, pbt=pbt, tax=pbt - profit, profit=profit,
        cap=100, reserves=400, assets=2000, eps=10,
    )  # fmt: skip
    if finance is not None:
        fact(session, co, "INCOME", "finance_costs", finance, end=end, document=d)
    if dep is not None:
        fact(session, co, "INCOME", "depreciation_and_amortisation", dep, end=end, document=d)
    for item, v in (goods or {}).items():
        fact(session, co, "INCOME", item, v, end=end, document=d)


def four_years(session, co, *, ebitda_base=100):
    d = doc(session, co)
    revenues = (1000, 1100, 1250, 1400)
    for i, end in enumerate(YEARS):
        # pbt + finance + depreciation = EBITDA that grows 10, 12, 14 per cent
        ebitda = round(ebitda_base * (1.1, 1.232, 1.40448)[i - 1]) if i else ebitda_base
        year(
            session, co, d, end, revenue=revenues[i], pbt=ebitda - 30, profit=ebitda - 60,
            finance=10, dep=20,
        )  # fmt: skip
    return d


# ---- gross profit -----------------------------------------------------------------------------------


def test_gross_profit_is_revenue_less_the_cost_of_goods_as_printed(session, co):
    d = doc(session, co)
    year(
        session, co, d, FY26, revenue=1000, pbt=200, profit=150,
        goods={
            "cost_of_materials_consumed": 400,
            "purchases_of_stock_in_trade": 100,
            "changes_in_inventories": -20,  # inventories grew: the cost of goods sold is lower
        },
    )  # fmt: skip
    run(session, co)
    assert value(session, co, "INCOME.gross_profit", FY26) == D(520) * CR
    assert near(value(session, co, "MARGIN.gross_margin", FY26), 52)


def test_a_manufacturer_without_stock_in_trade_uses_materials_and_inventories(session, co):
    d = doc(session, co)
    year(
        session, co, d, FY26, revenue=1000, pbt=200, profit=150,
        goods={"cost_of_materials_consumed": 600, "changes_in_inventories": 30},
    )  # fmt: skip
    run(session, co)
    assert value(session, co, "INCOME.gross_profit", FY26) == D(370) * CR


def test_a_trader_uses_purchases_and_inventories(session, co):
    d = doc(session, co)
    year(
        session, co, d, FY26, revenue=1000, pbt=200, profit=150,
        goods={"purchases_of_stock_in_trade": 700, "changes_in_inventories": -50},
    )  # fmt: skip
    run(session, co)
    assert value(session, co, "INCOME.gross_profit", FY26) == D(350) * CR


def test_materials_alone_is_a_last_resort_variant(session, co):
    d = doc(session, co)
    year(
        session,
        co,
        d,
        FY26,
        revenue=1000,
        pbt=200,
        profit=150,
        goods={"cost_of_materials_consumed": 650},
    )
    run(session, co)
    assert value(session, co, "INCOME.gross_profit", FY26) == D(350) * CR


def test_a_services_company_has_no_gross_profit_and_says_why(session, co):
    d = doc(session, co)
    year(session, co, d, FY26, revenue=1000, pbt=200, profit=150)
    report = run(session, co)
    assert calc(session, co, "INCOME.gross_profit", FY26) is None
    assert calc(session, co, "MARGIN.gross_margin", FY26) is None
    assert gaps(session, report, "INCOME.gross_profit"), "the missing inputs are listed, not hidden"


def test_the_variant_used_is_recorded_with_the_figure(session, co):
    d = doc(session, co)
    year(
        session, co, d, FY26, revenue=1000, pbt=200, profit=150,
        goods={"cost_of_materials_consumed": 600, "changes_in_inventories": 30},
    )  # fmt: skip
    run(session, co)
    record = calc(session, co, "INCOME.gross_profit", FY26)
    assert "inventories" in record.expression and "purchases" not in record.expression
    assert "manufacturer" in str(record.details)


# ---- EBITDA growth and CAGR ----------------------------------------------------------------------------------


def test_ebitda_growth_and_cagr_come_from_the_stored_ebitda(session, co):
    four_years(session, co)
    run(session, co)
    e = {end: value(session, co, "INCOME.ebitda", end) / CR for end in YEARS}
    assert e[FY23] == 100 and e[FY26] > e[FY25] > e[FY24] > e[FY23]
    yoy = value(session, co, "GROWTH.ebitda_yoy", FY26)
    assert near(yoy, (e[FY26] / e[FY25] - 1) * 100, "0.0001")
    cagr = value(session, co, "GROWTH.ebitda_cagr_3y", FY26)
    assert near(cagr, ((e[FY26] / e[FY23]) ** (D(1) / 3) - 1) * 100, "0.0001")


def test_revenue_cagr_over_three_years(session, co):
    four_years(session, co)
    run(session, co)
    cagr = value(session, co, "GROWTH.revenue_cagr_3y", FY26)
    assert near(cagr, ((D(1400) / D(1000)) ** (D(1) / 3) - 1) * 100, "0.0001")


def test_no_cagr_when_the_starting_year_is_missing(session, co):
    d = doc(session, co)
    for end, rev in ((FY24, 1100), (FY25, 1250), (FY26, 1400)):  # three years only
        year(session, co, d, end, revenue=rev, pbt=170, profit=140, finance=10, dep=20)
    report = run(session, co)
    assert calc(session, co, "GROWTH.revenue_cagr_3y", FY26) is None
    assert gaps(session, report, "GROWTH.revenue_cagr_3y")
    assert calc(session, co, "GROWTH.revenue_yoy", FY26) is not None  # the shorter measure exists


def test_growth_from_a_loss_or_zero_base_is_not_calculated(session, co):
    d = doc(session, co)
    year(session, co, d, FY25, revenue=1000, pbt=-20, profit=-40, finance=10, dep=20)  # EBITDA = 10
    year(session, co, d, FY26, revenue=1100, pbt=100, profit=60, finance=10, dep=20)
    year(session, co, d, FY24, revenue=900, pbt=-60, profit=-80, finance=10, dep=20)  # EBITDA = -30
    run(session, co)
    assert calc(session, co, "GROWTH.ebitda_yoy", FY25) is None, "the base year EBITDA is negative"
    assert calc(session, co, "GROWTH.ebitda_yoy", FY26) is not None


def test_ebitda_growth_needs_both_years_on_the_same_basis(session, co):
    from app.models import Basis

    d = doc(session, co)
    year(session, co, d, FY25, revenue=1000, pbt=150, profit=100, finance=10, dep=20)
    non_financial_year(
        session, co, d, FY26, revenue=1100, other=10, pbt=170, tax=30, profit=140, cap=100,
        reserves=400, assets=2000, eps=10, basis=Basis.STANDALONE,
    )  # fmt: skip
    fact(session, co, "INCOME", "finance_costs", 10, end=FY26, document=d, basis=Basis.STANDALONE)
    fact(
        session,
        co,
        "INCOME",
        "depreciation_and_amortisation",
        20,
        end=FY26,
        document=d,
        basis=Basis.STANDALONE,
    )
    run(session, co)
    assert calc(session, co, "GROWTH.ebitda_yoy", FY26, basis="CONSOLIDATED") is None
    assert calc(session, co, "GROWTH.ebitda_yoy", FY26, basis="STANDALONE") is None


def test_bank_profiles_get_no_industrial_ebitda_growth(session, co):
    pytest.importorskip("sqlalchemy")
    from app.calc.catalogue import BY_CODE

    for code in ("GROWTH.ebitda_yoy", "GROWTH.ebit_yoy", "GROWTH.gross_profit_yoy"):
        assert BY_CODE[code].profile == "NONFIN"
    for code in (
        "INCOME.gross_profit",
        "MARGIN.gross_margin",
        "INCOME.ebitda",
        "MARGIN.ebitda_margin",
    ):
        assert BY_CODE[code].profile == "NONFIN"


def test_the_calculate_fingerprint_changes_with_the_catalogue():
    """New or changed metrics must reach companies whose inputs did not change."""
    from app.calc import catalogue
    from app.pipeline.stages import _hash

    def digest(metrics):
        return _hash([repr(m) for m in metrics])

    full = catalogue.CATALOGUE
    assert digest(full) == digest(full)
    assert digest(full) != digest(full[:-1])


def test_incidental_stock_in_trade_does_not_make_a_services_company_a_goods_company(session, co):
    """A services group with a small trading line has no gross profit, inventory or payable days."""
    d = doc(session, co)
    year(
        session, co, d, FY26, revenue=1000, pbt=200, profit=150,
        goods={"purchases_of_stock_in_trade": 7, "changes_in_inventories": 1},
    )  # fmt: skip
    fact(session, co, "BALANCE_SHEET", "inventories", 50, end=FY26, document=d)
    fact(session, co, "BALANCE_SHEET", "current_trade_payables", 90, end=FY26, document=d)
    report = run(session, co)
    for code in (
        "INCOME.gross_profit", "MARGIN.gross_margin", "WORKING_CAPITAL.inventory_days",
        "WORKING_CAPITAL.payable_days", "WORKING_CAPITAL.cash_conversion_cycle_days",
    ):  # fmt: skip
        assert calc(session, co, code, FY26) is None, code
    reasons = " ".join(g.reason for g in gaps(session, report, "INCOME.gross_profit"))
    assert "incidental" in reasons and "0.8%" in reasons


def test_a_real_goods_cost_at_the_threshold_still_gives_a_gross_profit(session, co):
    d = doc(session, co)
    year(
        session,
        co,
        d,
        FY26,
        revenue=1000,
        pbt=200,
        profit=150,
        goods={"cost_of_materials_consumed": 100},
    )
    run(session, co)
    assert value(session, co, "INCOME.gross_profit", FY26) == D(900) * CR


def test_a_value_stored_before_a_gate_tightened_is_withdrawn_not_left_live(session, co):
    from sqlalchemy import select

    from app.models import EvidenceRecord, EvidenceStatus

    d = doc(session, co)
    year(
        session,
        co,
        d,
        FY26,
        revenue=1000,
        pbt=200,
        profit=150,
        goods={"cost_of_materials_consumed": 400},
    )
    run(session, co)
    assert value(session, co, "INCOME.gross_profit", FY26) == D(600) * CR
    # the filing is re-read and the goods line turns out to be a small trading item
    row = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == co.id,
            EvidenceRecord.metric == "INCOME.cost_of_materials_consumed",
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )
    assert row is not None
    row.value = D(5) * CR
    session.flush()
    run(session, co)
    assert calc(session, co, "INCOME.gross_profit", FY26) is None
    old = session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == co.id,
            EvidenceRecord.metric == "INCOME.gross_profit",
            EvidenceRecord.status == EvidenceStatus.WITHDRAWN,
        )
    ).all()
    assert old and "conditions no longer met" in old[0].status_reason
