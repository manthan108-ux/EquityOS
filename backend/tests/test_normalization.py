from datetime import date
from decimal import Decimal

import pytest

from app.normalization import (
    fiscal_year_label,
    latest_expected_annual,
    latest_expected_quarter,
    normalized_value,
    parse_amount,
    parse_percent,
    parse_period_label,
    previous_annual,
    previous_quarters,
    quarter_label,
    to_inr,
)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Mar 2026", date(2026, 3, 31)),
        ("Jun 2025", date(2025, 6, 30)),
        ("Feb 2024", date(2024, 2, 29)),  # leap year
        ("March 2025", date(2025, 3, 31)),
        ("sep-2025", date(2025, 9, 30)),
        ("2025-12", date(2025, 12, 31)),
        ("  Dec 2025 ", date(2025, 12, 31)),
    ],
)
def test_period_labels(label, expected):
    assert parse_period_label(label) == expected


@pytest.mark.parametrize("label", ["", "FY2026", "2026", "Foo 2026", "2025-13", "Q1 FY26", "Mar"])
def test_unparseable_periods_return_none_not_a_guess(label):
    assert parse_period_label(label) is None


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("1", "crore", "10000000"),
        ("2.5", "Crores", "25000000.0"),
        ("1", "lakh", "100000"),
        ("3", "million", "3000000"),
        ("1", "billion", "1000000000"),
        ("7", "inr", "7"),
    ],
)
def test_unit_conversion_to_rupees(value, unit, expected):
    assert to_inr(Decimal(value), unit) == Decimal(expected)


def test_unknown_unit_is_not_assumed():
    assert to_inr(Decimal(5), "furlongs") is None
    assert normalized_value(Decimal(5), "") is None


def test_per_share_values_are_not_scaled():
    assert normalized_value(Decimal("51.47"), "inr_per_share") == Decimal("51.47")


def test_crore_arithmetic_is_exact():
    # 10,86,181 crore must not pick up floating point error
    assert to_inr(Decimal("1086181"), "crore") == Decimal("10861810000000")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,234.50", "1234.50"),
        ("(1,234.5)", "-1234.5"),
        ("-12", "-12"),
        ("Rs. 5", "5"),
        ("₹ 1,00,000", "100000"),
        (12, "12"),
        (1.5, "1.5"),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text", [None, "", "-", "NA", "N/A", "nil", "abc", "()", "1.2.3", "12abc", "Rs.", ".5x", "1e5"]
)
def test_not_available_markers_are_none_never_zero(text):
    assert parse_amount(text) is None


def test_parse_percent():
    assert parse_percent("4.39%") == (Decimal("4.39"), True)
    assert parse_percent("20.15") == (Decimal("20.15"), False)
    assert parse_percent(None) == (None, False)
    assert parse_percent("n/a") == (None, False)


def test_fiscal_year_labels():
    assert fiscal_year_label(date(2026, 3, 31)) == "FY2026"
    assert fiscal_year_label(date(2025, 12, 31)) == "FY2026"
    assert fiscal_year_label(date(2025, 4, 30)) == "FY2026"


def test_expected_annual_period_respects_the_filing_window():
    assert latest_expected_annual(date(2026, 11, 7)) == date(2026, 3, 31)
    assert latest_expected_annual(date(2026, 4, 20)) == date(2025, 3, 31)  # FY26 not due yet
    assert latest_expected_annual(date(2026, 6, 30)) == date(2026, 3, 31)


def test_expected_quarter_respects_the_filing_window():
    # Results are expected 60 days after quarter end (45-day statutory deadline plus slack).
    assert latest_expected_quarter(date(2026, 11, 7)) == date(2026, 6, 30)  # Sep not yet expected
    assert latest_expected_quarter(date(2026, 12, 1)) == date(2026, 9, 30)
    assert latest_expected_quarter(date(2026, 2, 20)) == date(2025, 9, 30)
    assert latest_expected_quarter(date(2026, 3, 5)) == date(2025, 12, 31)


def test_period_ranges():
    assert previous_annual(date(2026, 3, 31), 3) == [
        date(2026, 3, 31),
        date(2025, 3, 31),
        date(2024, 3, 31),
    ]
    assert previous_quarters(date(2026, 3, 31), 5) == [
        date(2026, 3, 31),
        date(2025, 12, 31),
        date(2025, 9, 30),
        date(2025, 6, 30),
        date(2025, 3, 31),
    ]


def test_quarter_labels_follow_the_indian_fiscal_year():
    assert quarter_label(date(2025, 6, 30)) == "Q1FY2026"
    assert quarter_label(date(2025, 9, 30)) == "Q2FY2026"
    assert quarter_label(date(2025, 12, 31)) == "Q3FY2026"
    assert quarter_label(date(2026, 3, 31)) == "Q4FY2026"
    with pytest.raises(ValueError):
        quarter_label(date(2025, 7, 31))
