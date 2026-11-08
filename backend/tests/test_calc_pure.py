"""Calculation engine, pure parts: technical indicators, price adjustment logic, catalogue."""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.calc import technical as T
from app.calc.adjust import (
    Bar,
    Event,
    PriceRow,
    adjust,
    cumulative_factor,
    detect_implied,
    events_from_actions,
    factor_between,
    match_standard,
    parse_action_factor,
    reconcile,
)
from app.calc.catalogue import BANK_GROUP_CAVEAT, BY_CODE, CATALOGUE
from app.calc.metrics import shift_month_end
from app.evidence.expression import names_in
from app.models import AdjustmentKind, AdjustmentOrigin

D = Decimal


def dec(*values):
    return [D(str(v)) for v in values]


# ---- indicator maths ------------------------------------------------------------------------------


def test_sma_and_ema_against_hand_calculation():
    assert T.sma(dec(1, 2, 3, 4, 5), 3) == D(4)
    # seed SMA(1,2,3)=2; alpha=0.5 -> 2+0.5*(4-2)=3 -> 3+0.5*(5-3)=4
    assert T.ema(dec(1, 2, 3, 4, 5), 3) == D(4)
    assert T.ema_series(dec(1, 2, 3, 4, 5), 3) == dec(2, 3, 4)


def test_rsi_matches_a_hand_calculation():
    # 15 closes -> 14 changes. Gains 0.06+0.72+0.50+0.27+0.32+0.42+0.24+0.14+0.67 = 3.34,
    # losses 0.25+0.54+0.19+0.42 = 1.40 (the last change is 0). With 14 changes there is no
    # smoothing step, so RS = 3.34/1.40 and RSI = 100 - 100/(1 + RS) = 70.4641...
    closes = dec(
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
    )
    expected = D(100) - D(100) / (1 + D("3.34") / D("1.40"))
    assert abs(T.rsi(closes, 14) - expected) < D("1e-9")
    assert abs(T.rsi(closes, 14) - D("70.4641")) < D("0.0001")


def test_rsi_applies_wilder_smoothing_after_the_first_period():
    # n=2 on 1,2,1,2: changes +1,-1,+1. first avgs (2 changes): gain .5, loss .5; then the third
    # change: gain (0.5*1+1)/2=.75, loss (0.5*1+0)/2=.25 -> RS=3 -> RSI=75
    assert T.rsi(dec(1, 2, 1, 2), 2) == D(75)


def test_rsi_extremes():
    assert T.rsi(dec(*range(1, 20)), 14) == D(100)
    assert T.rsi(dec(*range(20, 1, -1)), 14) == D(0)


def test_bollinger_uses_population_standard_deviation():
    mid, upper, lower = T.bollinger(dec(*range(1, 21)), 20, 2)
    sd = (D(20 * 20 - 1) / 12).sqrt()
    assert mid == D("10.5") and abs(upper - (D("10.5") + 2 * sd)) < D("1e-20")
    flat = T.bollinger(dec(*([5] * 20)))
    assert flat == (D(5), D(5), D(5))


def test_atr_true_range_and_wilder_smoothing():
    highs, lows, closes = dec(10, 12, 13, 15), dec(8, 9, 10, 12), dec(9, 11, 12, 14)
    # true ranges: max(3,|12-9|,|9-9|)=3, max(3,|13-11|,|10-11|)=3, max(3,|15-12|,|12-12|)=3
    assert T.atr(highs, lows, closes, 3) == D(3)
    # a new bar: TR = max(20-16, |20-14|, |16-14|) = 6, so ATR = (3*2 + 6)/3 = 4
    highs2, lows2, closes2 = [*highs, D(20)], [*lows, D(16)], [*closes, D(17)]
    assert T.atr(highs2, lows2, closes2, 3) == D(4)


def test_macd_needs_enough_history_and_signs_follow_the_trend():
    with pytest.raises(T.InsufficientHistory, match="MACD"):
        T.macd(dec(*range(1, 20)))
    rising = dec(*range(1, 61))
    line, signal, hist = T.macd(rising)
    assert line > 0 and signal > 0 and hist == line - signal


def test_volatility_drawdown_and_zero_cases():
    assert T.volatility(dec(*[100 * 1.01**i for i in range(10)]), 5) < D("0.000001")
    assert T.max_drawdown(dec(100, 120, 90, 110)) == D(-25)
    assert T.max_drawdown(dec(1, 2, 3)) == D(0)
    with pytest.raises(T.InsufficientHistory):
        T.volatility(dec(1, 2, 3), 60)


def test_calendar_helpers():
    assert T.months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert T.months_before(date(2024, 3, 31), 1) == date(2024, 2, 29)
    assert T.months_before(date(2026, 1, 15), 3) == date(2025, 10, 15)


def bars(closes, start=date(2025, 1, 1), step=1, **kw):
    out = []
    for i, c in enumerate(closes):
        c = D(str(c))
        out.append(
            Bar(
                trade_date=start + timedelta(days=i * step),
                close=c,
                open=c,
                high=kw.get("high", c),
                low=kw.get("low", c),
                volume=kw.get("volume", D(1000)),
                delivery_pct=kw.get("delivery", D(50)),
                raw_close=c,
                cumulative_factor=D(1),
            )
        )
    return out


def test_reference_bar_tolerance_and_window():
    b = bars([1, 2, 3], start=date(2025, 11, 7))
    assert T.reference_bar(b, date(2025, 11, 8)).trade_date == date(2025, 11, 8)
    # target a day before the first bar: accepted (within 8 days) and the first bar is used
    assert T.reference_bar(b, date(2025, 11, 6)).trade_date == date(2025, 11, 7)
    with pytest.raises(T.InsufficientHistory, match="starts"):
        T.reference_bar(b, date(2025, 10, 1))
    with pytest.raises(T.InsufficientHistory, match="needs prices from"):
        T.window(b, date(2025, 11, 20), 365)


def test_compute_reports_why_history_is_not_enough():
    short = bars(range(1, 30))
    assert T.compute("sma_20", short, short[-1].trade_date).value == D("19.5")
    with pytest.raises(T.InsufficientHistory, match="SMA\\(200\\) needs 200"):
        T.compute("sma_200", short, short[-1].trade_date)
    with pytest.raises(T.InsufficientHistory, match="no prices"):
        T.compute("sma_20", short, date(2000, 1, 1))
    with pytest.raises(KeyError):
        T.compute("nonsense", short, short[-1].trade_date)


def test_return_uses_the_bar_a_month_earlier_and_records_the_dates():
    b = bars([100] * 40 + [110], start=date(2026, 1, 1))
    reading = T.compute("return_1m", b, b[-1].trade_date)
    assert reading.unit == "percent" and reading.value == D(10)
    assert reading.note["from"] < reading.note["to"]


def test_volume_and_delivery_need_complete_data():
    b = bars([10] * 25, volume=D(2000), delivery=D(40))
    assert T.compute("avg_volume_20d", b, b[-1].trade_date).value == D(2000)
    assert T.compute("avg_delivery_pct_20d", b, b[-1].trade_date).value == D(40)
    b[-3] = Bar(**{**b[-3].__dict__, "delivery_pct": None})
    with pytest.raises(T.InsufficientHistory, match="delivery"):
        T.compute("avg_delivery_pct_20d", b, b[-1].trade_date)


def test_52_week_high_low_need_a_full_year():
    year = bars(range(100, 100 + 372), start=date(2025, 1, 1), high=D(500), low=D(1))
    as_of = year[-1].trade_date
    assert T.compute("high_52w", year, as_of).value == D(500)
    assert T.compute("low_52w", year, as_of).value == D(1)
    with pytest.raises(T.InsufficientHistory):
        T.compute("high_52w", year[-100:], as_of)


# ---- price adjustment -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,text,expected",
    [
        ("Bonus", "1:1", (D(2), AdjustmentKind.BONUS)),
        ("Bonus", "1:2", (D("1.5"), AdjustmentKind.BONUS)),
        ("Bonus", "2:1", (D(3), AdjustmentKind.BONUS)),
        ("Split", "From Rs 10/- to Re 1/-", (D(10), AdjustmentKind.SPLIT)),
        ("Stock Split", "FV from Rs.10 to Rs.5", (D(2), AdjustmentKind.SPLIT)),
        ("Split", "10:1", None),  # a bare ratio is ambiguous for a split
        ("Split", "from Rs 10 to Rs 10", None),
        ("Bonus", "none", None),
        ("Bonus", "0:1", None),
        ("Dividend", "1:1", None),
        ("Rights", "1:5", None),
    ],
)
def test_parse_action_factor(kind, text, expected):
    assert parse_action_factor(kind, text) == expected


def action(kind, ratio, when=date(2026, 1, 15)):
    return SimpleNamespace(
        id=1, action_type=kind, ratio_text=ratio, effective_date=when, provider_code="P"
    )


def test_events_from_actions_reports_every_row_it_cannot_use():
    events, skipped = events_from_actions(
        [
            action("Bonus", "1:1"),
            action("Dividend", "5"),
            action("Split", "10:1"),
            action("Bonus", "1:1", when=None),
        ]
    )
    assert (
        len(events) == 1
        and events[0].factor == 2
        and events[0].origin is AdjustmentOrigin.CORPORATE_ACTION
    )
    assert (
        len(skipped) == 2 and "ratio not unambiguous" in skipped[0] and "no ex-date" in skipped[1]
    )


@pytest.mark.parametrize(
    "ratio,expected",
    [
        ("2", 2),
        ("1.996", 2),
        ("1.5", "1.5"),
        ("1.2501", "1.25"),
        ("1.1", None),
        ("1.37", None),
        ("0.5", "0.5"),
        ("0.1", "0.1"),
        ("1", None),
        ("10.04", 10),
    ],
)
def test_match_standard(ratio, expected):
    got = match_standard(D(ratio))
    assert (got is None) == (expected is None)
    if expected is not None:
        assert abs(got - D(str(expected))) < D("0.0001")


def row(day, close, prev=None, **kw):
    return PriceRow(
        date(2026, 3, day), D(str(close)), D(str(prev)) if prev is not None else None, **kw
    )


def test_a_split_is_implied_from_the_exchanges_reference_price():
    events = detect_implied([row(2, 1000, 998), row(3, 505, 500), row(4, 510, 505)])
    assert len(events) == 1
    e = events[0]
    assert (e.ex_date, e.factor, e.applied) == (date(2026, 3, 3), D(2), True)
    assert e.kind is AdjustmentKind.SPLIT_OR_BONUS and e.confidence == "SINGLE_EXCHANGE"
    assert "matches standard factor" in e.detail


def test_consolidation_is_the_inverse_factor():
    (e,) = detect_implied([row(2, 100, 100), row(3, 1010, 1000)])
    assert e.kind is AdjustmentKind.CONSOLIDATION and e.factor == D("0.1") and e.applied


def test_noise_and_small_gaps_are_not_events_but_unexplained_gaps_are_reported_not_applied():
    assert detect_implied([row(2, 1000), row(3, 1002, 1000.5)]) == []  # 0.05%: rounding
    (e,) = detect_implied([row(2, 1395.4), row(3, 1390, 1347)])  # 3.6%: matches no ratio
    assert e.kind is AdjustmentKind.UNEXPLAINED and not e.applied
    assert "matches no split/bonus ratio" in e.detail
    assert (
        detect_implied([row(2, 1000), row(3, 995, 993)]) == []
    )  # 0.7%: below the report threshold


def test_a_possible_missing_session_is_mentioned():
    r1 = PriceRow(date(2026, 1, 30), D(1000), D(999))
    r2 = PriceRow(date(2026, 2, 6), D(505), D(500))
    (e,) = detect_implied([r1, r2])
    assert "missing session is possible" in e.detail


def test_reconcile_prefers_stored_actions_and_confirms_across_exchanges():
    explicit = Event(
        date(2026, 3, 3),
        D(2),
        AdjustmentKind.BONUS,
        AdjustmentOrigin.CORPORATE_ACTION,
        True,
        "EXPLICIT",
        "bonus 1:1",
    )
    nse = detect_implied([row(2, 1000, 998), row(3, 505, 500)])
    bse = detect_implied([row(2, 1001, 999), row(3, 504, 500.5)])
    both = reconcile([], nse, bse)
    assert both[0].confidence == "CONFIRMED" and both[0].applied
    merged = reconcile([explicit], nse)
    applied = [e for e in merged if e.applied]
    assert [e.origin for e in applied] == [AdjustmentOrigin.CORPORATE_ACTION]
    assert (
        "agrees with the stored corporate action" in next(e for e in merged if not e.applied).detail
    )
    wrong = Event(**{**explicit.__dict__, "factor": D(3)})
    assert "DISAGREES" in next(e for e in reconcile([wrong], nse) if not e.applied).detail


def test_adjusting_prices_and_volumes_for_events():
    e1 = Event(
        date(2026, 3, 3),
        D(2),
        AdjustmentKind.SPLIT,
        AdjustmentOrigin.CORPORATE_ACTION,
        True,
        "EXPLICIT",
        "",
    )
    e2 = Event(
        date(2026, 3, 5),
        D(5),
        AdjustmentKind.SPLIT,
        AdjustmentOrigin.CORPORATE_ACTION,
        True,
        "EXPLICIT",
        "",
    )
    ignored = Event(
        date(2026, 3, 4),
        D(3),
        AdjustmentKind.UNEXPLAINED,
        AdjustmentOrigin.IMPLIED,
        False,
        "NOT_CLASSIFIED",
        "",
    )
    rows = [
        row(2, 1000, volume=100, open=1000, high=1010, low=990),
        row(3, 500, volume=200),
        row(5, 100, volume=1000),
    ]
    out = adjust(rows, [e1, e2, ignored])
    assert [b.cumulative_factor for b in out] == [D(10), D(5), D(1)]
    assert [b.close for b in out] == [D(100), D(100), D(100)]
    assert out[0].high == D(101) and out[0].volume == D(1000) and out[2].volume == D(1000)
    assert out[0].raw_close == D(1000)
    assert cumulative_factor([e1, e2], date(2026, 3, 3)) == D(5)  # ex-date itself is already new
    assert factor_between([e1, e2], date(2026, 3, 2), date(2026, 3, 4)) == D(2)
    assert factor_between([e1, e2], date(2026, 3, 5), date(2026, 3, 9)) == D(1)


# ---- catalogue ------------------------------------------------------------------------------------


def test_every_catalogue_formula_uses_exactly_its_declared_inputs():
    for metric in CATALOGUE:
        assert metric.variants, metric.code
        for variant in metric.variants:
            assert set(names_in(variant.expression)) == set(variant.inputs), (
                metric.code,
                variant.expression,
            )
        assert metric.period_types and metric.profile in ("ANY", "BANK", "NONFIN")
        assert metric.unit in (None, "inr", "percent", "x", "ratio", "days", "shares")


def test_catalogue_codes_look_right_and_derived_inputs_exist():
    codes = {m.code for m in CATALOGUE}
    for code in codes:
        category, _, name = code.partition(".")
        assert category.isupper() and name.islower(), code
    for metric in CATALOGUE:
        for variant in metric.variants:
            for r in variant.inputs.values():
                for name in r.metrics:
                    derived = name in BY_CODE
                    statement = name.split(".")[0] in (
                        "INCOME",
                        "BALANCE_SHEET",
                        "CASH_FLOW",
                        "SECURITY",
                    )
                    assert derived or statement, (metric.code, name)
    assert BANK_GROUP_CAVEAT <= codes


def test_ttm_has_two_methods_with_a_named_period():
    ttm = [m for m in CATALOGUE if m.code == "TTM.net_profit"]
    assert {m.period_types for m in ttm} == {("QUARTERLY",), ("HALF_YEAR",), ("NINE_MONTH",)}
    assert all(m.period_label == "TTM" for m in ttm)
    half = next(m for m in ttm if m.period_types == ("HALF_YEAR",))
    year = half.variants[0].inputs["year"]
    assert (year.period_type, year.shift_months) == ("ANNUAL", 6)


def test_operating_profit_measures_are_defined_from_printed_lines_for_non_financials_only():
    by = {m.code: m for m in CATALOGUE}
    # an undefined "operating margin" is still not offered: nothing here defines operating profit
    assert not [c for c in by if "operating_margin" in c.lower()]
    for code in (
        "INCOME.ebit",
        "INCOME.ebitda",
        "MARGIN.ebit_margin",
        "MARGIN.ebitda_margin",
        "RATIO.interest_cover",
        "RETURN.roce",
    ):
        assert by[code].profile == "NONFIN", code  # a bank's interest is its raw material
    ebit = by["INCOME.ebit"].variants[0]
    assert set(names_in(ebit.expression)) == {"pbt", "finance"} and "other income" in ebit.note
    ebitda = by["INCOME.ebitda"].variants[0]
    assert ebitda.inputs["depreciation"].metrics == ("INCOME.depreciation_and_amortisation",)
    assert by["RATIO.interest_cover"].requires == (("finance", ">", 0),)
    assert by["RETURN.roce"].period_types == ("ANNUAL",)


def test_shift_month_end():
    assert shift_month_end(date(2026, 3, 31), 12) == date(2025, 3, 31)
    assert shift_month_end(date(2026, 6, 30), 3) == date(2026, 3, 31)
    assert shift_month_end(date(2026, 3, 31), 36) == date(2023, 3, 31)
    assert shift_month_end(date(2026, 1, 31), 1) == date(2025, 12, 31)
