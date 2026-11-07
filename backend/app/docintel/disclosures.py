"""Disclosures that are not period-column tables: narrative sections, the group-entity roster of
the related-party note, and the shareholding pattern.

Everything here is deterministic and quotes the document:

* sections are located by their HEADING and stored as an excerpt with the page range (the full
  text of every page is already stored per page, so any claim can cite a page);
* flags on the auditor's report are explicit phrases the report prints (never a judgement);
* the group roster copies the entity names and relationship words exactly as printed;
* the shareholding pattern is accepted only if its categories add up to the printed total.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

from app.docintel import detect, taxonomy
from app.docintel.layout import (
    Word,
    decimals_of,
    find_columns,
    group_lines,
    merge_split_numbers,
    parse_cell,
    split_row,
)
from app.docintel.pdf import PageData
from app.docintel.periods import ColumnPeriod, _dates
from app.docintel.statements import CellFact, ColumnInfo, StatementBlock

HEADING_LINES = 12
MAX_SECTION_PAGES = 60
EXCERPT_CHARS = 6000

# ----------------------------------------------------------------------------- sections

SECTION_HEADINGS: dict[str, re.Pattern] = {
    "AUDITOR_REPORT": re.compile(
        r"^\W*(report of the (independent )?auditors?|independent auditors?[’']?s?[’']?s? report)",
        re.IGNORECASE,
    ),
    "ACCOUNTING_POLICIES": re.compile(
        r"^\W*(\d+[.)]?\s*)?(summary of )?(significant )?accounting policies( and .*)?\W*$",
        re.IGNORECASE,
    ),
    "MANAGEMENT_DISCUSSION": re.compile(
        r"^\W*management(\W+s)? discussion (and|&) analysis", re.IGNORECASE
    ),
}


@dataclass
class SectionResult:
    kind: str
    heading: str
    start_page: int
    end_page: int
    text: str
    flags: dict = field(default_factory=dict)


def _head(page: PageData) -> list[str]:
    return [ln.strip() for ln in page.text.splitlines()[:HEADING_LINES] if ln.strip()]


def _heading_kind(page: PageData) -> tuple[str, str] | None:
    for line in _head(page):
        if len(line) > 90:
            continue
        for kind, pattern in SECTION_HEADINGS.items():
            if pattern.match(line):
                return kind, line
    return None


def _ends_section(page: PageData) -> bool:
    """A page that opens a statement, a schedule or another section ends the section above."""
    for line in _head(page):
        if (
            detect.classify_title(line) in (taxonomy.BALANCE_SHEET, taxonomy.CASH_FLOW)
            or detect.classify_disclosure_title(line) == taxonomy.NOTE
        ):
            return True
    return False


def find_sections(pages: list[PageData]) -> list[SectionResult]:
    """Narrative sections by heading. A section runs until the next heading or statement page."""
    starts = [(p.number, *hit) for p in pages if (hit := _heading_kind(p))]
    if not starts:
        return []
    by_number = {p.number: p for p in pages}
    sections: list[SectionResult] = []
    boundaries = sorted({n for n, _, _ in starts})
    for number, kind, heading in starts:
        # consecutive pages repeating the same running heading belong to the section just opened
        if sections and sections[-1].kind == kind and number <= sections[-1].end_page + 1:
            continue
        later = [b for b in boundaries if b > number]
        hard_stop = number + MAX_SECTION_PAGES
        end = number
        for n in range(number + 1, min(hard_stop, max(by_number)) + 1):
            page = by_number.get(n)
            if page is None:
                break
            other = _heading_kind(page)
            if (other and other[0] != kind) or _ends_section(page):
                break
            if later and n >= later[0] and other and other[0] != kind:
                break
            end = n
        text = "\n".join(
            f"[page {n}]\n{by_number[n].text}" for n in range(number, end + 1) if n in by_number
        )
        section = SectionResult(kind, heading[:300], number, end, text[:EXCERPT_CHARS])
        if kind == "AUDITOR_REPORT":
            section.flags = auditor_flags(
                "\n".join(by_number[n].text for n in range(number, end + 1) if n in by_number)
            )
        sections.append(section)
    return sections


_OPINION_HEADINGS = {
    "qualified": re.compile(r"^\W*basis for qualified opinion", re.IGNORECASE | re.MULTILINE),
    "adverse": re.compile(r"^\W*basis for adverse opinion", re.IGNORECASE | re.MULTILINE),
    "disclaimer": re.compile(r"^\W*basis for disclaimer of opinion", re.IGNORECASE | re.MULTILINE),
}
_HEADING_FLAGS = {
    "key_audit_matters": re.compile(r"^\W*key audit matters?\W*$", re.IGNORECASE | re.MULTILINE),
    "emphasis_of_matter": re.compile(r"^\W*emphasis of matter\W*$", re.IGNORECASE | re.MULTILINE),
    "other_matter": re.compile(r"^\W*other matters?\W*$", re.IGNORECASE | re.MULTILINE),
}
_GOING_CONCERN = re.compile(r"material uncertainty related to going concern", re.IGNORECASE)
_TRUE_AND_FAIR = re.compile(r"give a true and fair view", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.;])\s+")


def auditor_flags(text: str) -> dict:
    """Signals the auditor's report prints explicitly, with quotes. No interpretation."""
    opinion = next((name for name, p in _OPINION_HEADINGS.items() if p.search(text)), None)
    if opinion is None:
        opinion = "unmodified" if _TRUE_AND_FAIR.search(text) else "unknown"
    flat = " ".join(text.split())
    quotes = [s[:300] for s in _SENTENCE.split(flat) if "in our opinion" in s.lower()][:2]
    going = _GOING_CONCERN.search(flat)
    if going:
        quotes.append(flat[max(0, going.start() - 120) : going.end() + 120])
    flags = {name: bool(p.search(text)) for name, p in _HEADING_FLAGS.items()}
    flags.update(opinion=opinion, going_concern_uncertainty_mentioned=bool(going), quotes=quotes)
    return flags


# ----------------------------------------------------------------------------- group roster

# A relationship cell holds ONLY relationship words (optionally "of <something short>"): it never
# contains figures or a transaction description. Anything else is a different kind of table.
_KIND = (
    r"(?:wholly[- ]owned )?(?:subsidiary|fellow subsidiary|associate|joint venture|joint operation|"
    r"holding company|ultimate holding company|parent|promoter(?: group)?|"
    r"other related entit(?:y|ies)|key management personnel)"
)
_RELATIONSHIP = re.compile(
    rf"^{_KIND}(?:\s+of\s+[A-Za-z' -]{{2,40}})?$|^consolidated as per [A-Za-z0-9' ().-]{{2,60}}$",
    re.IGNORECASE,
)
_HEADER = re.compile(r"nature of (the )?relationship|relationship", re.IGNORECASE)


@dataclass
class GroupRow:
    entity_name: str
    relationship: str
    page_number: int


def _strip_marks(name: str) -> str:
    """Drop footnote digits glued to the end ("Fund1", "Limited2") and outer spaces."""
    return re.sub(r"(?<=[A-Za-z\)])\d{1,2}$", "", name.strip()).strip(" ,;")


def find_group_entities(pages: list[PageData]) -> list[GroupRow]:
    """Entities a related-party note lists with a nature of relationship (page by page)."""
    rows: list[GroupRow] = []
    for page in pages:
        if page.needs_ocr or not page.words:
            continue
        lines = [merge_split_numbers(ln) for ln in group_lines(page.words)]
        header_at = next(
            (
                i
                for i, ln in enumerate(lines)
                if _HEADER.search(ln.text) and re.search(r"name of|entity|party", ln.text, re.I)
            ),
            None,
        )
        if header_at is None:
            continue
        rel_word = next(
            (w for w in lines[header_at].words if re.match(r"nature|relationship", w.text, re.I)),
            None,
        )
        if rel_word is None:
            continue
        split_x = rel_word.x0 - 4
        previous: GroupRow | None = None
        for line in lines[header_at + 1 :]:
            left = [w for w in line.words if w.x1 <= split_x + 4 or w.x0 < split_x]
            right = [w for w in line.words if w not in left]
            entity = " ".join(w.text for w in left).strip()
            relation = " ".join(w.text for w in right).strip()
            if relation and _RELATIONSHIP.match(_strip_marks(relation)) and entity:
                previous = GroupRow(_strip_marks(entity), _strip_marks(relation), page.number)
                rows.append(previous)
            elif (
                previous is not None
                and entity
                and not relation
                and (
                    previous.entity_name.count("(") > previous.entity_name.count(")")
                    or re.fullmatch(r"(Limited|Ltd\.?|Private Limited|PLC)\d*", entity)
                )
            ):
                previous.entity_name = _strip_marks(f"{previous.entity_name} {entity}")
            elif relation and previous is not None and not entity:
                continue
            else:
                if previous is not None and rows:
                    break  # the roster ended
    seen: set[tuple[str, str]] = set()
    unique = []
    for row in rows:
        key = (row.entity_name.lower(), row.relationship.lower())
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


# ----------------------------------------------------------------------------- shareholding

_SHAREHOLDING_TITLE = re.compile(r"shareholding pattern", re.IGNORECASE)
_SHARES_HEADER = re.compile(r"no\.? of shares", re.IGNORECASE)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKC", text).lower()).strip("_")[:72]


def extract_shareholding(page_number: int, words: list[Word]) -> list[StatementBlock]:
    """A "Shareholding pattern ... at <date>" table: category, number of shares, % holding."""
    lines = [merge_split_numbers(ln) for ln in group_lines(words)]
    blocks: list[StatementBlock] = []
    for i, title in enumerate(lines):
        if not _SHAREHOLDING_TITLE.search(title.text) or len(title.words) > 20:
            continue
        dates = [d for d in _dates(title.text.lower()) if not isinstance(d, str)]
        if len(dates) != 1 or i + 2 >= len(lines):
            continue
        header = lines[i + 1]
        if not (_SHARES_HEADER.search(header.text) and "%" in header.text):
            continue
        body: list = []
        for line in lines[i + 2 :]:
            body.append(line)
            if re.match(r"\s*total\b", line.text, re.IGNORECASE):
                break
        columns = find_columns(body, min_support=3)
        if len(columns) != 2:
            continue
        rows = [split_row(line, columns, k) for k, line in enumerate(body)]
        pending = ""
        facts: list[CellFact] = []
        for row in rows:
            if row is None:
                pending = ""
                continue
            label = " ".join(row.label.split())
            if not row.has_values:
                pending = f"{pending} {label}".strip()
                continue
            wraps = pending and (
                label[:1].islower()
                or pending.count("(") > pending.count(")")
                or pending.rstrip().endswith((",", " and"))
            )
            full = f"{pending} {label}".strip() if wraps else label
            pending = ""
            key = "total" if re.match(r"total\b", full, re.IGNORECASE) else _slug(full)
            for col_index, raw in row.cells.items():
                facts.append(
                    CellFact(
                        key=key,
                        raw_label=full,
                        row_index=row.line_index,
                        column_index=col_index,
                        raw_value=raw,
                        value=parse_cell(raw),
                        decimals=decimals_of(raw),
                        bbox=row.bboxes.get(col_index),
                        page_number=page_number,
                    )
                )
        if not any(f.key == "total" for f in facts) or len({f.key for f in facts}) < 4:
            continue
        period = ColumnPeriod(dates[0], None, True, None)
        blocks.append(
            StatementBlock(
                page_number=page_number,
                statement=taxonomy.SHAREHOLDING,
                title=title.text[:200],
                detection=f"title: {title.text[:120]}",
                basis=None,
                unit=None,
                columns=[
                    ColumnInfo(0, columns[0].x0, columns[0].x1, "No. of Shares", period),
                    ColumnInfo(1, columns[1].x0, columns[1].x1, "% holding", period),
                ],
                facts=facts,
            )
        )
    return blocks


def shareholding_sums(block: StatementBlock) -> list[tuple[int, bool, str, Decimal, Decimal]]:
    """(column, passed, text, difference, tolerance): categories add up to the printed total."""
    out = []
    for column, name in ((0, "shares"), (1, "percent")):
        total = next(
            (f for f in block.facts if f.key == "total" and f.column_index == column), None
        )
        parts = [
            f
            for f in block.facts
            if f.key != "total" and f.column_index == column and f.value is not None
        ]
        if total is None or total.value is None or len(parts) < 3:
            continue
        computed = sum((f.value for f in parts), Decimal(0))
        decimals = max([total.decimals, *(f.decimals for f in parts)])
        # share counts are exact integers: no rounding slack at all; percentages are rounded
        tolerance = (
            Decimal(0)
            if column == 0
            else Decimal(1).scaleb(-decimals) * Decimal(len(parts) + 1) / 2
        )
        difference = total.value - computed
        out.append(
            (
                column,
                abs(difference) <= tolerance,
                f"shareholding {name}: total {total.value} vs sum of {len(parts)} "
                f"categories = {computed}",
                difference,
                tolerance,
            )
        )
    return out
