# ruff: noqa: E501 - patterns are regular expressions; splitting them hurts readability
"""Operating disclosures from prose: vehicle sales volumes, employee attrition, utilisation.

These figures are stated in sentences in press releases, fact sheets and annual reports
("The Company achieved its highest-ever quarterly total sales volume of 696,654 units ... Q2
(July-September) FY 2026-27", "voluntary IT services' attrition at 13.7% for FY 2026"). A reader
takes one only when ALL of these hold, and otherwise reads nothing:

* the sentence names the measure exactly (total sales volume / attrition / utilisation) and gives
  ONE number for it;
* the PERIOD is named in the text itself, as a quarter, a financial year or a month; a figure with
  no stated period is never assigned one;
* the context is not a different thing with the same name: actuarial "attrition rate (for
  gratuity)", "utilisation of funds / reserves", "capacity utilisation" are refused;
* only born-digital text is read (the pipeline keeps scanned pages on the statement route).

Where a document states a measure in more than one way (voluntary and total attrition, utilisation
including and excluding trainees, domestic and export volumes), each variant is kept under its own
DIMENSION and never blended.

Nothing here estimates, interpolates or annualises: a value is what the page printed.
"""

import calendar
import re
from datetime import date
from decimal import Decimal

from app.docintel import taxonomy
from app.docintel.layout import decimals_of, parse_cell
from app.docintel.periods import ColumnPeriod, parse_column_header
from app.docintel.statements import CellFact, ColumnInfo, StatementBlock

OPERATING = taxonomy.OPERATING
DETECTION = "kpi_text"  # routed like the other key-figure blocks (see docintel/kpi.py)

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | {
    m.lower(): i for i, m in enumerate(calendar.month_abbr) if m
}
_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
)
_MONTH_YEAR = rf"({_MONTH})\s*,?\s*(\d{{4}})"
_FY = r"(?:FY|F\.Y\.|fiscal(?: year)?|financial year)\s*'?(\d{4}(?:\s?-\s?\d{2,4})?|\d{2})"
_QUARTER = r"Q([1-4])\s*(?:\([A-Za-z\- ]{3,25}\))?\s*,?\s*(?:FY|F\.Y\.)\s*'?(\d{4})\s?-\s?(\d{2,4})|Q([1-4])\s*-?\s*FY\s*'?(\d{2,4})"
_NOT_EMPLOYEE = re.compile(
    r"(?i)gratuity|pension|actuar|mortality|discount rate|salary growth|assumption|estimate|plan assets"
)
_NOT_UTILISATION = re.compile(
    r"(?i)capacity|funds?\b|reserve|csr|asset|plant|infrastructure|proceeds|amount|credit|facility|limit"
)


def clean(text: str) -> str:
    """Page text with the spacing artefacts of PDF extraction removed ("13. 7 %" -> "13.7%")."""
    t = " ".join(text.replace("’", "'").replace(" ", " ").split())
    t = re.sub(r"(\d)\s*\.\s+(\d)", r"\1.\2", t)
    t = re.sub(r"(\d)\s+%", r"\1%", t)
    return t


def _month_end(name: str, year: int) -> date:
    month = _MONTHS[name.lower()]
    return date(year, month, calendar.monthrange(year, month)[1])


def _fy_period(label: str) -> ColumnPeriod | None:
    """'2026' / '2025-26' / '26' -> the financial year ended 31 March."""
    label = label.replace(" ", "")
    m = re.fullmatch(r"(\d{4})-(\d{2,4})", label)
    if m:
        first, second = int(m.group(1)), int(m.group(2)) % 100
        if (first + 1) % 100 != second:
            return None
        return ColumnPeriod(date(first + 1, 3, 31), 12, False, None)
    year = int(label) + (2000 if len(label) == 2 else 0)
    return ColumnPeriod(date(year, 3, 31), 12, False, None)


def _quarter_period(m: re.Match) -> ColumnPeriod | None:
    if m.group(1):
        q, y1, y2 = m.group(1), int(m.group(2)), m.group(3)
        # "FY 2026-27" ends in the second year; a bare "FY 2027" ends in it too
        year = y1 + 1 if (y1 + 1) % 100 == int(y2) % 100 else y1
    else:
        q, year = m.group(4), int(m.group(5)) + (2000 if len(m.group(5)) == 2 else 0)
    period = parse_column_header(f"Q{q}FY{year}")
    return period if period.period_end and not period.problem else None


def _periods_in(text: str) -> list[tuple[str, ColumnPeriod, str]]:
    """Every period named in the text: (kind, period, dimension-hint)."""
    found: list[tuple[str, ColumnPeriod, str]] = []
    for m in re.finditer(_QUARTER, text, re.I):
        p = _quarter_period(m)
        if p:
            found.append(("quarter", p, ""))
    for m in re.finditer(_FY, text, re.I):
        # "FY 2026-27" inside a quarter label was already taken as a quarter
        if re.search(
            r"Q[1-4][^.]{0,40}" + re.escape(m.group(0)),
            text[max(0, m.start() - 60) : m.end()],
            re.I,
        ):
            continue
        p = _fy_period(m.group(1))
        if p:
            found.append(("year", p, ""))
    for m in re.finditer(_MONTH_YEAR, text):
        year = int(m.group(2))
        if 2000 <= year <= 2100:
            found.append(
                ("month", ColumnPeriod(_month_end(m.group(1), year), None, True, None), "month")
            )
    return found


def _single_period(*windows: str) -> tuple[ColumnPeriod, str] | None:
    """The one period named in the first window that names any; None if it names two different ones."""
    for text in windows:
        periods = _periods_in(text)
        distinct = {(p.period_end, p.duration_months) for _, p, _ in periods}
        if len(distinct) == 1:
            kind, period, hint = periods[0]
            return period, hint
        if len(distinct) > 1:
            return None
    return None


def _block(page: int, period: ColumnPeriod, label: str, unit: str | None) -> StatementBlock:
    return StatementBlock(
        page_number=page,
        statement=OPERATING,
        title="Operating disclosure stated in the text",
        detection=DETECTION,
        basis=None,
        unit=unit,
        columns=[ColumnInfo(0, 0.0, 0.0, label, period)],
        facts=[],
    )


# ----------------------------------------------------------------------------- the measures

_VOLUME_NUM = r"([\d]{1,3}(?:,\d{2,3})+|\d{4,})"
_VOLUME = [
    # "... total sales volume of 696,654 units" (the period is named earlier in the same passage)
    re.compile(
        rf"(?i)\b(?:quarterly |monthly |highest-ever (?:quarterly |monthly )?)?total\s+(?:vehicle\s+)?(?:sales|dispatch(?:es)?|volumes?)(?:\s+volumes?)?\s+(?:of|was|were|at|stood at)\s+{_VOLUME_NUM}\s+(?:units|vehicles)"
    ),
    # "total sales of 1,89,123 units in October 2026"
    re.compile(
        rf"(?i)\btotal\s+(?:vehicle\s+)?(?:sales|dispatch(?:es)?|volumes?)[^.]{{0,40}}?\b{_VOLUME_NUM}\s+(?:units|vehicles)"
    ),
]
_ATTRITION = re.compile(
    r"(?i)\b(voluntary\s+)?(?:(?:IT\s+services|employee)['\s]+)?attrition(?:\s+rate)?\s*(\(LTM\)|\(last twelve months\))?\s+(?:at|of|was|stood at|is|stands at)\s+(\d+(?:\.\d+)?)%"
)
_UTILISATION = re.compile(
    r"(?i)\butili[sz]ation\s*(\((?:including|excluding)\s+trainees\))?\s+(?:at|of|was|stood at|is|stands at)\s+(\d+(?:\.\d+)?)%"
)


def _fact(key: str, number: str, label: str, page: int, dimension: str = "") -> CellFact:
    value = parse_cell(number)
    return CellFact(
        key=key,
        raw_label=label[:120],
        row_index=0,
        column_index=0,
        raw_value=number,
        value=value,
        decimals=decimals_of(number),
        bbox=None,
        page_number=page,
        dimension=dimension or None,
    )


def extract_page_operating(page_number: int, text: str) -> list[StatementBlock]:
    """Operating-disclosure blocks of one born-digital page (possibly none)."""
    t = clean(text)
    blocks: dict[tuple, StatementBlock] = {}

    def add(
        key: str, number: str, period: ColumnPeriod, label: str, unit: str | None, dim: str
    ) -> None:
        ident = (period.period_end, period.duration_months, unit)
        block = blocks.get(ident)
        if block is None:
            block = _block(page_number, period, label, unit)
            blocks[ident] = block
        existing = [f for f in block.facts if f.key == key and (f.dimension or "") == dim]
        value = parse_cell(number)
        if existing:
            if existing[0].value != value:  # two different numbers for one measure and period
                existing[0].duplicate = True
            return
        block.facts.append(_fact(key, number, label, page_number, dim))
        block.facts[-1].row_index = len(block.facts) - 1

    for pattern in _VOLUME:
        for m in pattern.finditer(t):
            before, after = t[max(0, m.start() - 320) : m.start()], t[m.end() : m.end() + 90]
            # the period is named after the figure ("... units in October 2026") or just before it
            found = _single_period(m.group(0) + " " + after[:60], before)
            if found is None:
                continue
            period, hint = found
            add("vehicle_volumes", m.group(m.lastindex), period, m.group(0), "units", hint)
    for m in _ATTRITION.finditer(t):
        around = t[max(0, m.start() - 140) : m.end() + 140]
        if _NOT_EMPLOYEE.search(around):
            continue  # an actuarial assumption, not the workforce's attrition
        found = _single_period(t[m.end() : m.end() + 70], t[max(0, m.start() - 200) : m.start()])
        if found is None:
            continue
        dim = "voluntary" if m.group(1) else ""
        if m.group(2):
            dim = "ltm"
        add("attrition", m.group(3), found[0], m.group(0), None, dim)
    for m in _UTILISATION.finditer(t):
        around = t[max(0, m.start() - 60) : m.start()]
        if _NOT_UTILISATION.search(around):
            continue
        found = _single_period(t[m.end() : m.end() + 70], t[max(0, m.start() - 200) : m.start()])
        if found is None:
            continue
        dim = ""
        if m.group(1):
            dim = (
                "including_trainees" if "including" in m.group(1).lower() else "excluding_trainees"
            )
        add("utilization", m.group(2), found[0], m.group(0), None, dim)

    out = []
    for block in blocks.values():
        # percentages must be percentages
        block.facts = [
            f
            for f in block.facts
            if f.value is not None
            and (f.key == "vehicle_volumes" or Decimal(0) < f.value <= Decimal(100))
            and not (f.key == "vehicle_volumes" and f.value <= 0)
        ]
        if block.facts:
            out.append(block)
    return out
