"""Calculation engine against a real database, with hand-calculated expectations."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.calc.adjust import adjusted_bars, rebuild_adjustments
from app.calc.catalogue import BANK_GROUP_NOTE
from app.calc.engine import calculate_company
from app.calc.technical import InsufficientHistory
from app.calc.technical_store import Skipped, recompute, register_technical
from app.core.database import get_session
from app.evidence.engine import validate_company
from app.main import create_app
from app.models import (
    AdjustmentKind,
    AdjustmentStatus,
    Basis,
    CalculationGap,
    Company,
    CorporateAction,
    EodPrice,
    EvidenceClass,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    FinancialFact,
    PeriodType,
    PriceAdjustment,
    Security,
    Severity,
)
from tests.test_evidence_db import CR, FY25, FY26, NOW, TODAY, cell, doc, fact

D = Decimal
SEP25, SEP26 = date(2025, 9, 30), date(2026, 9, 30)


@pytest.fixture
def co(session):
    from app.seeds.loader import seed_reference_data, seed_test_universe

    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


def run(session, co):
    return calculate_company(session, co.id, today=TODAY, now=NOW)


def calc(session, co, metric, end, ptype="ANNUAL", basis="CONSOLIDATED"):
    return session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == co.id,
            EvidenceRecord.metric == metric,
            EvidenceRecord.period_end == end,
            EvidenceRecord.period_type == ptype,
            EvidenceRecord.basis == basis,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )


def value(session, co, metric, end, **kw):
    record = calc(session, co, metric, end, **kw)
    assert record is not None, f"{metric} {end} was not calculated"
    return record.value


def near(actual, expected, places="0.000001"):
    return abs(actual - D(str(expected))) <= D(places)


def gaps(session, report, metric):
    return [g for g in report.gaps if g.metric == metric]


def non_financial_year(
    session,
    co,
    d,
    end,
    *,
    revenue,
    other,
    pbt,
    tax,
    profit,
    cap,
    reserves,
    assets,
    eps,
    basis=Basis.CONSOLIDATED,
    **kw,
):
    f = lambda st, item, v, **k: fact(  # noqa: E731
        session, co, st, item, v, end=end, basis=basis, document=d, **{**kw, **k}
    )
    f("INCOME", "revenue_from_operations", revenue)
    f("INCOME", "other_income", other)
    f("INCOME", "total_income", revenue + other)
    f("INCOME", "profit_before_tax", pbt)
    f("INCOME", "tax_expense", tax)
    f("INCOME", "net_profit", profit)
    f("INCOME", "eps_basic", eps, unit="inr_per_share")
    f("BALANCE_SHEET", "equity_share_capital", cap)
    f("BALANCE_SHEET", "other_equity", reserves)
    f("BALANCE_SHEET", "total_assets", assets)
    f("BALANCE_SHEET", "total_equity_and_liabilities", assets)


@pytest.fixture
def two_years(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY25,
        revenue=1000,
        other=50,
        pbt=150,
        tax=30,
        profit=120,
        cap=100,
        reserves=400,
        assets=2000,
        eps=12,
    )
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=55,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    return d


# ---- fundamentals ---------------------------------------------------------------------------------


def test_non_financial_ratios_match_hand_calculation(session, co, two_years):
    report = run(session, co)
    v = lambda m: value(session, co, m, FY26)  # noqa: E731
    assert report.fundamentals.profile == "NONFIN"
    assert v("BALANCE.owners_equity") == 600 * CR
    assert v("INCOME.attributable_profit") == 150 * CR
    assert v("GROWTH.revenue_yoy") == D("10")
    assert near(v("GROWTH.total_income_yoy"), 10)
    assert v("GROWTH.net_profit_yoy") == D("25") and v("GROWTH.eps_yoy") == D("25")
    assert near(v("MARGIN.net_margin"), "13.636364") and near(v("MARGIN.pbt_margin"), "18.181818")
    assert v("RATIO.effective_tax_rate") == D("25")
    assert v("RETURN.roe_closing") == D("25")
    assert near(v("RETURN.roe_average"), "27.272727")  # 150 / ((600+500)/2)
    assert v("RETURN.roa_closing") == D("6.25")
    assert near(v("RETURN.roa_average"), "6.818182")  # 150 / ((2400+2000)/2)
    assert v("LEVERAGE.equity_multiplier") == D("4")
    assert near(v("MARGIN.other_income_share"), "4.761905")
    # FY2025 has no earlier year stored: averages and growth are gaps, not zero
    assert calc(session, co, "GROWTH.revenue_yoy", FY25) is None
    assert any("2024-03-31" in g.reason for g in gaps(session, report, "GROWTH.revenue_yoy"))
    # bank-only metrics are not attempted for a non-financial company
    assert not gaps(session, report, "RATIO.cost_to_income")
    assert calc(session, co, "RATIO.cost_to_income", FY26) is None


def test_every_result_records_formula_inputs_definition_and_caveat(session, co, two_years):
    run(session, co)
    roe = calc(session, co, "RETURN.roe_closing", FY26)
    assert roe.expression == "profit / equity * 100" and roe.unit == "percent"
    assert roe.details["definition"].startswith("RETURN.roe_closing@ANNUAL#0")
    assert {"profit", "equity"} == set(roe.details["inputs"])
    profit = calc(session, co, "INCOME.attributable_profit", FY26)
    assert any("non-controlling" in n for n in profit.details["notes"])


def test_missing_inputs_become_gaps_with_the_reason(session, co, two_years):
    report = run(session, co)
    dte = gaps(session, report, "LEVERAGE.debt_to_equity")
    assert dte and "BALANCE_SHEET.borrowings" in dte[0].reason
    assert {g.metric for g in report.gaps} >= {"CASH.free_cash_flow", "LIQUIDITY.current_ratio"}
    stored = session.scalars(
        select(CalculationGap).where(CalculationGap.run_id == report.run.id)
    ).all()
    assert len(stored) == len(report.gaps) == report.run.gaps
    assert report.run.calculated == len(report.fundamentals.calculated) + len(
        report.market.calculated
    )


def test_growth_from_a_loss_or_zero_base_is_refused_not_computed(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY25,
        revenue=1000,
        other=0,
        pbt=-10,
        tax=0,
        profit=-10,
        cap=100,
        reserves=400,
        assets=2000,
        eps=-1,
    )
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=0,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    report = run(session, co)
    assert calc(session, co, "GROWTH.net_profit_yoy", FY26) is None
    gap = next(g for g in gaps(session, report, "GROWTH.net_profit_yoy") if g.period_end == FY26)
    assert "before is -10" in gap.reason and "undefined or misleading" in gap.reason
    assert value(session, co, "GROWTH.revenue_yoy", FY26) == D(10)  # revenue growth is fine


def test_consolidated_and_standalone_are_calculated_separately_and_never_mixed(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=0,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=900,
        other=0,
        pbt=100,
        tax=20,
        profit=80,
        cap=100,
        reserves=300,
        assets=1000,
        eps=8,
        basis=Basis.STANDALONE,
    )
    run(session, co)
    assert value(session, co, "RETURN.roe_closing", FY26, basis="CONSOLIDATED") == D("25")
    assert value(session, co, "RETURN.roe_closing", FY26, basis="STANDALONE") == D("20")
    for record in session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == co.id,
            EvidenceRecord.kind == "CALCULATION",
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    ):
        bases = {
            i
            for i in (
                session.get(EvidenceRecord, x["evidence_id"]).basis
                for x in record.details["inputs"].values()
            )
            if i
        }
        assert bases <= {record.basis}, record.metric
    # a standalone profit is just the net profit; no non-controlling caveat there
    assert not any(
        "non-controlling" in n
        for n in calc(session, co, "INCOME.attributable_profit", FY26, basis="STANDALONE").details[
            "notes"
        ]
    )


def test_owners_profit_is_preferred_when_stored(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=0,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    fact(session, co, "INCOME", "net_profit_owners", 140, end=FY26, document=d)
    run(session, co)
    assert value(session, co, "INCOME.attributable_profit", FY26) == 140 * CR
    assert calc(session, co, "INCOME.attributable_profit", FY26).details["notes"] == []


def test_a_blocked_input_is_named_in_the_gap(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=0,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    # total equity and liabilities that disagree with total assets: the balance-sheet identity fails
    session.scalar(
        select(FinancialFact).where(FinancialFact.line_item == "total_equity_and_liabilities")
    ).original_value = D(3000)
    session.scalar(
        select(FinancialFact).where(FinancialFact.line_item == "total_equity_and_liabilities")
    ).value_inr = 3000 * CR
    session.flush()
    report = run(session, co)
    assert calc(session, co, "RETURN.roa_closing", FY26) is None
    reason = next(g.reason for g in gaps(session, report, "RETURN.roa_closing"))
    assert "blocked by validation" in reason and "BALANCE_SHEET.total_assets" in reason
    assert value(session, co, "RETURN.roe_closing", FY26) == D(25)  # unaffected inputs still work


def test_a_calculation_is_no_better_than_its_weakest_input(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=0,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    profit = session.scalar(select(FinancialFact).where(FinancialFact.line_item == "net_profit"))
    profit.provider_code, profit.document_id, profit.source_page = "UPSTOX_FUNDAMENTALS", None, None
    profit.source_url = "https://upstox.invalid"
    session.flush()
    run(session, co)
    roe = calc(session, co, "RETURN.roe_closing", FY26)
    resolution = session.scalar(
        select(EvidenceResolution).where(EvidenceResolution.selected_evidence_id == roe.id)
    )
    assert resolution.quality.value == "LOW"
    # a secondary-source input (tier 5) makes the calculation a tier-5 result
    assert any("secondary source" in r for r in resolution.quality_reasons)


def test_a_second_run_changes_nothing(session, co, two_years):
    first = run(session, co)
    ids = {r.id for r in [*first.fundamentals.calculated]}
    n = session.scalar(select(func.count()).select_from(EvidenceRecord))
    second = run(session, co)
    assert {r.id for r in second.fundamentals.calculated} == ids
    assert session.scalar(select(func.count()).select_from(EvidenceRecord)) == n
    assert second.run.id != first.run.id and second.run.gaps == first.run.gaps


def test_the_validation_after_a_run_is_clean_and_covers_every_calculation(session, co, two_years):
    report = run(session, co)
    assert report.validation.run.errors == 0
    resolved = session.scalar(
        select(func.count())
        .select_from(EvidenceResolution)
        .where(EvidenceResolution.company_id == co.id)
    )
    assert resolved >= report.calculated


# ---- banks ----------------------------------------------------------------------------------------


def bank_year(session, co, d, end, basis):
    def f(item, v, st="INCOME"):
        return fact(session, co, st, item, v, end=end, basis=basis, document=d)

    f("interest_earned", 900)
    f("interest_expended", 500)
    f("other_income", 100)
    f("total_income", 1000)
    f("operating_expenses", 300)
    f("operating_profit_before_provisions", 200)  # 900 - 500 + 100 - 300
    f("provisions_excluding_tax", 40)
    f("profit_before_tax", 160)
    f("tax_expense", 40)
    f("net_profit", 120)
    f("capital", 10, "BALANCE_SHEET")
    f("reserves_and_surplus", 90, "BALANCE_SHEET")
    f("total_assets", 2000, "BALANCE_SHEET")
    f("total_capital_and_liabilities", 2000, "BALANCE_SHEET")
    f("advances", 1200, "BALANCE_SHEET")
    f("deposits", 1500, "BALANCE_SHEET")


def test_bank_profile_uses_bank_metrics_and_flags_group_effects(session, co):
    d = doc(session, co)
    bank_year(session, co, d, FY26, Basis.CONSOLIDATED)
    bank_year(session, co, d, FY26, Basis.STANDALONE)
    report = run(session, co)
    assert report.fundamentals.profile == "BANK"
    v = lambda m, basis="CONSOLIDATED": value(session, co, m, FY26, basis=basis)  # noqa: E731
    assert v("INCOME.net_interest_income") == 400 * CR
    assert v("RATIO.cost_to_income") == D("60")  # 300 / (400 + 100)
    assert v("MARGIN.ppop_margin") == D("20") and v("RATIO.provisions_to_ppop") == D("20")
    assert v("RATIO.advances_to_deposits") == D("80")
    assert v("BALANCE.owners_equity") == 100 * CR and v("RETURN.roe_closing") == D("120")
    assert v("MARGIN.net_profit_to_income") == D("12")
    # not attempted for banks
    assert calc(session, co, "MARGIN.net_margin", FY26) is None
    assert not gaps(session, report, "LEVERAGE.debt_to_equity")
    consolidated = calc(session, co, "RATIO.cost_to_income", FY26)
    standalone = calc(session, co, "RATIO.cost_to_income", FY26, basis="STANDALONE")
    assert BANK_GROUP_NOTE in consolidated.details["notes"]
    assert BANK_GROUP_NOTE not in standalone.details["notes"]
    equity = calc(session, co, "BALANCE.owners_equity", FY26)
    assert any("ESOP" in n for n in equity.details["notes"])


# ---- trailing twelve months -----------------------------------------------------------------------


def ttm_data(session, co, d, h1_fy27=250):
    q = lambda end, v: fact(  # noqa: E731
        session, co, "INCOME", "total_income", v, end=end, ptype=PeriodType.QUARTERLY, document=d
    )
    for end, v in (
        (date(2025, 6, 30), 100),
        (SEP25, 110),
        (date(2025, 12, 31), 120),
        (FY26, 130),
        (date(2026, 6, 30), 120),
        (SEP26, 130),
    ):
        q(end, v)
    fact(session, co, "INCOME", "total_income", 460, end=FY26, document=d)
    fact(
        session,
        co,
        "INCOME",
        "total_income",
        210,
        end=SEP25,
        ptype=PeriodType.HALF_YEAR,
        document=d,
    )
    fact(
        session,
        co,
        "INCOME",
        "total_income",
        h1_fy27,
        end=SEP26,
        ptype=PeriodType.HALF_YEAR,
        document=d,
    )


def test_ttm_by_four_quarters_and_by_year_plus_ytd_agree(session, co):
    ttm_data(session, co, doc(session, co))
    report = run(session, co)
    by_quarters = calc(session, co, "TTM.total_income", date(2026, 6, 30), ptype="TTM")
    assert by_quarters.value == 480 * CR  # 110 + 120 + 130 + 120
    at_sep = session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.metric == "TTM.total_income",
            EvidenceRecord.period_end == SEP26,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    ).all()
    assert len(at_sep) == 2 and {r.value for r in at_sep} == {500 * CR}
    methods = {r.details["definition"].split("#")[0] for r in at_sep}
    assert methods == {"TTM.total_income@QUARTERLY", "TTM.total_income@HALF_YEAR"}
    assert {tuple(r.details["mixed_period_types"] or ()) for r in at_sep} == {
        (),
        ("ANNUAL", "HALF_YEAR"),
    }
    assert all(r.period_type == "TTM" for r in at_sep)
    assert report.validation.run.errors == 0  # two methods, same answer: no conflict
    # TTM gaps made redundant by the other method are not reported
    assert not [
        g
        for g in gaps(session, report, "TTM.total_income")
        if g.period_end in (SEP26, date(2026, 6, 30))
    ]


def test_two_ttm_methods_that_disagree_are_a_conflict_and_nothing_is_used(session, co):
    ttm_data(session, co, doc(session, co), h1_fy27=300)  # 460 + 300 - 210 = 550, quarters say 500
    report = run(session, co)
    assert "CROSS_SOURCE_CONFLICT" in {
        f.rule for f in report.validation.findings if f.severity is Severity.ERROR
    }
    row = session.scalar(
        select(EvidenceResolution).where(
            EvidenceResolution.metric == "TTM.total_income",
            EvidenceResolution.period_end == SEP26,
            EvidenceResolution.period_type == "TTM",
        )
    )
    assert row.status.value == "CONFLICT" and row.selected_evidence_id is None


# ---- prices: valuation and technicals ---------------------------------------------------------------


def listing(session, co, exchange="NSE"):
    return session.scalar(
        select(Security)
        .join(Security.exchange)
        .where(Security.company_id == co.id, Security.exchange.has(code=exchange))
    )


def add_prices(session, security, closes, end=date(2026, 11, 6), series="EQ", prev=None):
    day, rows = end, []
    for c in reversed(closes):
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        rows.append((day, D(str(c))))
        day -= timedelta(days=1)
    rows.reverse()
    last = None
    for i, (day, close) in enumerate(rows):
        reference = prev.get(day) if prev and day in prev else (rows[i - 1][1] if i else close)
        session.add(
            EodPrice(
                security_id=security.id,
                trade_date=day,
                series=series,
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                prev_close=reference,
                volume=1000 + i,
                turnover_inr=close * (1000 + i),
                delivery_pct=D("50"),
                source_url=f"https://nse.invalid/{day}",
                file_sha256="a" * 64,
                retrieved_at=NOW,
            )
        )
        last = day
    session.flush()
    return last


def shares(session, co, d, count=10_000_000):
    cell(
        session,
        co,
        d,
        "SHAREHOLDING",
        "total",
        count,
        unit="shares",
        months=None,
        end=FY26,
        basis=None,
    )


def test_valuation_multiples_from_price_shares_and_statements(session, co, two_years):
    shares(session, co, two_years)
    add_prices(session, listing(session, co), [300] * 30)
    report = run(session, co)
    price_date = report.market.price_date
    assert price_date == date(2026, 11, 6)
    cap = session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.market_cap")
    )
    assert cap.value == 300 * 10_000_000 and cap.unit == "inr"
    pe = session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.pe_annual")
    )
    assert pe.value == D(20) and pe.unit == "x"  # 300 / 15
    pb = session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.price_to_book")
    )
    assert pb.value == D("0.5")  # 3,000,000,000 / (600 crore)
    assert any("price of 2026-11-06" in n for n in pe.details["notes"])
    # no trailing EPS exists (no quarters), and that is said
    assert any(g.metric == "VALUATION.pe_ttm" for g in report.gaps)
    assert report.validation.run.errors == 0


def test_a_bonus_after_the_statement_date_adjusts_eps_and_share_count_explicitly(
    session, co, two_years
):
    shares(session, co, two_years)
    sec = listing(session, co)
    add_prices(session, sec, [300] * 30)
    session.add(
        CorporateAction(
            company_id=co.id,
            provider_code="UPSTOX_FUNDAMENTALS",
            action_type="Bonus",
            effective_date=date(2026, 6, 1),
            ratio_text="1:1",
            source_url="https://x.invalid",
            retrieved_at=NOW,
        )
    )
    session.flush()
    run(session, co)
    pe = session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.pe_annual")
    )
    assert pe.value == D(40)  # 300 / (15 / 2)
    assert pe.expression == "price / (eps / factor)"
    cap = session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.market_cap")
    )
    assert cap.value == 300 * 20_000_000  # shares doubled by the bonus
    factor = session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "ADJUSTMENT.cumulative_factor")
    )
    assert factor.value == D(2) and factor.evidence_class is EvidenceClass.FACT
    assert factor.details["events"][0]["origin"] == "CORPORATE_ACTION"
    assert any("adjusted by factor 2" in n for n in pe.details["notes"])


def test_missing_shares_make_market_cap_and_book_multiple_gaps(session, co, two_years):
    add_prices(session, listing(session, co), [300] * 30)
    report = run(session, co)
    cap_gap = next(g for g in report.gaps if g.metric == "VALUATION.market_cap")
    assert "SHAREHOLDING.total.shares" in cap_gap.reason
    assert any(g.metric == "VALUATION.price_to_book" for g in report.gaps)
    assert session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.pe_annual")
    )


def test_a_loss_has_no_pe(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=0,
        pbt=-50,
        tax=0,
        profit=-50,
        cap=100,
        reserves=500,
        assets=2400,
        eps=-5,
    )
    add_prices(session, listing(session, co), [300] * 30)
    report = run(session, co)
    gap = next(g for g in report.gaps if g.metric == "VALUATION.pe_annual")
    assert "not meaningful" in gap.reason
    assert (
        session.scalar(select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.pe_annual"))
        is None
    )


def test_no_prices_is_reported_once_and_fundamentals_still_run(session, co, two_years):
    report = run(session, co)
    assert report.market.security is None
    assert {g.metric for g in report.market.gaps} == {"TECH.*", "VALUATION.*"}
    assert report.run.calculated == len(report.fundamentals.calculated) > 0


def test_technical_indicators_are_recorded_with_their_inputs_and_reasons_for_gaps(session, co):
    sec = listing(session, co)
    add_prices(session, sec, [100 + i % 7 for i in range(60)])
    report = run(session, co)
    sma20 = session.scalar(select(EvidenceRecord).where(EvidenceRecord.metric == "TECH.sma_20"))
    closes = [100 + i % 7 for i in range(60)][-20:]
    assert sma20.value == D(sum(closes)) / 20 and sma20.kind == "TECHNICAL"
    assert sma20.dimension == "NSE:RELIANCE" and sma20.period_end == date(2026, 11, 6)
    assert sma20.details["method"] == "technical.v1" and sma20.details["bars"] == 60
    assert len(sma20.details["price_digest"]) == 64
    sma200 = [g for g in report.market.gaps if g.metric == "TECH.sma_200"]
    assert sma200 and "needs 200 prices; 60 stored" in sma200[0].reason
    assert [g for g in report.market.gaps if g.metric == "TECH.return_1y"]
    assert report.validation.run.errors == 0


def test_technical_value_is_reproducible_and_a_changed_price_is_detected(session, co):
    sec = listing(session, co)
    add_prices(session, sec, [100 + i % 7 for i in range(60)])
    run(session, co)
    sma = session.scalar(select(EvidenceRecord).where(EvidenceRecord.metric == "TECH.sma_20"))
    assert recompute(session, sma)[0] == sma.value
    last = session.scalar(
        select(EodPrice).where(EodPrice.security_id == sec.id).order_by(EodPrice.trade_date.desc())
    )
    last.close = last.close + 10
    session.flush()
    report = validate_company(session, co.id, today=TODAY, now=NOW, sync=False)
    rules = {(f.rule, f.metric) for f in report.findings if f.severity is Severity.ERROR}
    assert ("CALC_INPUT_NOT_CURRENT", "TECH.sma_20") in rules
    assert ("CALC_RECOMPUTE_MISMATCH", "TECH.sma_20") in rules
    blocked = session.scalar(
        select(EvidenceResolution).where(EvidenceResolution.selected_evidence_id == sma.id)
    )
    assert blocked.blocked


def test_register_technical_skips_with_a_reason_and_is_idempotent(session, co):
    sec = listing(session, co)
    add_prices(session, sec, [100 + i % 5 for i in range(30)])
    skipped = register_technical(session, co.id, sec, "sma_200", date(2026, 11, 6))
    assert isinstance(skipped, Skipped) and "SMA(200)" in skipped.reason
    a = register_technical(session, co.id, sec, "sma_20", date(2026, 11, 6), now=NOW)
    b = register_technical(session, co.id, sec, "sma_20", date(2026, 11, 6), now=NOW)
    assert a.id == b.id
    with pytest.raises(InsufficientHistory):
        recompute(
            session,
            EvidenceRecord(
                details={"as_of": "2000-01-01", "security_id": sec.id, "indicator": "sma_20"}
            ),
        )


# ---- price adjustment events in the database --------------------------------------------------------


def split_prices(session, sec, event_day=date(2026, 11, 4)):
    closes = [1000] * 8 + [505] * 4
    last = add_prices(session, sec, closes, end=date(2026, 11, 6))
    ex = session.scalar(
        select(EodPrice).where(EodPrice.security_id == sec.id, EodPrice.trade_date == event_day)
    )
    first_new = session.scalars(
        select(EodPrice)
        .where(EodPrice.security_id == sec.id, EodPrice.close == 505)
        .order_by(EodPrice.trade_date)
    ).first()
    first_new.prev_close = D("500")  # the exchange's reference price on the ex-date
    session.flush()
    return first_new.trade_date, last, ex


def test_a_split_seen_by_both_exchanges_is_confirmed_and_applied(session, co):
    nse, bse = listing(session, co, "NSE"), listing(session, co, "BSE")
    ex_date, _, _ = split_prices(session, nse)
    split_prices(session, bse)
    report = rebuild_adjustments(session, co.id, now=NOW)
    rows = session.scalars(select(PriceAdjustment).order_by(PriceAdjustment.security_id)).all()
    assert len(rows) == 2 and {r.confidence for r in rows} == {"CONFIRMED"}
    assert all(
        r.status is AdjustmentStatus.APPLIED and r.factor == 2 and r.ex_date == ex_date
        for r in rows
    )
    assert {r.kind for r in rows} == {AdjustmentKind.SPLIT_OR_BONUS}
    bars = adjusted_bars(session, nse.id)
    assert bars[0].close == D(500) and bars[0].raw_close == D(1000) and bars[-1].close == D("505")
    assert bars[0].volume == D(2000) + 0 or bars[0].volume > 0
    assert len(report.applied()) == 2
    # rebuilding replaces the derived rows: no duplicates
    rebuild_adjustments(session, co.id, now=NOW)
    assert session.scalar(select(func.count()).select_from(PriceAdjustment)) == 2


def test_an_unexplained_gap_is_recorded_but_prices_stay_raw(session, co):
    sec = listing(session, co)
    add_prices(session, sec, [1395] * 5 + [1390] * 5, prev={date(2026, 11, 2): D("1347")})
    report = rebuild_adjustments(session, co.id, now=NOW)
    (row,) = session.scalars(select(PriceAdjustment)).all()
    assert row.kind is AdjustmentKind.UNEXPLAINED and row.status is AdjustmentStatus.REPORTED_ONLY
    assert len(report.unexplained()) == 1 and not report.applied()
    assert all(b.close == b.raw_close for b in adjusted_bars(session, sec.id))


def test_a_stored_bonus_overrides_and_is_cross_checked_with_the_prices(session, co):
    sec = listing(session, co)
    ex_date, _, _ = split_prices(session, sec)
    session.add(
        CorporateAction(
            company_id=co.id,
            provider_code="UPSTOX_FUNDAMENTALS",
            action_type="Bonus",
            effective_date=ex_date,
            ratio_text="1:1",
            source_url="https://x.invalid",
            retrieved_at=NOW,
        )
    )
    session.flush()
    rebuild_adjustments(session, co.id, now=NOW)
    rows = {r.origin.value: r for r in session.scalars(select(PriceAdjustment))}
    assert rows["CORPORATE_ACTION"].status is AdjustmentStatus.APPLIED
    assert rows["CORPORATE_ACTION"].corporate_action_id is not None
    assert rows["IMPLIED"].status is AdjustmentStatus.REPORTED_ONLY
    assert "agrees with the stored corporate action" in rows["IMPLIED"].detail
    adjusted = [b for b in adjusted_bars(session, sec.id) if b.trade_date < ex_date]
    assert all(b.cumulative_factor == 2 for b in adjusted)  # applied once, not twice


def test_an_unparseable_corporate_action_is_reported_in_the_run(session, co, two_years):
    session.add(
        CorporateAction(
            company_id=co.id,
            provider_code="UPSTOX_FUNDAMENTALS",
            action_type="Split",
            effective_date=date(2026, 6, 1),
            ratio_text="10:1",
            source_url="https://x.invalid",
            retrieved_at=NOW,
        )
    )
    session.flush()
    report = run(session, co)
    assert (
        report.adjustments.skipped_actions
        and "not unambiguous" in report.adjustments.skipped_actions[0]
    )
    assert report.run.summary["skipped_corporate_actions"]


# ---- API ------------------------------------------------------------------------------------------


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_api_serves_metrics_gaps_technicals_adjustments_and_the_run(session, co, two_years, client):
    add_prices(session, listing(session, co), [100 + i % 7 for i in range(60)])
    run(session, co)
    base = f"/api/companies/{co.id}"

    roe = client.get(
        f"{base}/metrics", params={"prefix": "RETURN.roe_closing", "basis": "consolidated"}
    ).json()
    assert [m["period_end"] for m in roe] == ["2026-03-31", "2025-03-31"]
    top = roe[0]
    assert top["value"] == "25.000000" and top["unit"] == "percent" and top["category"] == "RETURN"
    assert top["formula"] == "profit / equity * 100" and top["label"].startswith("Return on equity")
    assert set(top["inputs"]) == {"profit", "equity"} and top["quality"] == "MEDIUM"
    assert top["blocked"] is False

    only_quarterly = client.get(f"{base}/metrics", params={"period_type": "quarterly"}).json()
    assert all(m["period_type"] == "QUARTERLY" for m in only_quarterly)

    g = client.get(f"{base}/metrics/gaps", params={"prefix": "LEVERAGE"}).json()
    assert g and all(x["metric"].startswith("LEVERAGE") for x in g) and g[0]["reason"]

    tech = client.get(f"{base}/technicals").json()
    assert "TECH.sma_20" in {t["metric"] for t in tech}

    runinfo = client.get(f"{base}/calculation-run").json()
    assert runinfo["profile"] == "NONFIN" and runinfo["calculated"] > 0
    assert client.get(f"{base}/price-adjustments").json() == []


def test_api_404s_and_no_run_yet(session, co, client):
    import uuid

    ghost = uuid.uuid4()
    for path in ("metrics", "technicals", "calculation-run", "metrics/gaps", "price-adjustments"):
        assert client.get(f"/api/companies/{ghost}/{path}").status_code == 404
    assert client.get(f"/api/companies/{co.id}/calculation-run").status_code == 404
    assert client.get(f"/api/companies/{co.id}/metrics").json() == []
    assert client.post(f"/api/companies/{co.id}/metrics").status_code == 405


# ---- EBIT, EBITDA, interest cover, ROCE -------------------------------------------------------------


def test_operating_profit_measures_match_hand_calculation(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=55,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    fact(session, co, "INCOME", "finance_costs", 40, end=FY26, document=d)
    fact(session, co, "INCOME", "depreciation_and_amortisation", 60, end=FY26, document=d)
    fact(session, co, "BALANCE_SHEET", "total_current_liabilities", 600, end=FY26, document=d)
    run(session, co)
    v = lambda m: value(session, co, m, FY26)  # noqa: E731
    assert v("INCOME.ebit") == 240 * CR  # 200 + 40
    assert v("INCOME.ebitda") == 300 * CR  # 240 + 60
    assert near(v("MARGIN.ebit_margin"), "21.818182")  # 240 / 1100
    assert near(v("MARGIN.ebitda_margin"), "27.272727")  # 300 / 1100
    assert v("RATIO.interest_cover") == D("6")  # 240 / 40
    assert near(v("RETURN.roce"), "13.333333")  # 240 / (2400 - 600)
    ebit = calc(session, co, "INCOME.ebit", FY26)
    assert any("other income" in n for n in ebit.details["notes"])


def test_a_company_that_prints_no_depreciation_has_no_ebitda_but_keeps_ebit(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=55,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    fact(session, co, "INCOME", "finance_costs", 40, end=FY26, document=d)
    report = run(session, co)
    assert calc(session, co, "INCOME.ebit", FY26) is not None
    assert calc(session, co, "INCOME.ebitda", FY26) is None
    gap = next(g for g in report.gaps if g.metric == "INCOME.ebitda")
    assert "depreciation_and_amortisation" in gap.reason
    assert calc(session, co, "RETURN.roce", FY26) is None  # needs current liabilities too


def test_zero_finance_costs_give_no_interest_cover_not_infinity(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=55,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    fact(session, co, "INCOME", "finance_costs", 0, end=FY26, document=d)
    report = run(session, co)
    assert calc(session, co, "RATIO.interest_cover", FY26) is None
    gap = next(g for g in report.gaps if g.metric == "RATIO.interest_cover")
    assert "undefined or misleading" in gap.reason


def test_banks_do_not_get_ebit_style_measures(session, co):
    d = doc(session, co)
    bank_year(session, co, d, FY26, Basis.CONSOLIDATED)
    fact(session, co, "INCOME", "finance_costs", 5, end=FY26, document=d)
    report = run(session, co)
    assert calc(session, co, "INCOME.ebit", FY26) is None
    assert not [g for g in report.gaps if g.metric in ("INCOME.ebit", "RETURN.roce")]


# ---- shares outstanding from paid-up capital and face value -----------------------------------------


def set_face_value(session, company, value):
    for sec in session.scalars(select(Security).where(Security.company_id == company.id)):
        sec.face_value = D(str(value))
    session.flush()


def test_shares_outstanding_are_derived_from_capital_and_the_exchanges_face_value(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=0,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    set_face_value(session, co, 10)
    run(session, co)
    face = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.metric == "SECURITY.face_value",
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )
    assert face.value == D(10) and face.unit == "inr_per_share" and face.period_end is None
    assert face.kind == "SECURITY" and face.provider_code == "NSE_ARCHIVES"
    shares_rec = calc(session, co, "BALANCE.shares_outstanding", FY26)
    assert (
        shares_rec.value == D(100) * CR / D(10) and shares_rec.unit == "shares"
    )  # 10 crore shares
    assert any("face value" in n for n in shares_rec.details["notes"])
    assert {i["metric"] for i in shares_rec.details["inputs"].values()} == {
        "BALANCE_SHEET.equity_share_capital",
        "SECURITY.face_value",
    }


def test_market_cap_uses_the_derived_share_count_and_book_multiple_follows(session, co, two_years):
    set_face_value(session, co, 10)
    add_prices(session, listing(session, co), [300] * 30)
    run(session, co)
    cap = session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.market_cap")
    )
    assert cap.value == 300 * 10 * CR  # 300 per share x (100 crore / 10) shares
    assert any("shares outstanding as at" in n for n in cap.details["notes"])
    pb = session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.price_to_book")
    )
    assert pb is not None and pb.value == D(5)  # 3,000 crore market cap / 600 crore of equity


def test_no_face_value_means_no_share_count_and_a_stated_reason(session, co):
    d = doc(session, co)
    non_financial_year(
        session,
        co,
        d,
        FY26,
        revenue=1100,
        other=0,
        pbt=200,
        tax=50,
        profit=150,
        cap=100,
        reserves=500,
        assets=2400,
        eps=15,
    )
    for sec in session.scalars(select(Security).where(Security.company_id == co.id)):
        sec.face_value = None
    session.flush()
    add_prices(session, listing(session, co), [300] * 30)
    report = run(session, co)
    assert calc(session, co, "BALANCE.shares_outstanding", FY26) is None
    gap = next(g for g in report.gaps if g.metric == "BALANCE.shares_outstanding")
    assert "SECURITY.face_value" in gap.reason
    cap = next(g for g in report.gaps if g.metric == "VALUATION.market_cap")
    assert "could not be derived" in cap.reason


def test_a_split_after_the_statement_adjusts_the_derived_share_count(session, co, two_years):
    set_face_value(session, co, 10)
    sec = listing(session, co)
    add_prices(session, sec, [300] * 30)
    session.add(
        CorporateAction(
            company_id=co.id,
            provider_code="UPSTOX_FUNDAMENTALS",
            action_type="Bonus",
            effective_date=date(2026, 6, 1),
            ratio_text="1:1",
            source_url="https://x.invalid",
            retrieved_at=NOW,
        )
    )
    session.flush()
    run(session, co)
    cap = session.scalar(
        select(EvidenceRecord).where(EvidenceRecord.metric == "VALUATION.market_cap")
    )
    assert cap.value == 300 * 20 * CR  # the bonus doubled the share count
