"""Page layout analysis: words -> lines -> table rows with numeric columns.

Financial statements in Indian annual reports and results filings are usually laid out with
whitespace, not ruled grids, so generic grid-based table detectors miss or split them. This module
works from word positions instead:

1. words whose vertical centres are close form a *line*;
2. numeric tokens are collected and their horizontal extents are merged into *columns* (tokens in
   one column overlap horizontally whether they are right- or centre-aligned);
3. a line is a *table row* when every label word sits left of the first value column; its cells
   are the tokens that overlap each column.

Everything here is deterministic and works on plain coordinates, so it is unit-tested without
PDF files. Nothing guesses: a token that does not overlap a column is never forced into one.
"""

import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from app.normalization import parse_amount


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.1)


@dataclass
class Line:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)


# Cell values that mean "not applicable / nil" in Indian filings. They are NOT zero: the value is
# recorded as absent.
NA_MARKERS = {
    "-",
    "--",
    "–",
    "—",
    "‒",
    "−",
    "nil",
    "na",
    "n.a.",
    "n/a",
    "*",
    "..",
    "...",
    "…",  # some banks print '..' for nil
}
_MINUS = str.maketrans({"−": "-", "–": "-", "—": "-", "‒": "-"})
_DECIMALS = re.compile(r"\.(\d+)\)?%?$")


# Digit grouping as printed: Indian (1,23,45,678.90) or international (12,345,678.90), or none.
_GROUPED = re.compile(r"^-?\(?-?(\d{1,3}(,\d{2})*,\d{3}|\d{1,3}(,\d{3})+|\d+)(\.\d+)?\)?$")


def parse_cell(text: str) -> Decimal | None:
    """Number in a statement cell: '1,23,456.78', '(1,234.5)', '-12.3', '−12.3', '12.3%'.

    Returns None for NA markers and anything that is not unambiguously a number. Commas must be
    real digit-group separators, so a date fragment like '30,' is not read as 30.
    """
    raw = text.strip().translate(_MINUS)
    if raw.endswith("%"):
        raw = raw[:-1]
    # '( 1,234 )' style spacing inside parentheses
    raw = re.sub(r"^\(\s+", "(", re.sub(r"\s+\)$", ")", raw))
    if not raw or raw.lower() in NA_MARKERS:
        return None
    if not _GROUPED.match(raw):
        return None
    return parse_amount(raw)


def decimals_of(text: str) -> int:
    match = _DECIMALS.search(text.strip())
    return len(match.group(1)) if match else 0


def is_value_token(text: str) -> bool:
    """A token that can stand in a value column: a number or an NA marker."""
    raw = text.strip()
    return raw.lower() in NA_MARKERS or parse_cell(raw) is not None


def _is_number(text: str) -> bool:
    return parse_cell(text) is not None


def group_lines(words: list[Word], tolerance: float = 0.5) -> list[Line]:
    """Group words into lines by vertical centre; each line's words are sorted left to right."""
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (w.yc, w.x0)):
        if lines:
            current = lines[-1]
            centre = statistics.fmean(w.yc for w in current)
            height = min(min(w.height for w in current), word.height)
            if abs(word.yc - centre) <= tolerance * height:
                current.append(word)
                continue
        lines.append([word])
    return [Line(sorted(group, key=lambda w: w.x0)) for group in lines]


def merge_split_numbers(line: Line, gap: float = 3.0) -> Line:
    """Join tokens a PDF split inside one number: '(' '1,234' ')' or '1,234' ')' -> '(1,234)'."""
    out: list[Word] = []
    for word in line.words:
        if out:
            prev = out[-1]
            joined = prev.text + word.text
            close = word.x0 - prev.x1 <= gap
            if (
                close
                and (
                    prev.text in {"(", "-", "−"}
                    or word.text in {")", "%"}
                    or (
                        prev.text.startswith("(")
                        and not prev.text.endswith(")")
                        and word.text.endswith(")")
                    )
                )
                and _is_number(joined)
            ):
                out[-1] = Word(
                    prev.x0, min(prev.y0, word.y0), word.x1, max(prev.y1, word.y1), joined
                )
                continue
        out.append(word)
    return Line(out)


@dataclass(frozen=True)
class Column:
    index: int
    x0: float
    x1: float
    support: int  # number of lines with a number in this column

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


def find_columns(lines: list[Line], min_support: int = 3, min_share: float = 0.25) -> list[Column]:
    """Value columns: horizontal clusters of numeric tokens that recur on enough lines.

    A cluster must be right of where row labels usually start, so serial-number columns
    ("1", "2", ...) at the left edge are not mistaken for values.
    """
    label_starts = [
        next((w.x0 for w in ln.words if re.search(r"[A-Za-z]{3,}", w.text)), None) for ln in lines
    ]
    label_starts = [x for x in label_starts if x is not None]
    label_left = statistics.median(label_starts) if label_starts else 0.0

    spans: list[tuple[float, float, int]] = []
    numeric_lines = 0
    for i, line in enumerate(lines):
        nums = [w for w in line.words if _is_number(w.text) and w.x0 > label_left]
        if nums:
            numeric_lines += 1
        spans.extend((w.x0, w.x1, i) for w in nums)
    if not spans:
        return []

    spans.sort()
    clusters: list[list[tuple[float, float, int]]] = [[spans[0]]]
    right = spans[0][1]
    for span in spans[1:]:
        if span[0] < right - 0.5:  # genuine horizontal overlap with the current cluster
            clusters[-1].append(span)
            right = max(right, span[1])
        else:
            clusters.append([span])
            right = span[1]

    needed = max(min_support, int(min_share * numeric_lines))
    columns = []
    for cluster in clusters:
        support = len({s[2] for s in cluster})
        if support >= needed:
            columns.append((min(s[0] for s in cluster), max(s[1] for s in cluster), support))
    return [Column(i, x0, x1, n) for i, (x0, x1, n) in enumerate(columns)]


@dataclass
class Row:
    line_index: int
    label: str
    cells: dict[int, str] = field(default_factory=dict)  # column index -> raw cell text
    bboxes: dict[int, tuple[float, float, float, float]] = field(default_factory=dict)
    label_bbox: tuple[float, float, float, float] | None = None

    @property
    def has_values(self) -> bool:
        return bool(self.cells)


_NOTE_REF = re.compile(r"^\d{1,3}[A-Za-z]{1,2}$")


def split_row(line: Line, columns: list[Column], index: int, pad: float = 2.0) -> Row | None:
    """Turn a line into a table row, or None if the line is prose crossing the value columns."""
    if not columns:
        return None
    first = columns[0].x0 - pad
    label_words: list[Word] = []
    row = Row(index, "")
    for word in line.words:
        col = next((c for c in columns if word.x1 >= c.x0 - pad and word.x0 <= c.x1 + pad), None)
        # Alphanumeric schedule/note references ("1A", "18B") sit in a note column; keep them as
        # cells so the row is not mistaken for prose. They never parse as values.
        if col is not None and (is_value_token(word.text) or _NOTE_REF.match(word.text)):
            if col.index in row.cells:  # two tokens in one cell: keep text, it will not parse
                row.cells[col.index] += " " + word.text
                x0, y0, x1, y1 = row.bboxes[col.index]
                row.bboxes[col.index] = (x0, min(y0, word.y0), word.x1, max(y1, word.y1))
            else:
                row.cells[col.index] = word.text
                row.bboxes[col.index] = (word.x0, word.y0, word.x1, word.y1)
        elif word.x1 <= first:
            label_words.append(word)
        else:
            return None  # text in the value area: prose or a header line, not a data row
    row.label = " ".join(w.text for w in label_words)
    if label_words:
        row.label_bbox = (
            min(w.x0 for w in label_words),
            min(w.y0 for w in label_words),
            max(w.x1 for w in label_words),
            max(w.y1 for w in label_words),
        )
    return row


def phrases(line: Line, gap_ratio: float = 0.9) -> list[Line]:
    """Split a line into phrases separated by gaps wider than ~one character height."""
    if not line.words:
        return []
    height = statistics.median(w.height for w in line.words)
    out = [[line.words[0]]]
    for prev, word in zip(line.words, line.words[1:], strict=False):
        if word.x0 - prev.x1 > gap_ratio * height:
            out.append([word])
        else:
            out[-1].append(word)
    return [Line(p) for p in out]
