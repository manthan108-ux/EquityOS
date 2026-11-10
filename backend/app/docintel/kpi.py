# ruff: noqa: E501 - patterns are regular expressions; splitting them hurts readability
"""Key performance indicators from results press releases, key-parameter sheets and presentations.

These documents state the figures a bank is judged on (net interest margin, gross and net NPA,
capital adequacy, CASA ratio, deposits, gross advances) in small tables or in plain sentences, not
in the audited statements. Two deterministic readers cover them:

* the TABLE reader: a header line carrying at least two period labels ("Q2FY26 Q1FY27 Q2FY27",
  "Sep 25 Jun 26 Sep 26", "30-Sep-25 30-Jun-26 30-Sep-26") defines the columns; each following row
  whose label is a known KPI gives one value per column. A value is assigned to the nearest
  header, and a row is dropped if two values land under one period;
* the SENTENCE reader: a known KPI, a single number and an explicit date in one sentence
  ("Gross advances were at ₹ 32,195 billion as of September 30, 2026").

Rules that keep it honest:

* only born-digital text is read (scanned or OCR pages go through the statement route, which holds
  values to arithmetic checks); a label must match a KPI exactly, so sub-portfolio rows ("Retail
  advances") and different measures ("GNPA as a ratio of customer assets") are ignored;
* two measures with the same name are kept apart by a DIMENSION (domestic / whole bank NIM; NIM on
  assets / on interest-earning assets), never blended;
* implausible values (a ratio above 100, a net NPA ratio above the gross one) and ratios that do not
  agree with the amounts printed beside them are dropped with a reason;
* the result is the same block structure the statement reader produces, so page, column header,
  period, unit and validation notes are stored exactly like every other extracted value.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.docintel import taxonomy
from app.docintel.layout import (
    Line,
    Word,
    decimals_of,
    group_lines,
    parse_cell,
)
from app.docintel.periods import ColumnPeriod, parse_column_header
from app.docintel.statements import CellFact, ColumnInfo, StatementBlock

DETECTION_TABLE = "kpi_table"
DETECTION_TEXT = "kpi_text"
MAX_ROWS_AFTER_HEADER = 60
MAX_NIM = Decimal(10)

_PERIODISH = re.compile(
    r"(?i)(\bq[1-4]\s?-?\s?fy|\bh[12]\s?-?\s?fy|\bfy\s?-?\s?\d{2}|\b9m\s?-?\s?fy"
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-'’.]*\d{2,4}\b"
    r"|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b)"
)
_FISCAL_MARK = re.compile(r"(?i)\b(q[1-4]|h[12]|9m|fy)")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
# the words a period label may be made of; anything else ("YoY", "%", "bps") ends the label
_PERIOD_WORD = re.compile(
    rf"(?i)^(?:q[1-4]|h[12]|9m|fy|{_MONTH}|\d{{1,2}}|\d{{4}}|'?\d{{2}}|{_MONTH}[-'’.]?\d{{2,4}}"
    rf"|q[1-4]-?fy\d{{2,4}}|h[12]-?fy\d{{2,4}}|9m-?fy\d{{2,4}}|fy-?\d{{2,4}}"
    rf"|\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{2,4}}|\d{{1,2}}[-.]{_MONTH}[-.]\d{{2,4}})$"
)
_UNIT_RE = [
    (re.compile(r"(?i)lakh\s+crore"), None),  # unsupported scale: amounts are skipped
    (re.compile(r"(?i)\btrillion\b|\btn\b"), None),
    (re.compile(r"(?i)\bcrores?\b|\bcr\b"), "crore"),
    (re.compile(r"(?i)\bbillion\b|\bbn\b"), "billion"),
    (re.compile(r"(?i)\bmillion\b|\bmn\b"), "million"),
    (re.compile(r"(?i)\blakhs?\b"), "lakh"),
]
_FOOTNOTE = re.compile(r"[\^*†‡§]+|\s\d$")


@dataclass(frozen=True)
class Kpi:
    key: str
    kind: str  # percent | amount
    label: re.Pattern
    # dimension keywords checked in order against the raw label: (regex, dimension name)
    dimensions: tuple[tuple[re.Pattern, str], ...] = ()


def _p(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


KPIS: tuple[Kpi, ...] = (
    Kpi(
        "net_interest_margin",
        "percent",
        _p(
            r"^(?:(?:whole bank|domestic|global|overall|total) )?(?:nim|net interest margin)"
            r"(?: (?:nim|whole bank|domestic|global|overall|assets|interest earning assets|"
            r"on total assets|on interest earning assets|calculated|reported))*$"
        ),
        (
            (_p(r"interest earning"), "interest_earning_assets"),
            (_p(r"domestic"), "domestic"),
            (_p(r"whole bank|global|overall"), "whole_bank"),
            (_p(r"\bassets\b"), "total_assets"),
        ),
    ),
    Kpi(
        "gross_npa_ratio",
        "percent",
        _p(
            r"^(?:total )?(?:gross npa|gnpa|gross non performing assets)(?: ratio)?"
            r"(?: (?:as a ratio of|to|as a of|of) gross (?:customer )?advances)?$"
        ),
    ),
    Kpi(
        "net_npa_ratio",
        "percent",
        _p(
            r"^(?:total )?(?:net npa|nnpa|net non performing assets)(?: ratio)?"
            r"(?: (?:as a ratio of|to|as a of|of) net (?:customer )?advances)?$"
        ),
    ),
    Kpi(
        "capital_adequacy_ratio",
        "percent",
        _p(
            r"^(?:total |overall )?(?:car|crar|capital adequacy ratio|capital adequacy|"
            r"capital to risk weighted assets ratio)(?: (?:car|crar|basel iii))*$"
        ),
    ),
    Kpi("casa_ratio", "percent", _p(r"^casa(?: deposits?)? ratio$")),
    Kpi("gross_advances", "amount", _p(r"^gross (?:advances|loans)$")),
    Kpi("total_deposits", "amount", _p(r"^(?:total )?(?:eop )?deposits$")),
    Kpi("gross_npa_amount", "amount", _p(r"^(?:gnpa|gross npa)$")),
    Kpi("net_npa_amount", "amount", _p(r"^(?:nnpa|net npa)$")),
)
PERCENT_KEYS = {k.key for k in KPIS if k.kind == "percent"}


_PUNCT = re.compile(r"[,()%\-–:]+")


def normalise_label(text: str) -> str:
    """Lower-case, footnote marks and punctuation removed: "NIM, % (Domestic)" -> "nim domestic"."""
    label = _FOOTNOTE.sub("", text.replace("’", "'")).lower()
    return " ".join(_PUNCT.sub(" ", label).split())


def match_kpis(label: str) -> list[tuple[Kpi, str]]:
    """Every key figure the label can mean (a bare "GNPA" is a ratio OR an amount)."""
    norm = normalise_label(label)
    found = []
    for kpi in KPIS:
        if kpi.label.match(norm):
            dimension = ""
            for pattern, name in kpi.dimensions:
                if pattern.search(norm):
                    dimension = name
                    break
            found.append((kpi, dimension))
    return found


def match_kpi(label: str) -> tuple[Kpi, str] | None:
    found = match_kpis(label)
    return found[0] if found else None


# ----------------------------------------------------------------------------- headers


@dataclass
class Anchor:
    x: float  # centre of the header cell
    text: str
    period: ColumnPeriod | None  # None = a header that is not a period (QoQ %, YoY %, bps)


_MONTH_YEAR = re.compile(rf"(?i)^({_MONTH})[-'’.\s]*(\d{{4}}|\d{{2}})$")
_MONTH_NUM = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}


def _period_of(text: str) -> ColumnPeriod | None:
    if not _PERIODISH.search(text):
        return None
    bare = _MONTH_YEAR.match(text.strip())
    if bare:  # "Sep 26": a value as at that month end (balance and asset-quality figures)
        year = int(bare.group(2))
        year += 2000 if year < 100 else 0
        month = _MONTH_NUM[bare.group(1)[:3].lower()]
        return ColumnPeriod(
            date(year, month, calendar.monthrange(year, month)[1]), None, True, None
        )
    period = parse_column_header(text)
    if period.period_end is not None and period.problem is None:
        return period
    if period.problem == "duration not stated" and not _FISCAL_MARK.search(text):
        # a bare date over a key-figure column ("30-Sep-26"): the value is as at that date
        again = parse_column_header("as at " + text)
        if again.period_end is not None and again.problem is None:
            return again
    return None


def header_anchors(line: Line) -> list[Anchor]:
    """The header cells of a line: greedy longest period label first, other text as ignored cells."""
    words = line.words
    anchors: list[Anchor] = []
    i = 0
    pending: list[Word] = []

    def flush() -> None:
        if pending:
            x0, x1 = pending[0].x0, pending[-1].x1
            anchors.append(Anchor((x0 + x1) / 2, " ".join(w.text for w in pending), None))
            pending.clear()

    while i < len(words):
        matched = False
        for n in (1, 2, 3):
            chunk = words[i : i + n]
            if len(chunk) < n or not all(_PERIOD_WORD.match(w.text) for w in chunk):
                continue
            text = " ".join(w.text for w in chunk)
            period = _period_of(text)
            if period is not None:
                flush()
                anchors.append(Anchor((chunk[0].x0 + chunk[-1].x1) / 2, text, period))
                i += n
                matched = True
                break
        if not matched:
            if pending and words[i].x0 - pending[-1].x1 > 12:
                flush()
            pending.append(words[i])
            i += 1
    flush()
    return anchors


def _is_header(anchors: list[Anchor]) -> bool:
    periods = [a for a in anchors if a.period]
    return (
        len(periods) >= 2
        and len({(a.period.period_end, a.period.duration_months) for a in periods}) >= 2
    )


def _unit_near(lines: list[Line], index: int) -> tuple[str | None, bool]:
    """(unit, stated) from the header line and up to three lines above it."""
    for line in lines[max(0, index - 3) : index + 1][::-1]:
        text = line.text
        for pattern, unit in _UNIT_RE:
            if pattern.search(text) and re.search(r"(?i)₹|rs\.?|inr|`|in\s", text):
                return unit, True
    return None, False


# ----------------------------------------------------------------------------- tables


@dataclass
class _Group:
    page: int
    anchors: list[Anchor]
    unit: str | None
    unit_stated: bool
    header_text: str
    rows: list[tuple[int, str, list[Word]]]  # (line index, label, value words)


def _value_words(line: Line, left: float) -> tuple[str, list[Word]]:
    values = [w for w in line.words if w.x0 >= left and parse_cell(w.text) is not None]
    if not values:
        return "", []
    first = min(w.x0 for w in values)
    label = " ".join(w.text for w in line.words if w.x1 <= first + 0.5 and w.x0 < first)
    return label, values


def _tolerance(anchors: list[Anchor]) -> float:
    xs = sorted(a.x for a in anchors)
    gaps = [b - a for a, b in zip(xs, xs[1:], strict=False) if b - a > 1]
    return (min(gaps) * 0.6) if gaps else 30.0


def table_blocks(page_number: int, words: list[Word]) -> list[StatementBlock]:
    lines = group_lines(words)
    blocks: list[StatementBlock] = []
    h = 0
    while h < len(lines):
        anchors = header_anchors(lines[h])
        if not _is_header(anchors):
            h += 1
            continue
        # a second header line directly below can add ignored cells ("QoQ, / bps")
        extra: list[Anchor] = []
        end = min(len(lines), h + 1 + MAX_ROWS_AFTER_HEADER)
        stop = end
        for j in range(h + 1, end):
            if _is_header(header_anchors(lines[j])):
                stop = j
                break
        unit, stated = _unit_near(lines, h)
        left = min(a.x for a in anchors) - 80
        rows = []
        for j in range(h + 1, stop):
            label, values = _value_words(lines[j], left)
            if label and values:
                rows.append((j, label, values))
        for j in range(h + 1, min(h + 2, stop)):
            for a in header_anchors(lines[j]):
                if abs(min(anchors, key=lambda e: abs(e.x - a.x)).x - a.x) < 25:
                    continue  # a second header line of a column that already has an anchor
                if not a.period and not any(
                    w.x0 >= left and parse_cell(w.text) is not None for w in lines[j].words
                ):
                    extra.append(a)
        group = _Group(
            page_number, [*anchors, *extra], unit, stated, " ".join(a.text for a in anchors), rows
        )
        block = _group_block(group)
        if block is not None:
            blocks.append(block)
        h = stop
    return blocks


def _group_block(group: _Group) -> StatementBlock | None:
    period_anchors = [a for a in group.anchors if a.period]
    column_of = {id(a): i for i, a in enumerate(period_anchors)}
    columns = [
        ColumnInfo(
            i,
            a.x - 20,
            a.x + 20,
            a.text,
            a.period,
        )
        for i, a in enumerate(period_anchors)
    ]
    tol = _tolerance(group.anchors)
    facts: list[CellFact] = []
    issues: list[dict] = []
    seen: dict[tuple, CellFact] = {}
    for row_index, label, values in group.rows:
        candidates = match_kpis(label)
        if not candidates:
            continue
        # the kind of number printed decides between a ratio and an amount of the same name
        first = values[0]
        first_value = parse_cell(first.text)
        kpi, dimension = next(
            (
                (k, d)
                for k, d in candidates
                if first_value is not None and _plausible(k, first_value, first.text)
            ),
            candidates[0],
        )
        if kpi.kind == "amount" and group.unit is None:
            issues.append(
                {
                    "page": group.page,
                    "code": "KPI_UNIT_UNKNOWN",
                    "detail": f"{label}: the reporting unit is not stated or not supported",
                }
            )
            continue
        by_anchor: dict[int, list[Word]] = {}
        for w in values:
            cx = (w.x0 + w.x1) / 2
            nearest = min(group.anchors, key=lambda a: abs(a.x - cx))
            if abs(nearest.x - cx) <= tol + 15:
                by_anchor.setdefault(id(nearest), []).append(w)
        if any(len(ws) > 1 for ws in by_anchor.values()):
            issues.append(
                {
                    "page": group.page,
                    "code": "KPI_ROW_AMBIGUOUS",
                    "detail": f"{label}: two values under one header; row skipped",
                }
            )
            continue
        by_anchor = {
            k: ws
            for k, ws in by_anchor.items()
            if all((v := parse_cell(w.text)) is not None and _plausible(kpi, v, w.text) for w in ws)
            or k not in {id(a) for a in period_anchors}
        }
        if sum(1 for a in period_anchors if by_anchor.get(id(a))) < 2:
            # one number under several period headers cannot be tied to its column with certainty
            # (multi-panel slides): the row is skipped, never guessed
            issues.append(
                {
                    "page": group.page,
                    "code": "KPI_ROW_SPARSE",
                    "detail": f"{label}: fewer than two period columns carry a value; row skipped",
                }
            )
            continue
        for a in period_anchors:
            ws = by_anchor.get(id(a))
            if not ws:
                continue
            w = ws[0]
            value = parse_cell(w.text)
            if value is None or not _plausible(kpi, value, w.text):
                continue
            fact = CellFact(
                key=kpi.key,
                raw_label=label,
                row_index=row_index,
                column_index=column_of[id(a)],
                raw_value=w.text,
                value=value,
                decimals=decimals_of(w.text),
                bbox=(w.x0, w.y0, w.x1, w.y1),
                page_number=group.page,
                dimension=dimension or None,
            )
            ident = (kpi.key, dimension, fact.column_index)
            if ident in seen:
                if seen[ident].value != value:
                    seen[ident].duplicate = fact.duplicate = True
                continue
            seen[ident] = fact
            facts.append(fact)
    if not facts:
        return None
    block = StatementBlock(
        page_number=group.page,
        statement=taxonomy.RATIOS,
        title="Key figures (results press release / presentation)",
        detection=DETECTION_TABLE,
        basis=None,
        unit=group.unit,
        columns=columns,
        facts=facts,
        issues=issues,
    )
    return block


def _plausible(kpi: Kpi, value: Decimal, raw: str) -> bool:
    if kpi.kind == "percent":
        # a percentage is printed with a decimal point or a % sign; a bare integer is a count/bps
        if "." not in raw and "%" not in raw:
            return False
        return Decimal(0) < value <= Decimal(100) and not (
            kpi.key == "net_interest_margin" and value > MAX_NIM
        )
    if "%" in raw:  # a percentage under an amount label is a growth figure, not the amount
        return False
    # amounts: GNPA/NNPA amounts are whole numbers; a decimal there is a ratio under another title
    if kpi.key in ("gross_npa_amount", "net_npa_amount"):
        return "." not in raw and value >= 100
    return value > 0


# ----------------------------------------------------------------------------- sentences

_DATE = (
    r"(?:\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4}"
    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
)
_ASOF = rf"as\s+(?:of|on|at)\s+({_DATE})"
_AMT = r"(?:₹|Rs\.?|INR)\s?([\d,]+(?:\.\d+)?)\s*(billion|crores?|bn|cr|million|mn)\b"
_PCTV = r"([\d]+(?:\.\d+)?)\s?%"
_WINDOW = r"[^.%₹\d]{0,40}"

SENTENCES: tuple[tuple[str, str, re.Pattern], ...] = (
    (
        "total_deposits",
        "amount",
        re.compile(
            rf"(?i)\btotal\s+(?:eop\s+)?deposits\s+(?:were|stood|was)\s+(?:at\s+)?{_AMT}[^.]{{0,80}}?{_ASOF}"
        ),
    ),
    (
        "gross_advances",
        "amount",
        re.compile(
            rf"(?i)\bgross\s+advances\s+(?:were|stood|was)\s+(?:at\s+)?{_AMT}[^.]{{0,80}}?{_ASOF}"
        ),
    ),
    (
        "capital_adequacy_ratio",
        "percent",
        re.compile(
            rf"(?i)\b(?:total\s+)?capital\s+adequacy\s+ratio\s*(?:\(car\))?[^.%\d]{{0,60}}?(?:was|stood)\s+(?:at\s+)?{_PCTV}[^.]{{0,80}}?{_ASOF}"
        ),
    ),
    (
        "gross_npa_ratio",
        "percent",
        re.compile(
            rf"(?i)\bgross\s+non[- ]+performing\s+assets\s+(?:were|stood|was)\s+(?:at\s+)?{_PCTV}\s+of\s+gross\s+advances\s+{_ASOF}"
        ),
    ),
    (
        "net_npa_ratio",
        "percent",
        re.compile(
            rf"(?i)\bnet\s+non[- ]+performing\s+assets\s+(?:were|stood|was)\s+(?:at\s+)?{_PCTV}\s+of\s+net\s+advances\s+{_ASOF}"
        ),
    ),
)
_UNIT_WORD = {
    "billion": "billion",
    "bn": "billion",
    "crore": "crore",
    "crores": "crore",
    "cr": "crore",
    "million": "million",
    "mn": "million",
}


def sentence_blocks(page_number: int, text: str, lines: list[Line]) -> list[StatementBlock]:
    flat = " ".join(text.split())
    by_date: dict[str, StatementBlock] = {}
    y_of = {}
    for line in lines:
        for w in line.words:
            y_of.setdefault(w.text, (w.x0, w.y0, w.x1, w.y1))
    for key, kind, pattern in SENTENCES:
        for m in pattern.finditer(flat):
            groups = m.groups()
            if kind == "amount":
                number, unit_word, date_text = groups[0], groups[1], groups[2]
                unit = _UNIT_WORD.get(unit_word.lower())
                raw = number
            else:
                number, date_text = groups[0], groups[1]
                unit, raw = None, number + "%"
            period = _period_of_date(date_text)
            value = parse_cell(number)
            if period is None or value is None or (kind == "amount" and unit is None):
                continue
            if kind == "percent" and not (Decimal(0) < value <= Decimal(100)):
                continue
            block = by_date.get(date_text)
            if block is None:
                block = StatementBlock(
                    page_number=page_number,
                    statement=taxonomy.RATIOS,
                    title="Key figures stated in the text",
                    detection=DETECTION_TEXT,
                    basis=None,
                    unit=None,
                    columns=[ColumnInfo(0, 0.0, 0.0, f"as at {date_text}", period)],
                    facts=[],
                )
                by_date[date_text] = block
            if kind == "amount":
                if block.unit not in (None, unit):
                    continue  # one unit per block; a second unit would need its own block
                block.unit = unit
            block.facts.append(
                CellFact(
                    key=key,
                    raw_label=m.group(0)[:120],
                    row_index=len(block.facts),
                    column_index=0,
                    raw_value=raw,
                    value=value,
                    decimals=decimals_of(number),
                    bbox=y_of.get(number),
                    page_number=page_number,
                )
            )
    return list(by_date.values())


_QUARTER_LABEL = re.compile(r"(?i)\bq[1-4]\s?-?\s?fy\s?-?\d{2,4}\b")
CONTEXT_SENTENCES: tuple[tuple[str, re.Pattern], ...] = (
    (
        "gross_npa_ratio",
        re.compile(r"(?i)\bGNPA\s+(?:ratio\s+)?(?:at|stood at|was)\s+(\d+(?:\.\d+)?)\s?%"),
    ),
    (
        "net_npa_ratio",
        re.compile(r"(?i)\bNNPA\s+(?:ratio\s+)?(?:at|stood at|was)\s+(\d+(?:\.\d+)?)\s?%"),
    ),
    (
        "capital_adequacy_ratio",
        re.compile(
            r"(?i)\bcapital\s+adequacy\s+ratio\s*\(CAR\)\s*(?:stood at|at|was)\s+(\d+(?:\.\d+)?)\s?%"
        ),
    ),
    (
        "net_interest_margin",
        re.compile(
            r"(?i)\bnet\s+interest\s+margin\s*\(NIM\)\s*(?:at|stood at|was)\s+(\d+(?:\.\d+)?)\s?%"
        ),
    ),
)


def context_blocks(page_number: int, text: str, lines: list[Line]) -> list[StatementBlock]:
    """Bullet statements ("GNPA at 1.16%") on a slide whose heading names ONE quarter ("Q2FY27").

    The heading must be in the first eight lines and name exactly one quarter, otherwise the
    figure's period is not certain and nothing is read.
    """
    heading = " ".join(line.text for line in lines[:8])
    quarters = {re.sub(r"\s|-", "", q).upper() for q in _QUARTER_LABEL.findall(heading)}
    if len(quarters) != 1:
        return []
    label = next(iter(quarters))
    period = parse_column_header(label)
    if period.period_end is None or period.problem is not None:
        return []
    flat = " ".join(text.split())
    block = StatementBlock(
        page_number=page_number,
        statement=taxonomy.RATIOS,
        title=f"Key figures stated on a slide headed {label}",
        detection=DETECTION_TEXT,
        basis=None,
        unit=None,
        columns=[ColumnInfo(0, 0.0, 0.0, label, period)],
        facts=[],
    )
    for key, pattern in CONTEXT_SENTENCES:
        found = {m.group(1) for m in pattern.finditer(flat)}
        if len(found) != 1:  # none, or two different values: not certain
            continue
        number = found.pop()
        value = parse_cell(number)
        if value is None or not (Decimal(0) < value <= Decimal(100)):
            continue
        block.facts.append(
            CellFact(
                key=key,
                raw_label=f"{key.replace('_', ' ')} stated on the slide headed {label}",
                row_index=len(block.facts),
                column_index=0,
                raw_value=number + "%",
                value=value,
                decimals=decimals_of(number),
                bbox=None,
                page_number=page_number,
            )
        )
    return [block] if block.facts else []


def _period_of_date(text: str) -> ColumnPeriod | None:
    period = parse_column_header("as at " + text)
    return period if period.period_end is not None and period.problem is None else None


# ----------------------------------------------------------------------------- plausibility


def finalise(blocks: list[StatementBlock]) -> list[StatementBlock]:
    """Cross-checks across everything read from the document; failing values are dropped.

    * net NPA ratio may not exceed the gross NPA ratio for the same period;
    * a gross NPA ratio that disagrees with GNPA amount / gross advances printed in the same table
      by more than 0.05 percentage points is dropped together with its neighbours' trust.
    """
    values: dict[tuple, CellFact] = {}
    blocks_of: dict[int, StatementBlock] = {}
    for block in blocks:
        for fact in block.facts:
            col = block.column(fact.column_index)
            values[
                (fact.key, fact.dimension or "", col.period.period_end, col.period.duration_months)
            ] = fact
            blocks_of[id(fact)] = block
    by_end = {(k, d, e): f for (k, d, e, _m), f in values.items()}
    drop: set[int] = set()
    for (key, dim, end, months), fact in list(values.items()):
        if key == "net_npa_ratio":
            gross = values.get(("gross_npa_ratio", dim, end, months))
            if gross is not None and fact.value > gross.value:
                drop.update({id(fact), id(gross)})
                blocks_of[id(fact)].issues.append(
                    {
                        "page": fact.page_number,
                        "code": "KPI_NPA_ORDER",
                        "detail": f"net NPA {fact.value} exceeds gross NPA {gross.value} for {end}",
                    }
                )
        if key == "gross_npa_ratio":
            amount = by_end.get(("gross_npa_amount", "", end))
            advances = by_end.get(("gross_advances", "", end))
            if amount is not None and advances is not None and advances.value:
                implied = amount.value / advances.value * 100
                if abs(implied - fact.value) > Decimal("0.05"):
                    drop.update({id(fact), id(amount), id(advances)})
                    blocks_of[id(fact)].issues.append(
                        {
                            "page": fact.page_number,
                            "code": "KPI_NPA_RECOMPUTE",
                            "detail": f"printed {fact.value}% but amounts imply {implied:.2f}%",
                        }
                    )
    out = []
    for block in blocks:
        block.facts = [f for f in block.facts if id(f) not in drop]
        if block.facts:
            out.append(block)
    return out


# ----------------------------------------------------------------------------- entry point


def extract_page_kpis(page_number: int, words: list[Word], text: str) -> list[StatementBlock]:
    """KPI blocks of one born-digital page."""
    blocks = table_blocks(page_number, words)
    lines = group_lines(words)
    blocks.extend(sentence_blocks(page_number, text, lines))
    blocks.extend(context_blocks(page_number, text, lines))
    return blocks
