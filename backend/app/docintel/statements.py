"""Find financial statement tables on a page and read their numbers with provenance.

Per page:
  1. lines are built from word positions (`layout`);
  2. statement titles split the page into blocks (an untitled block at the top is accepted only if
     its line items clearly belong to one statement type: a continuation page);
  3. per block: value columns, the header text above each column, the column's period, the unit
     and the consolidated/standalone basis are read;
  4. every row is matched to a canonical line item and every cell is kept as a candidate fact.

Nothing is filled in: a missing unit, basis or period is left as None and reported, and such
values are later refused promotion.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.docintel import detect, taxonomy
from app.docintel.layout import (
    Column,
    Line,
    Word,
    decimals_of,
    find_columns,
    group_lines,
    merge_split_numbers,
    parse_cell,
    phrases,
    split_row,
)
from app.docintel.ocr import repair_header
from app.docintel.periods import (
    ColumnPeriod,
    fiscal_alignment_problem,
    parse_column_header,
    stated_duration,
)

MIN_MATCHED_ITEMS = 3  # fewer recognised line items than this: not treated as a statement
MIN_UNTITLED_ITEMS = 5
MIN_UNTITLED_RATIOS = 3  # ratio labels are distinctive, a few are enough
MAX_WRAPPED_LINES = 4  # a label is never assembled from more than this many label-only lines
PAD = 2.0
_DANGLING = re.compile(
    r"(\b(and|of|from|before|after|for|to|in|on|the|or|with|by|at)|[,/&\-(])$", re.IGNORECASE
)
# Serial numbers at the start of a wrapped piece ("6.", "10", "(b)") must not end up mid-label.
_LEADING_ENUM = re.compile(r"^(\(?(\d{1,2}|[ivx]{1,5}|[a-h])[.)]|\d{1,2})\s+", re.IGNORECASE)
_YEARISH = re.compile(r"^\(?((19|20)\d{2}|\d{1,2})\)?$")

MIN_GUTTER = 12.0  # narrowest empty strip (in PDF points) accepted as the gap between two tables


def _strip_enumeration(label: str) -> str:
    return _LEADING_ENUM.sub("", label.strip())


def _header_row(row) -> bool:
    """Column-header line that happens to contain numbers: years or day numbers only."""
    if re.search(r"particular", row.label, re.IGNORECASE):
        return True
    return all(_YEARISH.match(cell.strip()) for cell in row.cells.values())


@dataclass
class ColumnInfo:
    index: int
    x0: float
    x1: float
    header: str
    period: ColumnPeriod
    is_note: bool = False
    problem: str | None = None  # why this column cannot be used (None = usable)
    repairs: list[str] = field(default_factory=list)  # OCR slips corrected in the header text

    @property
    def usable(self) -> bool:
        return not self.is_note and self.problem is None and self.period.period_end is not None

    def as_json(self) -> dict:
        p = self.period
        return {
            "index": self.index,
            "header": self.header,
            "period_end": p.period_end.isoformat() if p.period_end else None,
            "duration_months": p.duration_months,
            "point_in_time": p.point_in_time,
            "audited": p.audited,
            "is_note": self.is_note,
            "problem": self.problem,
            "repairs": self.repairs,
        }


@dataclass
class CellFact:
    key: str
    raw_label: str
    row_index: int
    column_index: int
    raw_value: str
    value: Decimal | None
    decimals: int
    bbox: tuple[float, float, float, float] | None
    duplicate: bool = False  # the same item appeared twice in the block with different values
    page_number: int = 0  # page the value was printed on (statements can span pages)
    dimension: str | None = None  # segment name / note title the value belongs to


@dataclass
class StatementBlock:
    page_number: int
    statement: str
    title: str | None
    detection: str
    basis: str | None
    unit: str | None
    columns: list[ColumnInfo]
    facts: list[CellFact] = field(default_factory=list)
    unmatched_labels: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    continued_pages: list[int] = field(default_factory=list)  # later pages merged into this block
    dimension: str | None = None  # note tables: the schedule / note title
    inherited: list[str] = field(default_factory=list)  # context copied from the previous page

    @property
    def matched_keys(self) -> set[str]:
        return {f.key for f in self.facts}

    def column(self, index: int) -> ColumnInfo:
        return next(c for c in self.columns if c.index == index)

    def value(self, key: str, column: int, dimension: str | None = None) -> CellFact | None:
        return next(
            (
                f
                for f in self.facts
                if f.key == key
                and f.column_index == column
                and not f.duplicate
                and (dimension is None or f.dimension == dimension)
            ),
            None,
        )

    def dimensional(self, key: str, column: int) -> list[CellFact]:
        """All values of `key` in a column that belong to a dimension (segments, ...)."""
        return [
            f
            for f in self.facts
            if f.key == key
            and f.column_index == column
            and f.dimension is not None
            and not f.duplicate
        ]


# ------------------------------------------------------------------------------- headers


def _split_by_columns(part: Line, columns: list[Column]) -> list[Line]:
    """Split a phrase that merged two columns' headers ("31.03.2026 31.03.2025").

    Only done when every piece contains a digit (dates, years) or all pieces are the same word
    ("(Unaudited) (Unaudited)", "As at As at"): a genuine spanning header such as "Three months
    ended" is never cut into pieces.
    """
    pieces: dict[int, list[Word]] = {}
    unplaced: list[Word] = []
    for word in part.words:
        hits = [c for c in columns if word.x1 >= c.x0 - PAD and word.x0 <= c.x1 + PAD]
        if len(hits) > 1:
            return [part]
        if not hits:
            unplaced.append(word)  # a leading word left of its column ("As on 30.09.2026")
            continue
        for lead in unplaced:  # attach forward to the next word that has a column
            pieces.setdefault(hits[0].index, []).append(lead)
        unplaced = []
        pieces.setdefault(hits[0].index, []).append(word)
    if unplaced:  # trailing words with no column: not a repeated per-column header
        return [part]
    if len(pieces) >= 2:
        leads = []
        for words in pieces.values():
            first = next((i for i, w in enumerate(words) if any(c.isdigit() for c in w.text)), 0)
            leads.append(" ".join(w.text.lower() for w in words[:first]))
        # words outside every column are allowed only when each piece repeats the same lead
        # ("As on" / "As at"); otherwise it is a spanning header ("Quarter ended"), kept whole
        if (
            any(
                w
                for w in part.words
                if not [c for c in columns if w.x1 >= c.x0 - PAD and w.x0 <= c.x1 + PAD]
            )
            and len(set(leads)) != 1
        ):
            return [part]
    if len(pieces) < 2:
        return [part]
    lines = [Line(words) for _, words in sorted(pieces.items())]
    if all(any(ch.isdigit() for ch in ln.text) for ln in lines):
        return lines
    if len({ln.text.lower() for ln in lines}) == 1:
        return lines
    return [part]


def _is_note_text(text: str) -> bool:
    return bool(detect.NOTE_HEADER.fullmatch(" ".join(text.lower().strip(" .:").split())))


def _column_headers(
    header_lines: list[Line], columns: list[Column]
) -> tuple[dict[int, list[str]], set[int]]:
    """(header text per column, indexes of note-reference columns).

    Note columns ("Note", "Schedule") are identified first and excluded, so they never take a
    period header. Then, per header line: if there are at least as many phrases as period
    columns, each phrase goes to the column it overlaps; otherwise the phrases are spanning
    headers ("Three months ended" over several columns) and each column takes the phrase whose
    centre is nearest, unless two phrases are almost equally near (then: ambiguous).
    """
    texts: dict[int, list[str]] = {c.index: [] for c in columns}
    notes: set[int] = set()
    if not columns:
        return texts, notes
    area_left = columns[0].x0 - 40  # headers can start a little left of the first value
    for line in header_lines:
        for part in phrases(line):
            if _is_note_text(part.text):
                for col in columns:
                    if _overlap(part, col) > 0:
                        notes.add(col.index)
    value_cols = [c for c in columns if c.index not in notes]
    if not value_cols:
        return texts, notes
    pitch = (
        (value_cols[-1].xc - value_cols[0].xc) / (len(value_cols) - 1)
        if len(value_cols) > 1
        else 50.0
    )
    for line in header_lines:
        parts: list[Line] = []
        for part in phrases(line):
            if part.x1 <= area_left or _is_note_text(part.text):
                continue
            parts.extend(_split_by_columns(part, value_cols))
        if not parts:
            continue
        if len(parts) >= len(value_cols):
            for part in parts:
                best = max(value_cols, key=lambda c: _overlap(part, c))
                if _overlap(part, best) > 0:
                    texts[best.index].append(part.text)
                    continue
                nearest = min(value_cols, key=lambda c: abs(_xc(part) - c.xc))
                if abs(_xc(part) - nearest.xc) < pitch / 2:
                    texts[nearest.index].append(part.text)
        else:
            for col in value_cols:
                ranked = sorted(parts, key=lambda p: abs(_xc(p) - col.xc))
                d1 = abs(_xc(ranked[0]) - col.xc)
                if len(ranked) > 1 and abs(_xc(ranked[1]) - col.xc) - d1 < 0.25 * pitch:
                    texts[col.index].append("[ambiguous header]")
                    continue
                if d1 <= max(pitch * 1.6, (ranked[0].x1 - ranked[0].x0)):
                    texts[col.index].append(ranked[0].text)
    return texts, notes


def _xc(line: Line) -> float:
    return (line.x0 + line.x1) / 2


def _overlap(part: Line, col: Column) -> float:
    return max(0.0, min(part.x1, col.x1 + PAD) - max(part.x0, col.x0 - PAD))


def _read_columns(
    header_lines: list[Line],
    columns: list[Column],
    statement: str,
    ocr: bool = False,
    heading: str = "",
) -> list[ColumnInfo]:
    texts, notes = _column_headers(header_lines, columns)
    # A heading that states the length once ("for the year ended ...") can serve columns that do
    # not state it themselves, but ONLY when no column states its own. A statement mixing
    # "Three months ended" and "Year ended" columns has no single heading length to borrow.
    any_column_states_duration = any(
        parse_column_header(" ".join(texts[c.index])).duration_months is not None
        for c in columns
        if c.index not in notes
    )
    infos = []
    for col in columns:
        header = " ".join(texts[col.index]).strip()
        if col.index in notes:
            infos.append(
                ColumnInfo(
                    col.index,
                    col.x0,
                    col.x1,
                    header or "Note",
                    ColumnPeriod(None, None, False, None, "note reference column"),
                    is_note=True,
                )
            )
            continue
        if "[ambiguous header]" in header:
            infos.append(
                ColumnInfo(
                    col.index,
                    col.x0,
                    col.x1,
                    header,
                    ColumnPeriod(None, None, False, None, "ambiguous header"),
                    problem="header could not be assigned to this column unambiguously",
                )
            )
            continue
        repairs: list[str] = []
        if ocr:
            header, repairs = repair_header(header)
        period = parse_column_header(header)
        info = ColumnInfo(col.index, col.x0, col.x1, header, period, repairs=repairs)
        if statement == taxonomy.BALANCE_SHEET:
            if period.period_end is not None and period.problem in (None, "duration not stated"):
                info.period = ColumnPeriod(period.period_end, None, True, period.audited)
            else:
                info.problem = period.problem or "no period"
        elif statement in (taxonomy.SEGMENT, taxonomy.RATIOS, taxonomy.NOTE):
            # disclosures: either a period ("Three months ended ...") or a date ("At 31.03.2026")
            if period.period_end is None or (
                period.problem and period.problem != "duration not stated"
            ):
                info.problem = period.problem or "no period"
            elif not period.point_in_time and period.duration_months is None:
                info.problem = "duration not stated"
        else:
            if period.point_in_time:
                info.problem = "point-in-time column in a flow statement"
            elif period.period_end is not None and period.problem == "duration not stated":
                # "for the year ended March 31, 2026" printed once above the columns, not in them
                shared = None if any_column_states_duration else stated_duration(heading)
                if shared is None:
                    info.problem = "duration not stated"
                else:
                    info.period = ColumnPeriod(period.period_end, shared, False, period.audited)
            elif not period.ok:
                info.problem = period.problem or "no period"
            elif period.duration_months is None:
                info.problem = "duration not stated"
        if info.problem is None and info.period.duration_months is not None:
            info.problem = fiscal_alignment_problem(
                info.period.period_end, info.period.duration_months
            )
        infos.append(info)
    # Two usable columns claiming the same period: we cannot tell them apart, so neither is used.
    seen: dict[tuple[date | None, int | None], ColumnInfo] = {}
    for info in infos:
        if not info.usable:
            continue
        key = (info.period.period_end, info.period.duration_months)
        if key in seen:
            for dup in (info, seen[key]):
                dup.problem = "two columns have the same period"
        else:
            seen[key] = info
    return infos


# ------------------------------------------------------------------------------- blocks


def _unit_for(
    header_text: str, above: list[Line], page_lines: list[Line], ocr: bool = False
) -> tuple[str | None, str | None]:
    """(unit, problem). Nearest statement of unit wins: header, then lines above, then page."""
    units = detect.find_units(header_text, ocr)
    if len(units) == 1:
        return units.pop(), None
    if len(units) > 1:
        return None, f"conflicting units in header: {sorted(units)}"
    for line in reversed(above):
        units = detect.find_units(line.text, ocr)
        if len(units) == 1:
            return units.pop(), None
    page_units = detect.find_units(" ".join(ln.text for ln in page_lines), ocr)
    if len(page_units) == 1:
        return page_units.pop(), None
    if page_units:
        return None, f"several units on the page: {sorted(page_units)}"
    return None, "unit not stated"


def _basis_for(
    title: str, header_text: str, above: list[Line], page_lines: list[Line]
) -> str | None:
    for text in (title, header_text, " ".join(ln.text for ln in above[-6:])):
        found = detect.find_basis(text)
        if len(found) == 1:
            return found.pop()
        if len(found) > 1:
            return None
    found = detect.find_basis(" ".join(ln.text for ln in page_lines))
    return found.pop() if len(found) == 1 else None


# Statements whose block ends where numbered notes begin. A balance sheet or cash-flow statement is
# often followed on the same page by a note with its own table (Axis: the RBI resolution-framework
# table), whose numbers would otherwise be taken for an extra value column.
TRIM_FOOTNOTES = {taxonomy.NOTE, taxonomy.BALANCE_SHEET, taxonomy.CASH_FLOW}

_FOOTNOTE_START = re.compile(r"^\s*\d{1,2}\.\s+[A-Za-z]")


def _trim_footnotes(body: list[Line]) -> list[Line]:
    """End a schedule's lines where its footnotes begin ("1. Assets amounting to ... million").

    Footnote sentences contain amounts and years whose positions would otherwise be mistaken for
    extra value columns. The cut happens only after at least three numeric lines, so a table whose
    first row starts like a footnote is not truncated.
    """
    numeric = 0
    for i, line in enumerate(body):
        if any(parse_cell(w.text) is not None for w in line.words):
            numeric += 1
        if numeric >= 3 and len(line.words) >= 6 and _FOOTNOTE_START.match(line.text):
            return body[:i]
    return body


def _read_block(
    page_number: int,
    statement: str,
    title_line: Line | None,
    body: list[Line],
    above: list[Line],
    page_lines: list[Line],
    detection: str,
    ocr: bool = False,
) -> StatementBlock | None:
    if statement in TRIM_FOOTNOTES:
        body = _trim_footnotes(body)
    columns = find_columns(body)
    if not columns:
        return None
    rows = [split_row(line, columns, i, PAD) for i, line in enumerate(body)]
    # Header lines: everything above the first labelled row that carries values.
    first_data = next(
        (
            i
            for i, r in enumerate(rows)
            if r and r.has_values and re.search(r"[A-Za-z]{3,}", r.label) and not _header_row(r)
        ),
        None,
    )
    if first_data is None:
        return None
    header_lines = body[:first_data]
    header_text = " ".join(ln.text for ln in header_lines)
    title = title_line.text if title_line else None
    # Segment tables repeat "revenue" and "profit" per business line; they are not a statement.
    primary = statement in (taxonomy.INCOME, taxonomy.BALANCE_SHEET, taxonomy.CASH_FLOW)
    if primary and re.search(r"\bsegment", f"{title or ''} {header_text}", re.IGNORECASE):
        return None
    unit, unit_problem = _unit_for(
        " ".join(filter(None, [title, header_text])), above, page_lines, ocr
    )
    block = StatementBlock(
        page_number=page_number,
        statement=statement,
        title=title,
        detection=detection,
        basis=_basis_for(title or "", header_text, above, page_lines),
        unit=unit,
        columns=_read_columns(
            header_lines, columns, statement, ocr, f"{title or ''} {header_text}"
        ),
        dimension=(title or "")[:160] if statement == taxonomy.NOTE else None,
    )
    if not primary and above and not any(c.usable for c in block.columns):
        # a table that continues from the page before keeps its column headings ABOVE its title
        block.columns = _read_columns(
            [*above[-8:], *header_lines], columns, statement, ocr, f"{title or ''} {header_text}"
        )
        block.inherited.append("column headings read above the table title")
    if unit_problem:
        block.issues.append({"page": page_number, "code": "UNIT_UNKNOWN", "detail": unit_problem})
    if block.basis is None:
        block.issues.append(
            {
                "page": page_number,
                "code": "BASIS_UNKNOWN",
                "detail": "consolidated/standalone is not stated unambiguously near this statement",
            }
        )

    ctx = taxonomy.Context()
    # Headings above the first data row ("ASSETS", "Non-current assets") still set the context.
    for row in rows[:first_data]:
        if row is not None and row.label and not row.has_values:
            taxonomy.observe_heading(row.label, statement, ctx)
    # Label-only lines waiting to be joined with the next valued row (a label can wrap over
    # several lines, possibly below an unrelated heading).
    pieces: list[str] = []
    # label-only lines right above the first data row are the START of its wrapped label
    for earlier in reversed(rows[:first_data]):
        if earlier is None or not earlier.label or earlier.has_values:
            break
        pieces.insert(0, _strip_enumeration(" ".join(earlier.label.split())))
    pieces = pieces[-MAX_WRAPPED_LINES:]
    last_unmatched = None  # valued row whose label may continue on the next line
    for i in range(first_data, len(body)):
        row = rows[i]
        if row is None:  # prose or a header crossing the value columns
            pieces, last_unmatched = [], None
            continue
        label = " ".join(row.label.split())
        if not row.has_values:
            if not label:
                continue
            if last_unmatched is not None and _DANGLING.search(last_unmatched[1]):
                joined = f"{last_unmatched[1]} {_strip_enumeration(label)}"
                key = taxonomy.match_label(joined, statement, ctx)
                if key:
                    _add_facts(block, key, joined, last_unmatched[0], ctx.last_dimension)
                    block.unmatched_labels.remove(last_unmatched[1])
                    last_unmatched = None
                    continue
            taxonomy.observe_heading(label, statement, ctx)
            pieces = [*pieces, _strip_enumeration(label)][-MAX_WRAPPED_LINES:]
            last_unmatched = None
            continue
        # Joined forms, LONGEST first: "OPERATING PROFIT BEFORE / PROVISIONS AND / CONTINGENCIES"
        # must not be read as its tail "provisions and contingencies".
        tail = _strip_enumeration(label)
        joins = [" ".join([*pieces[-k:], tail]).strip() for k in range(len(pieces), 0, -1)]
        candidates: list[str] = []
        if pieces and (_is_continuation(label) or _DANGLING.search(pieces[-1])):
            candidates += joins
        if label:
            candidates.append(label)
        candidates += [j for j in joins if j not in candidates]  # wrap without a clear marker
        key, used = None, label or (joins[0] if joins else "")
        for candidate in candidates:
            key = taxonomy.match_label(candidate, statement, ctx)
            if key:
                used = candidate
                break
        pieces = []
        if key:
            _add_facts(block, key, used, row, ctx.last_dimension)
            last_unmatched = None
        else:
            if used:
                block.unmatched_labels.append(used)
            last_unmatched = (row, used) if used else None

    if len(block.matched_keys) < (MIN_MATCHED_ITEMS if title_line else _min_untitled(statement)):
        return None
    if not primary and not any(c.usable for c in block.columns):
        return None  # a disclosure table without a readable period column is not staged
    _drop_serial_number_columns(block)
    _mark_duplicates(block)
    return block


_ENUMERATOR = re.compile(r"^\(?([ivx]{1,4}|[a-z])[.)]", re.IGNORECASE)
_PAREN_ENUMERATOR = re.compile(r"^\(\s*(\d{1,2}|[ivx]+|[a-z])\s*\)", re.IGNORECASE)


_SERIAL_CELL = re.compile(r"^\(?\d{1,2}\)?$")
MAX_SERIAL = 40


def _drop_serial_number_columns(block: StatementBlock) -> None:
    """A column of item numbers ("1", "2", ... "9") is a serial-number column, not a period.

    Deliberately narrow: every cell is a whole number up to 40 with no decimal point, there are at
    least three of them, and every other column that has values is mostly written with decimals (as
    amounts in these tables are). The column is marked as a note-style column and its cells are
    removed, so its (often borrowed) heading can no longer clash with the real period column.
    """
    by_column: dict[int, list[CellFact]] = {}
    for fact in block.facts:
        by_column.setdefault(fact.column_index, []).append(fact)
    for index, cells in by_column.items():
        if len(cells) < 3 or not all(
            _SERIAL_CELL.match(c.raw_value.strip())
            and c.value is not None
            and 0 <= abs(c.value) <= MAX_SERIAL
            for c in cells
        ):
            continue
        others = [c for i, cs in by_column.items() if i != index for c in cs]
        if len(others) < 3 * len(cells) // 2:
            continue
        decimal = sum(1 for c in others if "." in c.raw_value)
        if decimal / len(others) < 0.7:
            continue
        column = block.column(index)
        column.is_note = True
        column.problem = "serial-number column (item numbers, not a period)"
        block.facts = [f for f in block.facts if f.column_index != index]


def _is_continuation(label: str) -> bool:
    """Does this row's label continue a wrapped label from the lines above?

    A value-only row does. Otherwise only a label that starts lowercase or with a bracketed
    remark ("(excluding subordinated debt)") does; enumerators ("iv)", "(a)", "j)", "a Retail
    Banking") start a NEW row, so a heading above is never glued to the first row below it.
    """
    if not label:
        return True
    if _ENUMERATOR.match(label) or _PAREN_ENUMERATOR.match(label):
        return False
    if re.match(r"^[a-z]\s+[A-Z]", label):
        return False
    if label[0] == "(":
        return True
    return label[0].islower()


def _min_untitled(statement: str) -> int:
    return MIN_UNTITLED_RATIOS if statement == taxonomy.RATIOS else MIN_UNTITLED_ITEMS


def _add_facts(
    block: StatementBlock, key: str, label: str, row, dimension: str | None = None
) -> None:
    for col_index, raw in row.cells.items():
        info = block.column(col_index)
        if info.is_note:
            continue
        block.facts.append(
            CellFact(
                key=key,
                raw_label=label,
                row_index=row.line_index,
                column_index=col_index,
                raw_value=raw,
                value=parse_cell(raw),
                decimals=decimals_of(raw),
                bbox=row.bboxes.get(col_index),
                page_number=block.page_number,
                dimension=dimension or block.dimension,
            )
        )


def _period_key(column: ColumnInfo) -> tuple:
    return (column.period.period_end, column.period.duration_months, column.period.point_in_time)


def merge_continuations(blocks: list[StatementBlock]) -> list[StatementBlock]:
    """Join a statement that continues on the next page into one block, so its checks see all of it.

    Merged only when the next page's block has the same statement type, basis and unit, and its
    usable columns carry exactly the same periods. Columns are matched by period, never by
    position. Anything that does not match stays a separate block.
    """
    out: list[StatementBlock] = []
    for block in sorted(blocks, key=lambda b: b.page_number):
        prev = out[-1] if out else None
        if prev is not None and _continues(prev, block):
            by_period = {_period_key(c): c.index for c in prev.columns if c.usable}
            mapping = {c.index: by_period[_period_key(c)] for c in block.columns if c.usable}
            for fact in block.facts:
                if fact.column_index in mapping:
                    fact.column_index = mapping[fact.column_index]
                    prev.facts.append(fact)
            prev.unmatched_labels += block.unmatched_labels
            prev.issues += block.issues
            prev.detection = f"{prev.detection[:150]} (continues on page {block.page_number})"
            prev.continued_pages.append(block.page_number)
            _mark_duplicates(prev)
            continue
        out.append(block)
    return out


def inherit_context(blocks: list[StatementBlock]) -> list[StatementBlock]:
    """Give a block that lacks its basis (or unit) the one printed on the PAGE BEFORE.

    Continuation pages repeat the column headings but not the "Standalone / Consolidated" title or
    the unit line. Copied only when the previous page's last block has exactly the same usable
    periods, so a table about something else is never given its neighbour's basis.
    """
    ordered = sorted(blocks, key=lambda b: (b.page_number, 0))
    for index, block in enumerate(ordered):
        if block.basis is not None and block.unit is not None:
            continue
        previous = next(
            (b for b in reversed(ordered[:index]) if b.page_number == block.page_number - 1),
            None,
        )
        if previous is None:
            continue
        mine = {_period_key(c) for c in block.columns if c.usable}
        if not mine or mine != {_period_key(c) for c in previous.columns if c.usable}:
            continue
        if block.basis is None and previous.basis is not None:
            block.basis = previous.basis
            block.inherited.append(f"basis from page {previous.page_number}")
            block.issues = [i for i in block.issues if i["code"] != "BASIS_UNKNOWN"]
        if block.unit is None and previous.unit is not None:
            block.unit = previous.unit
            block.inherited.append(f"unit from page {previous.page_number}")
            block.issues = [i for i in block.issues if i["code"] != "UNIT_UNKNOWN"]
    return ordered


_CONTD = re.compile(r"\s*\(?\s*contd\.?\s*\)?\s*$", re.IGNORECASE)


def _plain_title(title: str | None) -> str:
    return _CONTD.sub("", " ".join((title or "").lower().split()))


def _continues(prev: StatementBlock, block: StatementBlock) -> bool:
    last_page = prev.continued_pages[-1] if prev.continued_pages else prev.page_number
    if block.page_number != last_page + 1:
        return False
    if (prev.statement, prev.basis, prev.unit) != (block.statement, block.basis, block.unit):
        return False
    # A schedule is a continuation only if its title repeats ('SCHEDULE 4 - BORROWINGS (Contd.)');
    # a different title is a different table even when the columns are identical.
    if block.statement == taxonomy.NOTE and _plain_title(block.dimension) != _plain_title(
        prev.dimension
    ):
        return False
    mine = {_period_key(c) for c in prev.columns if c.usable}
    theirs = {_period_key(c) for c in block.columns if c.usable}
    return bool(mine) and mine == theirs


def _mark_duplicates(block: StatementBlock) -> None:
    """An item read twice in one column with different values is ambiguous: both are flagged."""
    by_cell: dict[tuple[str, int, str | None], list[CellFact]] = {}
    for fact in block.facts:
        by_cell.setdefault((fact.key, fact.column_index, fact.dimension), []).append(fact)
    for (key, _, _), facts in by_cell.items():
        if len(facts) > 1:
            values = {f.value for f in facts}
            if len(values) > 1:
                for fact in facts:
                    fact.duplicate = True
                block.issues.append(
                    {
                        "page": block.page_number,
                        "code": "DUPLICATE_ITEM",
                        "detail": f"'{key}' appears {len(facts)} times with different values",
                    }
                )
            else:
                for fact in facts[1:]:
                    block.facts.remove(fact)


def _widest_empty_channel(words: list[Word], lo: float, hi: float) -> float | None:
    """Middle of the widest vertical strip in [lo, hi] that no word touches (None if too narrow)."""
    spans = sorted((w.x0, w.x1) for w in words if w.x1 > lo and w.x0 < hi)
    best: tuple[float, float] | None = None
    reach = lo
    for x0, x1 in spans:
        if x0 - reach > (best[1] - best[0] if best else MIN_GUTTER - 0.001):
            best = (reach, x0)
        reach = max(reach, x1)
    if hi - reach > (best[1] - best[0] if best else MIN_GUTTER - 0.001):
        best = (reach, hi)
    return (best[0] + best[1]) / 2 if best else None


def two_up_gutter(words: list[Word]) -> float | None:
    """The x position separating two statements printed side by side on one page, or None.

    Some annual reports print a balance sheet and a profit-and-loss account next to each other
    (a landscape spread). Read as one table their columns collide. A page is treated as two-up
    only when two statement TITLES share a line and almost no word crosses the gap between them,
    so an ordinary page is never split.
    """
    for line in group_lines(words):
        titled = [
            p
            for p in phrases(line)
            if len(p.words) >= 2 and (detect.classify_title(p.text) or "") != ""
        ]
        if len(titled) < 2:
            continue
        gutter = _widest_empty_channel(words, titled[0].x0 + 1, titled[1].x0)
        if gutter is not None:
            return gutter
    return None


def extract_page_statements(
    page_number: int, words: list[Word], *, ocr: bool = False, _split: bool = True
) -> list[StatementBlock]:
    """All statement tables recognised on one page."""
    gutter = two_up_gutter(words) if _split else None
    if gutter is not None:
        halves = ([w for w in words if w.xc < gutter], [w for w in words if w.xc >= gutter])
        return [
            block
            for half in halves
            for block in extract_page_statements(page_number, half, ocr=ocr, _split=False)
        ]
    lines = [merge_split_numbers(line) for line in group_lines(words)]
    titles = [
        (i, detect.classify_title(line.text) or detect.classify_disclosure_title(line.text))
        for i, line in enumerate(lines)
    ]
    title_idx: list[tuple[int, str]] = []
    for i, kind in titles:
        if not kind:
            continue
        # Section headings of a segment / ratio table repeat its title ("1. Segment Revenue",
        # "2. Segmental Results"): the first title starts the table, the rest are inside it.
        if title_idx and kind in (taxonomy.SEGMENT, taxonomy.RATIOS) and title_idx[-1][1] == kind:
            continue
        title_idx.append((i, kind))
    blocks: list[StatementBlock] = []

    # Untitled top part of the page: a statement continued from the previous page.
    top_end = title_idx[0][0] if title_idx else len(lines)
    top = lines[:top_end]
    if top:
        row_labels = [ln.text for ln in top]
        scores = taxonomy.score_statement_types(_labels_only(top))
        best = max(scores, key=lambda k: scores[k])
        ranked = sorted(scores.values(), reverse=True)
        floor = MIN_UNTITLED_RATIOS if best == taxonomy.RATIOS else MIN_UNTITLED_ITEMS
        if ranked[0] >= floor and ranked[1] * 2 < ranked[0] and row_labels:
            block = _read_block(
                page_number,
                best,
                None,
                top,
                [],
                lines,
                "line items only (no title on this page)",
                ocr,
            )
            if block:
                blocks.append(block)

    for n, (start, statement) in enumerate(title_idx):
        end = title_idx[n + 1][0] if n + 1 < len(title_idx) else len(lines)
        # consecutive title lines ("ICICI Bank Limited" / "Consolidated Balance Sheet") merge
        body = lines[start + 1 : end]
        if not body:
            continue
        block = _read_block(
            page_number,
            statement,
            lines[start],
            body,
            lines[:start],
            lines,
            f"title: {lines[start].text[:120]}",
            ocr,
        )
        if block:
            blocks.append(block)
    return blocks


def _labels_only(lines: list[Line]) -> list[str]:
    """Leading text of each line (before the first number) for statement-type scoring."""
    out = []
    for line in lines:
        words = []
        for word in line.words:
            if parse_cell(word.text) is not None:
                break
            words.append(word.text)
        if words:
            out.append(" ".join(words))
    return out
