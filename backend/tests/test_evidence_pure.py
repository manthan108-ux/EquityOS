"""Evidence engine, pure parts: expression language, claim checker, resolution and rules.

No database here: every case is small enough to read, so each rule has an explicit failing case
as well as a passing one.
"""

import itertools
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.evidence.claims import CitedValue, check_figures, find_figures
from app.evidence.core import Fact, agree, grade, relative_difference, resolve_group
from app.evidence.expression import (
    ExpressionError,
    Quantity,
    apply_result_unit,
    evaluate,
    names_in,
)
from app.evidence.rules import Context, run_rules
from app.models.evidence import Quality, ResolutionStatus, Severity

D = Decimal
INR = "inr"
DOC = uuid.UUID("11111111-1111-1111-1111-111111111111")


def q(value, unit=INR):
    return Quantity(D(str(value)), unit)


# ---- expression language -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 + 2 * 3", "7"),
        ("(1 + 2) * 3", "9"),
        ("10 / 4", "2.5"),
        ("-2 * 3", "-6"),
        ("2 ^ 3", "8"),
        ("2 ^ 3 ^ 1", "8"),
        ("100 - 1 - 1", "98"),
    ],
)
def test_expression_precedence_and_associativity(text, expected):
    assert evaluate(text, {}).value == D(expected)


def test_expression_units_for_ratios_and_percentages():
    v = {"profit": q(250), "equity": q(1000)}
    roe = evaluate("profit / equity", v)
    assert (roe.value, roe.unit) == (D("0.25"), "ratio")
    pct = evaluate("profit / equity * 100", v)
    assert (pct.value, pct.unit) == (D("25"), "percent")
    back = evaluate("(profit / equity * 100) / 100", v)
    assert back.unit == "ratio"


def test_expression_growth_is_a_ratio():
    out = evaluate("now / before - 1", {"now": q(112), "before": q(100)})
    assert out.value == D("0.12") and out.unit == "ratio"


def test_expression_per_share_algebra():
    assert evaluate("income / shares", {"income": q(1000), "shares": q(10, "shares")}).unit == (
        "inr_per_share"
    )
    assert (
        evaluate("eps * shares", {"eps": q(5, "inr_per_share"), "shares": q(10, "shares")}).unit
        == "inr"
    )
    assert evaluate("m / p", {"m": q(100), "p": q(5, "inr_per_share")}).unit == "shares"


@pytest.mark.parametrize(
    "text,variables",
    [
        ("a + b", {"a": q(1), "b": q(1, "inr_per_share")}),  # rupees + per-share
        ("a * b", {"a": q(1), "b": q(1)}),  # rupees x rupees
        ("a / b", {"a": q(1, "shares"), "b": q(1)}),
        ("a - b", {"a": q(1), "b": q(1, "percent")}),
        ("a + 1", {"a": q(1)}),  # rupees + a bare number
    ],
)
def test_expression_refuses_unit_mixing(text, variables):
    with pytest.raises(ExpressionError):
        evaluate(text, variables)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "1 +",
        "(1 + 2",
        "1 2",
        "a b",
        "1 / 0",
        "2 ^ x",
        "__import__('os')",
        "1 + @",
        "a.b",
    ],
)
def test_expression_rejects_malformed_or_dangerous_input(text):
    with pytest.raises(ExpressionError):
        evaluate(text, {"x": q(1)})


def test_expression_unknown_and_unused_inputs():
    with pytest.raises(ExpressionError, match="unknown input"):
        evaluate("a + zzz", {"a": q(1)})
    with pytest.raises(ExpressionError, match="declared but not used"):
        evaluate("a", {"a": q(1), "b": q(2)})


def test_expression_division_by_zero_input_and_negative_fractional_power():
    with pytest.raises(ExpressionError, match="division by zero"):
        evaluate("a / b", {"a": q(1), "b": q(0)})
    with pytest.raises(ExpressionError, match="negative"):
        evaluate("(0 - 4) ^ 0.5", {})


def test_expression_cagr():
    out = evaluate("(end / start) ^ (1 / 3) - 1", {"end": q(1331), "start": q(1000)})
    assert abs(out.value - D("0.1")) < D("0.000001") and out.unit == "ratio"


def test_expression_length_and_depth_limits():
    with pytest.raises(ExpressionError):
        evaluate("1+" * 300 + "1", {})
    with pytest.raises(ExpressionError, match="deeply"):
        evaluate("(" * 60 + "1" + ")" * 60, {})


def test_names_in_and_result_unit_declaration():
    assert names_in("a / b + a") == ["a", "b"]
    assert apply_result_unit("ratio", None) == "ratio"
    assert apply_result_unit("ratio", "x") == "x"  # a multiple such as P/E
    with pytest.raises(ExpressionError):
        apply_result_unit("inr", "percent")


# ---- claim checker ---------------------------------------------------------------------------------


def cited(value, unit, evidence_id=1):
    return CitedValue(evidence_id, D(str(value)), unit)


def test_claim_money_matches_scaled_value():
    result = check_figures("Net profit was ₹23,450 crore.", [cited(234_500_000_000, "inr")])
    assert result.ok and result.matched[0][1] == 1


def test_claim_money_rounding_is_what_the_writer_used():
    value = cited(234_567_000_000, "inr")
    assert check_figures("about ₹23,457 crore", [value]).ok
    assert check_figures("about ₹23,460 crore", [value]).ok is False  # not a rounding of 23,456.7
    assert check_figures("about ₹2.35 lakh crore", [cited(2_350_000_000_000, "inr")]).ok


def test_claim_plain_number_cannot_match_a_scaled_value():
    assert not check_figures("revenue of 23,450", [cited(234_500_000_000, "inr")]).ok


def test_claim_percent_from_percent_or_ratio_and_sign_ignored():
    assert check_figures("ROE of 18.2%", [cited("18.2", "percent")]).ok
    assert check_figures("ROE of 18.2%", [cited("0.182", "ratio")]).ok
    assert check_figures("profit fell 12%", [cited("-12", "percent")]).ok
    assert not check_figures("ROE of 18.2%", [cited("0.19", "ratio")]).ok


def test_claim_bps_multiple_and_days():
    assert check_figures("margin up 50 bps", [cited("0.50", "percent")]).ok
    assert check_figures("trades at 24.5x earnings", [cited("24.5", "x")]).ok
    assert check_figures("collects in 45 days", [cited("45", "days")]).ok


def test_claim_untraced_number_is_found():
    result = check_figures("Profit rose 12% to ₹500 crore", [cited("12", "percent")])
    assert not result.ok and [f.text for f in result.untraced] == ["₹500 crore"]


def test_claim_ignores_years_fiscal_tokens_dates_and_references():
    text = (
        "In FY2026-27 Q2 and H1 of 2025, as at 30 September 2026 and March 2026, on page 228 "
        "(Note 12), the 3rd item; see Schedule 5."
    )
    assert find_figures(text) == []
    assert find_figures("1. First point\n2) Second point") == []


def test_claim_numbers_are_not_confused_across_units():
    # a per-share figure is not a crore figure
    assert not check_figures("EPS of ₹45 crore", [cited(45, "inr_per_share")]).ok
    assert check_figures("EPS of ₹45", [cited(45, "inr_per_share")]).ok


def test_claim_without_numbers_needs_no_matching():
    assert check_figures("The bank is well capitalised.", []).ok


# ---- resolution and grading -----------------------------------------------------------------------


def fact(
    id=1,
    metric="INCOME.net_profit",
    value=1000,
    provider="DOCUMENT_EXTRACTION",
    tier=2,
    end=date(2026, 3, 31),
    ptype="ANNUAL",
    basis="CONSOLIDATED",
    unit=INR,
    cls="FACT",
    original_unit="crore",
    document_id=DOC,
    page=1,
    **kw,
):
    return Fact(
        id=id,
        cls=cls,
        metric=metric,
        dimension="",
        period_end=end,
        period_type=ptype,
        basis=basis,
        value=D(str(value)),
        unit=unit,
        provider=provider,
        tier=tier,
        original_unit=original_unit,
        document_id=document_id,
        page=page,
        **kw,
    )


CRORE = 10_000_000


def test_single_source_is_selected():
    r = resolve_group([fact(value=5 * CRORE)])
    assert r.status is ResolutionStatus.SINGLE_SOURCE and r.selected.id == 1


def test_sources_within_one_percent_agree():
    a = fact(1, value=1000 * CRORE)
    b = fact(2, value=1005 * CRORE, provider="UPSTOX_FUNDAMENTALS", tier=5)
    r = resolve_group([a, b])
    assert r.status is ResolutionStatus.AGREED and r.selected.id == 1


def test_rounding_alone_counts_as_agreement():
    # EPS printed to whole rupees: 2 and 3 can both be right (1.5..2.5 and 2.5..3.5), though they
    # differ by 33% - but 2 and 4 cannot.
    def eps(id, value):
        return fact(
            id,
            metric="INCOME.eps_basic",
            value=value,
            unit="inr_per_share",
            original_unit="inr_per_share",
            provider=f"P{id}",
            tier=5,
        )

    assert agree(eps(1, 2), eps(2, 3))
    assert not agree(eps(1, 2), eps(2, 4))


def test_stronger_source_wins_and_the_disagreement_is_kept():
    a = fact(1, value=1000 * CRORE)
    b = fact(2, value=1500 * CRORE, provider="UPSTOX_FUNDAMENTALS", tier=5)
    r = resolve_group([b, a])
    assert r.status is ResolutionStatus.RESOLVED_BY_AUTHORITY and r.selected.id == 1
    assert r.alternatives[0]["evidence_id"] == 2 and r.max_difference_pct > 30


def test_equal_authority_disagreement_is_a_conflict_with_no_value():
    a = fact(1, value=1000 * CRORE)
    b = fact(2, value=1500 * CRORE, provider="COMPANY_IR", tier=2)
    r = resolve_group([a, b])
    assert r.status is ResolutionStatus.CONFLICT and r.selected is None


def test_failed_provenance_is_never_selected():
    a = fact(1, problem="document not linked")
    b = fact(2, value=1000 * CRORE, provider="UPSTOX_FUNDAMENTALS", tier=5)
    r = resolve_group([a, b])
    assert r.selected.id == 2
    only_bad = resolve_group([a])
    assert only_bad.status is ResolutionStatus.UNUSABLE and only_bad.selected is None


def test_reported_beats_calculated_and_calculated_alone_is_labelled():
    reported = fact(1, ptype="QUARTERLY", value=100 * CRORE)
    calc = fact(
        2, ptype="QUARTERLY", value=100 * CRORE, provider="DERIVED_QUARTER", cls="CALCULATION"
    )
    assert resolve_group([calc, reported]).selected.id == 1
    alone = resolve_group([calc])
    assert alone.status is ResolutionStatus.CALCULATED and alone.selected.id == 2


def test_relative_difference_floor_of_one():
    assert relative_difference(D("0"), D("0.5")) == D("0.5")


def graded(*facts):
    r = resolve_group(list(facts))
    return grade(r)


def test_grade_primary_single_source_is_medium_and_corroborated_is_high():
    level, reasons = graded(fact(1, value=100 * CRORE))
    assert level is Quality.MEDIUM and any("primary source" in x for x in reasons)
    level, _ = graded(
        fact(1, value=100 * CRORE),
        fact(2, value=100 * CRORE, provider="COMPANY_IR", tier=2),
    )
    assert level is Quality.HIGH
    level, _ = graded(fact(1, value=100 * CRORE, details={"other_documents_agree": 1}))
    assert level is Quality.HIGH


def test_grade_caps_and_lows():
    assert graded(fact(1, provider="UPSTOX_FUNDAMENTALS", tier=5))[0] is Quality.LOW
    assert graded(fact(1, provider="MANUAL_IMPORT", tier=6))[0] is Quality.LOW
    ocr = fact(1, details={"ocr_confidence": "91", "other_documents_agree": 2})
    assert graded(ocr)[0] is Quality.MEDIUM
    differ = fact(1, details={"other_documents_differ": 1})
    assert graded(differ)[0] is Quality.MEDIUM
    calc = fact(1, cls="CALCULATION", provider="DERIVED_QUARTER")
    assert graded(calc)[0] is Quality.MEDIUM
    assert graded(fact(1, details={"unit_unstated": True}))[0] is Quality.LOW
    conflict = graded(fact(1, value=1), fact(2, value=2 * CRORE, provider="COMPANY_IR"))
    assert conflict[0] is Quality.LOW


# ---- rules -----------------------------------------------------------------------------------------


def run(*facts, today=date(2026, 11, 8)):
    groups = {}
    for f in facts:
        groups.setdefault(f.key, []).append(f)
    resolved = {k: resolve_group(v) for k, v in groups.items()}
    return run_rules(Context(resolved=resolved, facts=list(facts), today=today))


def codes(findings, severity=None):
    return sorted(f.rule for f in findings if severity is None or f.severity is severity)


_ids = itertools.count(1)


def f_():  # unique ids
    return next(_ids)


def item(metric, value, **kw):
    return fact(f_(), metric=metric, value=value * CRORE, **kw)


def test_balance_sheet_identity():
    ok = run(
        item("BALANCE_SHEET.total_assets", 5000),
        item("BALANCE_SHEET.total_equity_and_liabilities", 5000),
    )
    assert "BALANCE_SHEET_IDENTITY" not in codes(ok)
    bad = run(
        item("BALANCE_SHEET.total_assets", 5000),
        item("BALANCE_SHEET.total_capital_and_liabilities", 5100),
    )
    assert "BALANCE_SHEET_IDENTITY" in codes(bad, Severity.ERROR)


def test_income_total_identity_for_company_and_bank():
    ok = run(
        item("INCOME.total_income", 1100),
        item("INCOME.revenue_from_operations", 1000),
        item("INCOME.other_income", 100),
    )
    assert "INCOME_TOTAL_IDENTITY" not in codes(ok)
    bank_bad = run(
        item("INCOME.total_income", 1100),
        item("INCOME.interest_earned", 900),
        item("INCOME.other_income", 100),
    )
    assert "INCOME_TOTAL_IDENTITY" in codes(bank_bad, Severity.ERROR)


def test_identity_is_not_applied_across_bases_or_periods():
    findings = run(
        item("INCOME.total_income", 1100, basis="CONSOLIDATED"),
        item("INCOME.revenue_from_operations", 5000, basis="STANDALONE"),
        item("INCOME.other_income", 100, basis="CONSOLIDATED"),
    )
    assert "INCOME_TOTAL_IDENTITY" not in codes(findings)


def test_cash_flow_identity_and_roll_forward():
    base = [
        item("CASH_FLOW.net_cash_from_operating_activities", 500),
        item("CASH_FLOW.net_cash_from_investing_activities", -300),
        item("CASH_FLOW.net_cash_from_financing_activities", -100),
    ]
    ok = run(*base, item("CASH_FLOW.net_change_in_cash", 100))
    assert "CASH_FLOW_IDENTITY" not in codes(ok)
    bad = run(*base, item("CASH_FLOW.net_change_in_cash", 150))
    assert "CASH_FLOW_IDENTITY" in codes(bad, Severity.ERROR)
    roll_bad = run(
        item("CASH_FLOW.net_change_in_cash", 100),
        item("CASH_FLOW.cash_at_beginning", 1000),
        item("CASH_FLOW.cash_at_end", 1200),
    )
    finding = next(f for f in roll_bad if f.rule == "CASH_ROLL_FORWARD")
    assert finding.severity is Severity.INFO and "not conclusive" in finding.message
    roll_fx = run(
        item("CASH_FLOW.net_change_in_cash", 100),
        item("CASH_FLOW.cash_at_beginning", 1000),
        item("CASH_FLOW.cash_at_end", 1200),
        item("CASH_FLOW.fx_effect_on_cash", 10),
    )
    assert next(f for f in roll_fx if f.rule == "CASH_ROLL_FORWARD").severity is Severity.ERROR


def quarters(metric, values, basis="CONSOLIDATED"):
    ends = [date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31)]
    return [
        fact(f_(), metric=metric, value=v * CRORE, end=e, ptype="QUARTERLY", basis=basis)
        for v, e in zip(values, ends, strict=False)
    ]


def test_quarters_must_add_up_to_the_year_and_the_half():
    metric = "INCOME.total_income"
    year = fact(f_(), metric=metric, value=400 * CRORE, ptype="ANNUAL")
    assert "QUARTERS_VS_CUMULATIVE" not in codes(run(year, *quarters(metric, [100] * 4)))
    bad = run(year, *quarters(metric, [100, 100, 100, 90]))
    assert "QUARTERS_VS_CUMULATIVE" in codes(bad, Severity.WARNING)
    half = fact(f_(), metric=metric, value=250 * CRORE, end=date(2025, 9, 30), ptype="HALF_YEAR")
    assert "QUARTERS_VS_CUMULATIVE" in codes(run(half, *quarters(metric, [100, 100, 0, 0])[:2]))
    nine = fact(f_(), metric=metric, value=300 * CRORE, end=date(2025, 12, 31), ptype="NINE_MONTH")
    assert "QUARTERS_VS_CUMULATIVE" not in codes(run(nine, *quarters(metric, [100] * 4)[:3]))


def test_quarter_sums_skip_incomplete_derived_and_non_additive_items():
    metric = "INCOME.total_income"
    year = fact(f_(), metric=metric, value=400 * CRORE, ptype="ANNUAL")
    three = quarters(metric, [100, 100, 100])
    assert "QUARTERS_VS_CUMULATIVE" not in codes(run(year, *three))  # one quarter missing
    derived = quarters(metric, [100, 100, 100, 5])
    derived[3].cls = "CALCULATION"
    assert "QUARTERS_VS_CUMULATIVE" not in codes(run(year, *derived))
    eps_year = fact(f_(), metric="INCOME.eps_basic", value=40 * CRORE, ptype="ANNUAL")
    assert "QUARTERS_VS_CUMULATIVE" not in codes(
        run(eps_year, *quarters("INCOME.eps_basic", [1] * 4))
    )


def test_plausibility_negative_totals_and_eps_sign():
    findings = run(
        item("BALANCE_SHEET.total_assets", -5),
        item("INCOME.total_income", -5),
    )
    assert codes(findings, Severity.ERROR).count("NEGATIVE_VALUE") == 1
    assert codes(findings, Severity.WARNING).count("NEGATIVE_VALUE") == 1
    eps = run(
        fact(
            f_(),
            metric="INCOME.eps_basic",
            value=-3,
            unit="inr_per_share",
            original_unit="inr_per_share",
        ),
        item("INCOME.net_profit", 100),
    )
    assert "EPS_SIGN" in codes(eps)


def test_large_year_change_is_flagged_only_for_annual_totals():
    now = fact(f_(), metric="INCOME.total_income", value=600 * CRORE)
    before = fact(f_(), metric="INCOME.total_income", value=100 * CRORE, end=date(2025, 3, 31))
    assert "LARGE_YEAR_CHANGE" in codes(run(now, before))
    steady = fact(f_(), metric="INCOME.total_income", value=120 * CRORE)
    assert "LARGE_YEAR_CHANGE" not in codes(run(steady, before))


def test_freshness():
    old = fact(f_(), metric="INCOME.total_income", end=date(2024, 3, 31))
    assert "STALE_DATA" in codes(run(old))
    recent = fact(f_(), metric="INCOME.total_income", end=date(2026, 3, 31))
    assert "STALE_DATA" not in codes(run(recent))


def test_source_findings_conflict_disagreement_and_provenance():
    a = fact(1001, value=1000 * CRORE)
    b = fact(1002, value=1500 * CRORE, provider="COMPANY_IR", tier=2)
    assert "CROSS_SOURCE_CONFLICT" in codes(run(a, b), Severity.ERROR)
    weak = fact(1003, value=1500 * CRORE, provider="UPSTOX_FUNDAMENTALS", tier=5)
    assert "SOURCE_DISAGREEMENT" in codes(run(a, weak), Severity.WARNING)
    bad = fact(1004, problem="the source document is not linked to this company")
    names = codes(run(bad), Severity.ERROR)
    assert "PROVENANCE" in names and "NO_USABLE_SOURCE" in names
    nopage = fact(1005, page=None)  # document figure without a page
    assert "PROVENANCE" in codes(run(nopage), Severity.ERROR)


def test_document_findings_discrepancy_restated_unit():
    f = fact(
        2001,
        details={"other_documents_differ": 1, "restated": True, "unit_unstated": True},
        page=3,
    )
    names = codes(run(f))
    assert {"DOCUMENT_DISCREPANCY", "RESTATED", "UNIT_UNSTATED"} <= set(names)
    assert next(x for x in run(f) if x.rule == "RESTATED").severity is Severity.INFO


def test_cash_flow_identity_accepts_the_exchange_rate_line_either_way():
    parts = [
        item("CASH_FLOW.net_cash_from_operating_activities", 500),
        item("CASH_FLOW.net_cash_from_investing_activities", -300),
        item("CASH_FLOW.net_cash_from_financing_activities", -100),
        item("CASH_FLOW.fx_effect_on_cash", 10),
    ]
    with_fx = run(*parts, item("CASH_FLOW.net_change_in_cash", 110))
    without_fx = run(*parts, item("CASH_FLOW.net_change_in_cash", 100))
    neither = run(*parts, item("CASH_FLOW.net_change_in_cash", 150))
    assert "CASH_FLOW_IDENTITY" not in codes(with_fx)
    assert "CASH_FLOW_IDENTITY" not in codes(without_fx)
    assert "CASH_FLOW_IDENTITY" in codes(neither, Severity.ERROR)
    roll = run(
        item("CASH_FLOW.net_change_in_cash", 100),
        item("CASH_FLOW.cash_at_beginning", 1000),
        item("CASH_FLOW.cash_at_end", 1110),
        item("CASH_FLOW.fx_effect_on_cash", 10),
    )
    assert "CASH_ROLL_FORWARD" not in codes(roll)


def test_a_conflict_between_units_is_described_as_such():
    shares = fact(1, metric="SHAREHOLDING.x", value=1000, unit="shares", original_unit=None)
    percent = fact(2, metric="SHAREHOLDING.x", value=5, unit="percent", original_unit=None)
    r = resolve_group([shares, percent])
    assert r.status is ResolutionStatus.CONFLICT and "different units" in r.note
