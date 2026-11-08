"""LLM-assisted reading of headers and tables: the model interprets, code verifies and reads values."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.docintel import taxonomy
from app.docintel.detect import find_units
from app.docintel.layout import Word
from app.docintel.periods import ColumnPeriod, parse_column_header
from app.docintel.statements import (
    CellFact,
    ColumnInfo,
    StatementBlock,
    _drop_serial_number_columns,
)
from app.llm.base import LlmResponse
from app.llm.headers import dates_in_order, resolve_headers, verify_interpretation
from app.llm.tables import (
    PageGrid,
    amount_columns,
    build_block,
    build_grid,
    label_belongs_to_line,
    read_missing_tables,
    render_page,
    withhold_inconsistent_periods,
)

PAGE_TEXT = "Quarter ended\n30.09.2026\n30.09.2025\nYear ended\n31.03.2026"


# ---- header verification --------------------------------------------------------------------------


def item(end, months, quote):
    return {"period_end": end, "duration_months": months, "quote": quote}


def test_verified_period_needs_a_real_quote_and_matching_month_and_year():
    ok, why = verify_interpretation(
        item("2026-09-30", 3, "Quarter ended 30.09.2026"), PAGE_TEXT.replace("\n", " "), False
    )
    assert why is None and ok.period_end == date(2026, 9, 30) and ok.duration_months == 3
    bad = [
        item("2026-09-30", 3, "Quarter ended 30.06.2026"),  # not in the text
        item("2026-09-30", 3, "Quarter ended"),  # no date inside the quote
        item("2026-09-30", 3, ""),
        item(None, None, "Quarter ended 30.09.2026"),
        item("30/09/2026", 3, "Quarter ended 30.09.2026"),
    ]
    for entry in bad:
        period, reason = verify_interpretation(entry, PAGE_TEXT.replace("\n", " "), False)
        assert period is None and reason


def test_duration_and_fiscal_alignment_are_enforced():
    text = "Year ended 31.03.2026 Quarter ended 30.09.2026"
    assert verify_interpretation(item("2026-03-31", 12, "Year ended 31.03.2026"), text, False)[0]
    assert (
        verify_interpretation(item("2026-09-30", 7, "Quarter ended 30.09.2026"), text, False)[0]
        is None
    )
    # a 12-month column ending in September contradicts the April-March fiscal year
    assert (
        verify_interpretation(item("2026-09-30", 12, "Quarter ended 30.09.2026"), text, False)[0]
        is None
    )
    assert verify_interpretation(item("2026-09-30", 6, "Quarter ended 30.09.2026"), text, False)[0]


def test_balance_sheet_columns_are_dates_without_duration():
    period, _ = verify_interpretation(
        item("2026-03-31", None, "As at 31 March 2026"), "As at 31 March 2026", True
    )
    assert (
        period.period_end == date(2026, 3, 31)
        and period.duration_months is None
        and period.point_in_time
    )


def test_stacked_headings_need_every_quoted_word_and_the_date_on_the_page():
    text = "Quarter ended Six months ended 30th September 2026 2025"
    entry = item("2026-09-30", 3, "quarter ended 30th September 2026")
    assert verify_interpretation(entry, text, False)[0] is None  # not contiguous
    assert verify_interpretation(entry, text, False, stacked=True)[0] is not None
    invented = item("2026-09-30", 3, "quarter ended 30th September 2026 restated")
    assert verify_interpretation(invented, text, False, stacked=True)[0] is None
    wrong_year = item("2027-09-30", 3, "quarter ended 30th September")
    assert verify_interpretation(wrong_year, text, False, stacked=True)[0] is None


def test_dates_in_order_hint():
    assert dates_in_order("30.09.2026 foo 30.09.2025 31.03.2026 1234.56") == [
        "30.09.2026",
        "30.09.2025",
        "31.03.2026",
    ]


# ---- deterministic repairs found on real filings ----------------------------------------------------


def test_fused_day_month_year_headers_parse():
    for text, end, months in [
        ("Three months ended 30June 2026 (Unaudited)", date(2026, 6, 30), 3),
        ("Three months ended 31March 2026 (Audited)", date(2026, 3, 31), 3),
        ("Nine months ended 31December2025", date(2025, 12, 31), 9),
    ]:
        period = parse_column_header(text)
        assert (period.period_end, period.duration_months, period.problem) == (end, months, None)


def test_garbled_rupee_unit_forms_are_read_only_inside_brackets():
    assert find_units("(Uncrores) Previous year") == {"crore"}
    assert find_units("('tincrores) Previous") == {"crore"}
    assert find_units("Uncrores without brackets") == set()
    assert find_units("(H crore)") == {"crore"}


@dataclass
class _Facts:
    rows: list


def cell(key, col, raw, row=0):
    return CellFact(
        key, key, row, col, raw, Decimal(raw.replace(",", "").strip("()")), 2, None, page_number=1
    )


def column(index, end=None, months=None):
    period = (
        ColumnPeriod(end, months, False, None)
        if end
        else ColumnPeriod(None, None, False, None, "no date")
    )
    return ColumnInfo(index, 0, 0, f"c{index}", period)


def test_serial_number_column_is_dropped_but_a_real_integer_column_is_not():
    block = StatementBlock(
        1,
        taxonomy.INCOME,
        None,
        "t",
        "STANDALONE",
        "crore",
        [
            column(0, date(2026, 9, 30), 3),
            column(1, date(2026, 9, 30), 3),
            column(2, date(2025, 9, 30), 3),
        ],
    )
    for r, serial in enumerate(["1", "2", "3", "4"]):
        block.facts.append(cell("INCOME.a", 0, serial, r))
        block.facts.append(cell("INCOME.a", 1, f"{r + 1}0.50", r))
        block.facts.append(cell("INCOME.a", 2, f"{r + 2}0.25", r))
    _drop_serial_number_columns(block)
    assert block.column(0).is_note and block.column(0).problem.startswith("serial-number")
    assert all(f.column_index != 0 for f in block.facts) and len(block.facts) == 8

    integers = StatementBlock(
        1,
        taxonomy.INCOME,
        None,
        "t",
        "STANDALONE",
        "crore",
        [column(0, date(2026, 9, 30), 3), column(1, date(2025, 9, 30), 3)],
    )
    for r in range(4):
        integers.facts.append(cell("INCOME.a", 0, str(r + 5), r))
        integers.facts.append(cell("INCOME.a", 1, str(r + 9), r))
    _drop_serial_number_columns(integers)  # no decimals anywhere: not provably a serial column
    assert not integers.column(0).is_note and len(integers.facts) == 8


# ---- table reader ----------------------------------------------------------------------------------


def w(text, x, y):
    return Word(x - 15, y, x + 15, y + 8, text)


def mirrored_page():
    """Left amount group (x 100,150), label in the middle, right group (x 400,450)."""
    words = []
    y = 10
    heading = [
        ("Quarter", 100),
        ("ended", 125),
        ("30.09.2026", 150),
        ("Six", 400),
        ("months", 425),
        ("ended", 440),
        ("30.09.2026", 450),
    ]
    words += [w(t, x, y) for t, x in heading]
    rows = [
        ("Revenue from operations", ["100", "90"], ["200", "180"]),
        ("Other income", ["10", "9"], ["20", "18"]),
        ("TOTAL INCOME", ["110", "99"], ["220", "198"]),
        ("TOTAL EXPENSES", ["80", "70"], ["160", "140"]),
        ("Profit before tax", ["30", "29"], ["60", "58"]),
    ]
    for label, left, right in rows:
        y += 20
        words.append(w(left[0], 100, y))
        words.append(w(left[1], 150, y))
        words.append(w(label, 280, y))
        words.append(w(right[0], 400, y))
        words.append(w(right[1], 450, y))
    text = "Quarter ended 30.09.2026 Six months ended 30.09.2026 Consolidated crore " + " ".join(
        r[0] for r in rows
    )
    return words, text


@dataclass
class Page:
    number: int
    text: str
    words: list
    needs_ocr: bool = False


def fake_response(data):
    return LlmResponse(data=data, raw_text="", provider="groq", model="m")


def test_amount_columns_follow_geometry_on_both_sides_of_the_label():
    words, _ = mirrored_page()
    grid = build_grid(words)
    assert len(grid.centres) == 4
    assert [round(c) for c in grid.centres] == [100, 150, 400, 450]
    assert "NUMBER COLUMNS" in render_page(grid) and "100@100" in render_page(grid)


def test_values_are_read_by_position_and_never_come_from_the_model(monkeypatch):
    monkeypatch.setattr("app.llm.tables.MIN_ROWS", 4)
    words, text = mirrored_page()
    page = Page(7, text.replace("Consolidated crore", "Consolidated in crore"), words)
    grid = build_grid(words)
    answer = {
        "columns": [
            {
                "index": 0,
                "period_end": "2026-09-30",
                "duration_months": 3,
                "quote": "Quarter ended 30.09.2026",
            },
            {
                "index": 2,
                "period_end": "2026-09-30",
                "duration_months": 6,
                "quote": "Six months ended 30.09.2026",
            },
        ],
        "rows": [
            {
                "label": "Revenue from operations",
                "line": 2,
                "value": "999",
            },  # extra fields are ignored
            {"label": "Other income", "line": 3},
            {"label": "TOTAL INCOME", "line": 4},
            {"label": "TOTAL EXPENSES", "line": 5},
            {"label": "Profit before tax", "line": 6},
            {"label": "Revenue from operations", "line": 3},  # label not on that line: refused
        ],
    }
    block, dropped = build_block(page, grid, answer, fake_response(answer))
    assert block is not None and dropped >= 1
    values = {(f.key, f.column_index): f.raw_value for f in block.facts}
    assert values[("revenue_from_operations", 0)] == "100"
    assert values[("revenue_from_operations", 2)] == "200"
    assert "999" not in values.values()
    assert block.basis == "CONSOLIDATED" and block.unit == "crore"
    assert block.column(0).usable and block.column(2).usable
    assert {c.index for c in block.columns} == {0, 2}  # columns the model did not name are absent
    assert all(f.column_index in (0, 2) for f in block.facts)
    assert any(i["code"] == "LLM_TABLE" for i in block.issues)


def test_label_must_belong_to_the_line_that_carries_the_amounts():
    words, _ = mirrored_page()
    grid = build_grid(words)
    lines = grid.lines
    assert label_belongs_to_line(lines, 3, "Other income")
    assert not label_belongs_to_line(
        lines, 3, "Revenue from operations"
    )  # a different row's heading
    assert not label_belongs_to_line(lines, 3, "")


def test_periods_that_contradict_each_other_are_withheld():
    cols = [
        column(0, date(2026, 9, 30), 3),
        column(1, date(2026, 6, 30), 3),
        column(2, date(2026, 9, 30), 6),
    ]
    good = StatementBlock(1, taxonomy.INCOME, None, "t", "STANDALONE", "crore", cols)
    for key, (a, b, c) in {
        "INCOME.total_income": ("110", "90", "200"),
        "INCOME.total_expenses": ("80", "60", "140"),
    }.items():
        good.facts += [cell(key, 0, a), cell(key, 1, b), cell(key, 2, c)]
    assert withhold_inconsistent_periods(good) == [] and all(c.usable for c in good.columns)

    cols2 = [
        column(0, date(2026, 9, 30), 3),
        column(1, date(2026, 6, 30), 3),
        column(2, date(2026, 9, 30), 6),
    ]
    bad = StatementBlock(1, taxonomy.INCOME, None, "t", "STANDALONE", "crore", cols2)
    # the "six months" column actually holds the June quarter
    for key, (a, b, c) in {
        "INCOME.total_income": ("110", "90", "90"),
        "INCOME.total_expenses": ("80", "60", "60"),
    }.items():
        bad.facts += [cell(key, 0, a), cell(key, 1, b), cell(key, 2, c)]
    notes = withhold_inconsistent_periods(bad)
    assert notes and all(not c.usable for c in bad.columns)
    assert "contradicted" in bad.column(2).problem


def test_only_results_documents_and_poorly_read_income_pages_are_sent():
    words, text = mirrored_page()
    page = Page(7, text + " profit before tax revenue from operations total income", words)

    class Router:
        calls = 0

        def complete_json(self, request):
            Router.calls += 1
            return fake_response({"columns": [], "rows": []})

    blocks, outcome = read_missing_tables([page], [], "annual_report", Router())
    assert blocks == [] and outcome.pages_asked == 0 and Router.calls == 0
    _, outcome = read_missing_tables([page], [], "quarterly_result", Router())
    assert outcome.pages_asked == 1 and Router.calls == 1


# ---- header resolver --------------------------------------------------------------------------------


def test_header_resolver_assigns_periods_only_when_the_quote_checks_out():
    cols = [
        ColumnInfo(
            0,
            0,
            0,
            "Quarter ended 30.09.2026",
            ColumnPeriod(None, None, False, None, "duration not stated"),
            problem="duration not stated",
        ),
        ColumnInfo(
            1,
            0,
            0,
            "Quarter ended 31.12.2099",
            ColumnPeriod(None, None, False, None, "no date in header"),
            problem="no date in header",
        ),
    ]
    block = StatementBlock(1, taxonomy.INCOME, None, "t", "STANDALONE", "crore", cols)

    class Router:
        def complete_json(self, request):
            assert "28152" not in request.user  # amounts are never sent
            return fake_response(
                {
                    "columns": [
                        {
                            "index": 0,
                            "period_end": "2026-09-30",
                            "duration_months": 3,
                            "quote": "Quarter ended 30.09.2026",
                        },
                        {
                            "index": 1,
                            "period_end": "2099-12-31",
                            "duration_months": 3,
                            "quote": "totally invented",
                        },
                    ]
                }
            )

    outcome = resolve_headers([block], {1: "Quarter ended 30.09.2026 28152.21 165.46"}, Router())
    assert outcome.resolved == 1 and outcome.refused == 1
    assert block.column(0).usable and block.column(0).period.duration_months == 3
    assert not block.column(1).usable and any("refused" in r for r in block.column(1).repairs)
    assert any("groq/m" in r for r in block.column(0).repairs)


def test_grid_helper_types():
    assert isinstance(PageGrid([], []), PageGrid) and amount_columns([]) == []
