"""Layouts found in real annual reports that the statement reader must handle (BPCL FY2026).

Everything here is synthetic geometry; no company figures. The shapes are copied from the layouts
of real filings: two statements side by side on one page, and a duration printed once in the
heading instead of in each column.
"""

from datetime import date

from app.docintel import taxonomy
from app.docintel.layout import Word
from app.docintel.periods import stated_duration
from app.docintel.statements import extract_page_statements, two_up_gutter
from tests.docintel_pages import LABEL_X, Page, balance_sheet_page, income_page, words

SHIFT = 700.0


def two_up_page() -> list[Word]:
    """A balance sheet (left) and a profit-and-loss account (right) printed on one wide page."""
    left = balance_sheet_page()
    right = [Word(w.x0 + SHIFT, w.y0 - 14, w.x1 + SHIFT, w.y1 - 14, w.text) for w in income_page()]
    return left + right


def test_two_statements_side_by_side_are_read_as_two_tables():
    blocks = extract_page_statements(1, two_up_page())
    kinds = {b.statement for b in blocks}
    assert taxonomy.BALANCE_SHEET in kinds and taxonomy.INCOME in kinds
    for block in blocks:
        assert any(c.usable for c in block.columns), block.title


def test_a_two_up_page_finds_a_gutter_between_the_tables():
    gutter = two_up_gutter(two_up_page())
    assert gutter is not None and 560 < gutter < SHIFT + LABEL_X


def test_an_ordinary_single_statement_page_is_never_split():
    assert two_up_gutter(balance_sheet_page()) is None
    assert two_up_gutter(income_page()) is None


def test_two_titles_with_words_crossing_between_them_are_not_split():
    page = two_up_page()
    # a long line of prose running across the middle of the page leaves no empty strip
    page += words(400.0, 40.0, " ".join(["text"] * 150))
    assert two_up_gutter(page) is None


def test_stated_duration_reads_a_heading_that_names_one_length():
    assert stated_duration("Statement of Cash Flows for the year ended March 31, 2026") == 12
    assert stated_duration("for the quarter ended 30 September 2026") == 3
    assert stated_duration("for the six months ended 30 September 2026") == 6


def test_stated_duration_says_nothing_when_lengths_conflict_or_are_absent():
    assert stated_duration("quarter ended and year ended") is None
    assert stated_duration("March 31, 2026") is None
    assert stated_duration("") is None


def cash_flow_with_heading_duration() -> list[Word]:
    page = Page()
    page.text("Standalone Cash Flow Statement")
    page.text("for the year ended 31 March 2026")
    page.text("(₹ in crore)", 540, right=True)
    page.raw(words(0, 460, "31 March 2026", right=True), words(0, 540, "31 March 2025", right=True))
    for label, a, b in [
        ("Net cash from operating activities (A)", "500.00", "450.00"),
        ("Purchase of property, plant and equipment", "(200.00)", "(180.00)"),
        ("Net cash used in investing activities (B)", "(300.00)", "(250.00)"),
        ("Dividends paid", "(50.00)", "(40.00)"),
    ]:
        page.row(label, a, b, rights=(460.0, 540.0))
    return page.items


def test_a_duration_printed_once_in_the_heading_applies_to_every_column():
    blocks = extract_page_statements(1, cash_flow_with_heading_duration())
    cash = next(b for b in blocks if b.statement == taxonomy.CASH_FLOW)
    assert [c.period.duration_months for c in cash.columns if c.usable] == [12, 12]
    assert {c.period.period_end for c in cash.columns if c.usable} == {
        date(2026, 3, 31),
        date(2025, 3, 31),
    }


def test_a_column_without_any_stated_duration_is_still_withheld():
    page = cash_flow_with_heading_duration()
    page = [w for w in page if w.text not in ("for", "year", "ended") or w.y0 > 60]
    cash = [b for b in extract_page_statements(1, page) if b.statement == taxonomy.CASH_FLOW]
    for block in cash:
        assert not any(c.usable for c in block.columns)
