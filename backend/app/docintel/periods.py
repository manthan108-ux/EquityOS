"""Parse the reporting period of a statement column from its header text.

Examples of real Indian headers:
  "Quarter ended 30.09.2026 (Unaudited)", "Three months ended September 30, 2026",
  "Six months ended 30-Sep-26", "Year ended 31.03.2026 Audited", "As at March 31, 2026",
  "FY2026", "Q2 FY2027", "Q2FY27", "H1 FY2027", "9M FY2026", "2025-26".

Rules:
  * Never guess. No date, two different dates, or a date that is not a month end -> no period.
  * Duration comes from words ("three months", "quarter", "six months", "year", ...) or from a
    fiscal label (Q/H/9M/FY). "As at" means a point in time (balance sheets).
  * Two-digit years are 20xx. Indian fiscal labels end in March (FY2026 = year to 31 Mar 2026).
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date

from app.normalization import month_end

_MONTHS = {m.lower()[:3]: i for i, m in enumerate(calendar.month_abbr) if m}
# Only real month words (full or abbreviated); "declared" or "march" inside other words never match.
_MON = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?"
)
# A two-digit year is accepted only after '-' or an apostrophe (30-Sep-26, Mar'26), never after a
# space, so "June 30" is not read as June 2030.
_YEAR = r"(?:[\s,]+(\d{4})|\s?[-']\s?(\d{2}|\d{4}))\b"

_NUMERIC_DATE = re.compile(r"\b(\d{1,2})\s?[./-]\s?(\d{1,2})\s?[./-]\s?(\d{4}|\d{2})\b")
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s-]+" + _MON + _YEAR)
_MONTH_DAY_YEAR = re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(\d{4})\b")
_MONTH_YEAR = re.compile(r"\b" + _MON + _YEAR)
# "Corresponding 3 months ended ... in the previous year" is a quarter, not a year.
_PREVIOUS_YEAR = re.compile(
    r"\b(?:in|of)\s+the\s+(?:previous|preceding|last)\s+(?:financial\s+)?year\b"
)
# "Q2 FY2027", "Q2FY27", "H1 FY27", "9M FY2026", "FY2026", "FY 2025-26". The letters FY are
# required: "Q2 2027" alone could be a calendar quarter, so it is not read.
_FISCAL = re.compile(
    r"\b(?:(q[1-4]|h[12]|9m)\s?[-']?\s?)?fy\s?[-']?\s?'?(\d{4}|\d{2})(?:\s?[-/]\s?(\d{2}))?\b"
)
_FY_RANGE = re.compile(r"\b(20\d{2})\s?[-/]\s?(\d{2})\b")

_DURATIONS = [
    (re.compile(r"\b(three|3)\s?months?\b|\bquarter\b|\bqtr\b"), 3),
    (re.compile(r"\b(six|6)\s?months?\b|\bhalf[\s-]?year\b"), 6),
    (re.compile(r"\b(nine|9)\s?months?\b"), 9),
    # "year" but not inside "half year" / "half-year"
    (re.compile(r"\b(twelve|12)\s?months?\b|(?<!half )(?<!half-)\byear\b|\bannual\b"), 12),
]
_POINT_IN_TIME = re.compile(r"\bas\s+(at|on)\b|^at\b|\bat\s+(?=\d|" + _MON + ")")


@dataclass(frozen=True)
class ColumnPeriod:
    period_end: date | None
    duration_months: int | None  # None for point-in-time columns or when unknown
    point_in_time: bool
    audited: bool | None
    problem: str | None = None  # why the period could not be read

    @property
    def ok(self) -> bool:
        return self.period_end is not None and self.problem is None


def _year(text: str) -> int:
    value = int(text)
    return 2000 + value if value < 100 else value


def _is_month_end(year: int, month: int, day: int) -> bool:
    return 1 <= month <= 12 and day == calendar.monthrange(year, month)[1]


def _dates(text: str) -> list[date | str]:
    """All dates in the text. A str entry describes something date-like that is not a month end."""
    found: list[date | str] = []
    consumed = text

    def add(year: int, month: int, day: int, raw: str) -> None:
        nonlocal consumed
        if _is_month_end(year, month, day):
            found.append(date(year, month, day))
        else:
            found.append(f"not a month end: {raw}")
        consumed = consumed.replace(raw, " ")

    for match in _NUMERIC_DATE.finditer(text):
        add(_year(match.group(3)), int(match.group(2)), int(match.group(1)), match.group(0))
    for match in list(_DAY_MONTH_YEAR.finditer(consumed)):
        year = match.group(3) or match.group(4)
        add(_year(year), _MONTHS[match.group(2)[:3]], int(match.group(1)), match.group(0))
    for match in list(_MONTH_DAY_YEAR.finditer(consumed)):
        add(_year(match.group(3)), _MONTHS[match.group(1)[:3]], int(match.group(2)), match.group(0))
    for match in _MONTH_YEAR.finditer(consumed):
        year = match.group(2) or match.group(3)
        found.append(month_end(_year(year), _MONTHS[match.group(1)[:3]]))
    return found


def _fiscal_label(text: str) -> tuple[date, int] | None:
    match = _FISCAL.search(text)
    if match:
        kind, fy = match.group(1), _year(match.group(2))
        if match.group(3):  # range form "FY2025-26": must be consecutive years; ends in the second
            if (fy + 1) % 100 != int(match.group(3)):
                return None
            fy += 1
        if kind is None:
            return date(fy, 3, 31), 12
        if kind == "9m":
            return date(fy - 1, 12, 31), 9
        if kind.startswith("h"):
            return (date(fy - 1, 9, 30), 6) if kind == "h1" else (date(fy, 3, 31), 6)
        quarter = int(kind[1])
        month = {1: 6, 2: 9, 3: 12, 4: 3}[quarter]
        year = fy if quarter == 4 else fy - 1
        return month_end(year, month), 3
    match = _FY_RANGE.search(text)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if (start + 1) % 100 == end:
            return date(start + 1, 3, 31), 12
    return None


# Under the project's April-March fiscal-year assumption, the month a period of a given length can
# end in. A header that says "year ended 30 June" is therefore a misreading (typically a quarter
# column that picked up the neighbouring "Year ended" heading), and its numbers would be stored as
# an annual figure. Such a column is withheld instead.
_ENDING_MONTHS = {12: (3,), 6: (9,), 9: (12,), 3: (3, 6, 9, 12)}
_LENGTH_NAMES = {12: "year", 6: "six-month", 9: "nine-month", 3: "quarter"}


def fiscal_alignment_problem(period_end: date | None, duration_months: int | None) -> str | None:
    """Why this period length cannot end on this date in an April-March fiscal year, if so."""
    if period_end is None or duration_months not in _ENDING_MONTHS:
        return None
    if period_end.month in _ENDING_MONTHS[duration_months]:
        return None
    return (
        f"a {_LENGTH_NAMES[duration_months]} period cannot end on {period_end:%d %B %Y} in an "
        "April-March fiscal year; the header was probably read against the wrong column"
    )


def stated_duration(text: str) -> int | None:
    """The one period length (in months) the text names ('for the year ended ...'), else None.

    Used for a statement-wide heading that states the length once for every column. If the text
    names several different lengths it says nothing definite, so the answer is None.
    """
    low = " ".join(text.lower().split())
    low = re.sub(r"(?<![a-z])ear ended", "year ended", low)
    found = {
        months for pattern, months in _DURATIONS if pattern.search(_PREVIOUS_YEAR.sub(" ", low))
    }
    return found.pop() if len(found) == 1 else None


def parse_column_header(text: str) -> ColumnPeriod:
    """Read the period of one statement column from its (possibly multi-line) header text."""
    low = " ".join(text.lower().split())
    # A font that drops the first glyph of a heading: "ear ended" is "year ended" ("ear" is not a
    # word that can precede "ended" in a statement heading).
    low = re.sub(r"(?<![a-z])ear ended", "year ended", low)
    low = re.sub(
        r"(?<=\d)(?=(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?![a-z]))", " ", low
    )
    low = re.sub(
        r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)(?=\d{4}\b)", r"\1 ", low
    )
    # Table cells that lost the space between day, month and year ("30June 2026", "June2026").
    audited: bool | None = None
    if re.search(r"\bun[\s-]?audited\b|\breviewed\b|\blimited review\b", low):
        audited = False
    elif re.search(r"\baudited\b", low):
        audited = True
    point = bool(_POINT_IN_TIME.search(low))

    for_duration = _PREVIOUS_YEAR.sub(" ", low)
    durations = {months for pattern, months in _DURATIONS if pattern.search(for_duration)}
    dates = _dates(low)
    bad = [d for d in dates if isinstance(d, str)]
    good = sorted({d for d in dates if isinstance(d, date)})
    fiscal = None if good or bad else _fiscal_label(low)

    if bad and not good:
        return ColumnPeriod(None, None, point, audited, bad[0])
    if fiscal:
        end, months = fiscal
        if durations and months not in durations:
            return ColumnPeriod(
                None, None, point, audited, "duration words contradict fiscal label"
            )
        return ColumnPeriod(end, None if point else months, point, audited)
    if not good:
        return ColumnPeriod(None, None, point, audited, "no date in header")
    if len(good) > 1:
        return ColumnPeriod(None, None, point, audited, "more than one date in header")
    if point:
        if durations:
            return ColumnPeriod(None, None, point, audited, "both 'as at' and a duration")
        return ColumnPeriod(good[0], None, True, audited)
    if len(durations) > 1:
        return ColumnPeriod(good[0], None, False, audited, "more than one duration in header")
    labelled = _label_durations(low, good[0])
    if durations and labelled and labelled != durations:
        return ColumnPeriod(good[0], None, False, audited, "duration words contradict period label")
    if not durations:
        if len(labelled) == 1:  # "Period ended September 30, 2026 (H1-2027)"
            return ColumnPeriod(good[0], labelled.pop(), False, audited)
        return ColumnPeriod(good[0], None, False, audited, "duration not stated")
    return ColumnPeriod(good[0], durations.pop(), False, audited)


_PERIOD_LABEL = re.compile(r"\b(q[1-4]|h[12]|9m|fy)\s?[-']?\s?(fy)?\s?'?(\d{4}|\d{2})\b")


def _label_durations(text: str, period_end: date) -> set[int]:
    """Durations implied by period labels ("Q2-2027", "H1-2027", "FY2026") that AGREE with the date.

    A label without "FY" can be fiscal or calendar; it is used only if exactly the reading that
    matches the column's own date exists. Labels that match neither reading are ignored.
    """
    found: set[int] = set()
    for match in _PERIOD_LABEL.finditer(text):
        kind, has_fy, year = match.group(1), match.group(2), _year(match.group(3))
        readings: list[tuple[date, int]] = []
        if kind == "fy":
            readings.append((date(year, 3, 31), 12))
        else:
            fiscal = _fiscal_label(f"{kind} fy{year}")
            if fiscal:
                readings.append(fiscal)
            if not has_fy:  # calendar reading: Q2-2027 = Apr-Jun 2027, H1-2027 = Jan-Jun 2027
                if kind.startswith("q"):
                    readings.append((month_end(year, int(kind[1]) * 3), 3))
                elif kind == "h1":
                    readings.append((month_end(year, 6), 6))
                elif kind == "h2":
                    readings.append((month_end(year, 12), 6))
                elif kind == "9m":
                    readings.append((month_end(year, 9), 9))
        found.update(months for end, months in readings if end == period_end)
    return found
