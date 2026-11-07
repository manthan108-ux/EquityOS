"""Deterministic normalization of amounts, units and reporting periods.

Principles:
  * Never guess. An unknown unit or unparseable period yields None, not a default.
  * The original value is always kept by the caller; this module only derives the normalized one.
  * Indian fiscal years end on 31 March; quarters end Jun / Sep / Dec / Mar.
"""

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal

# Multipliers to absolute rupees. Per-share and percentage units are NOT convertible to rupees.
UNIT_TO_INR: dict[str, Decimal] = {
    "inr": Decimal(1),
    "rupee": Decimal(1),
    "rupees": Decimal(1),
    "thousand": Decimal(10) ** 3,
    "lakh": Decimal(10) ** 5,
    "lakhs": Decimal(10) ** 5,
    "lac": Decimal(10) ** 5,
    "million": Decimal(10) ** 6,
    "mn": Decimal(10) ** 6,
    "crore": Decimal(10) ** 7,
    "crores": Decimal(10) ** 7,
    "cr": Decimal(10) ** 7,
    "billion": Decimal(10) ** 9,
    "bn": Decimal(10) ** 9,
}
PER_SHARE_UNITS = {"inr_per_share", "per_share"}

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_LABEL = re.compile(r"^\s*([A-Za-z]{3,9})[\s\-_,/]*(\d{4})\s*$")
_ISO_MONTH = re.compile(r"^\s*(\d{4})-(\d{2})\s*$")

# Statutory result deadlines (SEBI LODR): quarterly results within 45 days, annual within 60 days.
# We allow extra slack before declaring a period "expected", so late filers are not flagged early.
QUARTERLY_LAG = timedelta(days=60)
ANNUAL_LAG = timedelta(days=75)


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_period_label(label: str) -> date | None:
    """'Mar 2026' -> 2026-03-31. Returns None if the label is not a month-year."""
    if not label:
        return None
    iso = _ISO_MONTH.match(label)
    if iso and 1 <= int(iso.group(2)) <= 12:
        return month_end(int(iso.group(1)), int(iso.group(2)))
    match = _LABEL.match(label)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    return month_end(int(match.group(2)), month) if month else None


def to_inr(value: Decimal, unit: str) -> Decimal | None:
    """Convert a monetary amount to absolute rupees; None if the unit is unknown or non-monetary."""
    factor = UNIT_TO_INR.get(unit.strip().lower())
    return value * factor if factor is not None else None


def normalized_value(value: Decimal, unit: str) -> Decimal | None:
    """Value in rupees for monetary units, unchanged for per-share units, None otherwise."""
    if unit.strip().lower() in PER_SHARE_UNITS:
        return value
    return to_inr(value, unit)


_CURRENCY = re.compile(r"(?i)(?:\u20b9|\brs\b\.?|\binr\b)")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_NOT_AVAILABLE = {"", "-", "--", "na", "n/a", "n.a.", "nil"}


def parse_amount(text: str | int | float | Decimal | None) -> Decimal | None:
    """Parse '1,234.50', '(1,234.5)' (negative), '-12', 'Rs. 5', '₹ 1,00,000'.

    '-', '', 'NA' mean not available and return None (never zero). Anything that is not
    unambiguously a number after removing currency markers and separators also returns None.
    """
    if text is None:
        return None
    if isinstance(text, Decimal):
        return text
    if isinstance(text, (int, float)):
        return Decimal(str(text))
    raw = text.strip()
    if raw.lower() in _NOT_AVAILABLE:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = _CURRENCY.sub("", raw.strip("()")).replace(",", "").replace(" ", "")
    if not _NUMBER.match(cleaned):
        return None
    value = Decimal(cleaned)
    return -value if negative and value > 0 else value


def parse_percent(text: str | None) -> tuple[Decimal | None, bool]:
    """'4.39%' -> (4.39, True); '20.15' -> (20.15, False)."""
    if text is None:
        return None, False
    raw = text.strip()
    is_percent = raw.endswith("%")
    return parse_amount(raw.rstrip("%")), is_percent


def fiscal_year_label(period_end: date) -> str:
    """Indian fiscal year label: the year in which it ends. 2026-03-31 -> 'FY2026'."""
    return f"FY{period_end.year if period_end.month <= 3 else period_end.year + 1}"


def latest_expected_annual(today: date) -> date:
    """Most recent fiscal year end (31 March) whose annual results should already be published."""
    year = today.year
    while True:
        candidate = date(year, 3, 31)
        if candidate + ANNUAL_LAG <= today:
            return candidate
        year -= 1


def latest_expected_quarter(today: date) -> date:
    """Most recent quarter end whose results should already be published."""
    for offset in range(0, 8):
        month_index = today.year * 12 + (today.month - 1) - offset
        year, month = divmod(month_index, 12)
        month += 1
        if month in (3, 6, 9, 12):
            candidate = month_end(year, month)
            if candidate + QUARTERLY_LAG <= today:
                return candidate
    raise AssertionError("unreachable")


def previous_annual(period_end: date, count: int) -> list[date]:
    """`count` fiscal year ends ending at period_end, newest first."""
    return [date(period_end.year - i, 3, 31) for i in range(count)]


def previous_quarters(period_end: date, count: int) -> list[date]:
    """`count` quarter ends ending at period_end, newest first."""
    out, year, month = [], period_end.year, period_end.month
    for _ in range(count):
        out.append(month_end(year, month))
        month -= 3
        if month <= 0:
            month += 12
            year -= 1
    return out


def quarter_label(period_end: date) -> str:
    """Canonical fiscal-quarter label: Jun=Q1, Sep=Q2, Dec=Q3, Mar=Q4 of the March-ending year.

    2025-06-30 -> 'Q1FY2026', 2026-03-31 -> 'Q4FY2026'. Documents use this exact form in
    `financial_period`, so coverage can match them to required quarters.
    """
    quarter = {6: 1, 9: 2, 12: 3, 3: 4}.get(period_end.month)
    if quarter is None:
        raise ValueError(f"{period_end} is not a quarter end")
    return f"Q{quarter}{fiscal_year_label(period_end)}"


def period_label(period_end: date, months: int) -> str:
    """Canonical label of a flow period ending at `period_end` and lasting `months`.

    3 -> 'Q2FY2027'; 12 -> 'FY2027'. The cumulative year-to-date periods of an Indian fiscal year
    (April start): 6 months ending September -> 'H1FY2027'; 9 months ending December -> '9MFY2027'.
    Anything else (for example six months ending March, which is not year-to-date) has no label
    and raises ValueError.
    """
    if months == 3:
        return quarter_label(period_end)
    if months == 12:
        return fiscal_year_label(period_end)
    if months == 6 and period_end.month == 9:
        return f"H1{fiscal_year_label(period_end)}"
    if months == 9 and period_end.month == 12:
        return f"9M{fiscal_year_label(period_end)}"
    raise ValueError(f"no fiscal label for a {months}-month period ending {period_end}")


def months_between(earlier_end: date, later_end: date) -> int:
    """Whole months from one month-end to a later one."""
    return (later_end.year - earlier_end.year) * 12 + (later_end.month - earlier_end.month)
