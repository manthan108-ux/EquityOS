"""Balance-sheet pages with a trailing note table and one-phrase 'As on' headers (Axis Q2 FY27)."""

from datetime import date

import pytest

from app.docintel.pdf import read_pdf
from app.docintel.statements import extract_page_statements
from tests.test_research_vault import make_pdf

pymupdf = pytest.importorskip("pymupdf")

ROWS = [
    ("Capital", ("622.74", "621.63", "620.58")),
    ("Reserves and Surplus", ("2,29,042.49", "2,12,957.29", "1,97,638.87")),
    ("Deposits", ("14,50,287.83", "13,33,791.44", "12,01,655.60")),
    ("Borrowings", ("3,24,487.88", "2,80,510.62", "2,40,841.49")),
    ("Other Liabilities and Provisions", ("1,00,970.14", "1,10,208.88", "82,480.47")),
    ("TOTAL", ("21,05,513.00", "19,38,727.00", "17,23,239.00")),
]


def balance_sheet(tmp_path, *, trailing_table: bool, tight_header: bool):
    right = (365, 429, 493)  # right edges of the three value columns
    lines = [
        (60, 30, "AXIS BANK LIMITED"),
        (60, 44, "1. Consolidated Statement of Assets and Liabilities is given below:"),
        (455, 56, "(Rs. in crores)"),
    ]
    if tight_header:  # one phrase: the gaps between the three dates are narrower than a letter
        lines.append((306, 68, "As on 30.09.2026 As on 31.03.2026 As on 30.09.2025"))
    else:
        lines += [
            (306, 68, "As on 30.09.2026"),
            (370, 68, "As on 31.03.2026"),
            (434, 68, "As on 30.09.2025"),
        ]
    lines += [(314, 80, "(Unaudited)"), (382, 80, "(Audited)"), (442, 80, "(Unaudited)")]
    lines.append((60, 80, "Particulars"))
    y = 96
    for label, values in ROWS:
        lines.append((60, y, label))
        for x, v in zip(right, values, strict=True):
            lines.append((x - 6.2 * len(v) * 0.6 * 1.0, y, v))
        y += 13
    if trailing_table:
        lines += [
            (
                60,
                y + 10,
                "2. Details of resolution plans implemented under the RBI framework are as under:",
            ),
            (60, y + 24, "Type of borrower"),
            (60, y + 36, "Personal loans"),
            (60, y + 48, "Corporate persons"),
        ]
        for n, (a, b, c) in enumerate(
            [("814.16", "4.19", "3.75"), ("144.16", "17.20", "161.36"), ("90.00", "1.10", "2.20")]
        ):
            for x, v in zip((300, 350, 396), (a, b, c), strict=True):
                lines.append((x, y + 36 + 12 * n, v))
    path = tmp_path / "bs.pdf"
    path.write_bytes(make_pdf(lines))
    return read_pdf(path, use_ocr=False).pages[0]


def blocks(page):
    return [b for b in extract_page_statements(1, page.words) if b.statement == "BALANCE_SHEET"]


def test_a_note_table_below_the_balance_sheet_adds_no_extra_column(tmp_path):
    (block,) = blocks(balance_sheet(tmp_path, trailing_table=True, tight_header=False))
    assert [c.period.period_end for c in block.columns] == [
        date(2026, 9, 30),
        date(2026, 3, 31),
        date(2025, 9, 30),
    ]
    assert all(c.usable for c in block.columns)
    assert {f.key for f in block.facts} >= {"deposits", "capital"}


def test_one_phrase_headers_with_a_repeated_lead_are_split_per_column(tmp_path):
    (block,) = blocks(balance_sheet(tmp_path, trailing_table=False, tight_header=True))
    assert [c.period.period_end for c in block.columns] == [
        date(2026, 9, 30),
        date(2026, 3, 31),
        date(2025, 9, 30),
    ]
    assert all(c.usable and c.period.point_in_time for c in block.columns)
    deposits = {f.column_index: str(f.value) for f in block.facts if f.key == "deposits"}
    assert deposits == {0: "1450287.83", 1: "1333791.44", 2: "1201655.60"}


def test_both_problems_together(tmp_path):
    (block,) = blocks(balance_sheet(tmp_path, trailing_table=True, tight_header=True))
    assert len(block.columns) == 3 and all(c.usable for c in block.columns)
    assert block.basis == "CONSOLIDATED"


def test_a_genuine_spanning_header_is_still_kept_whole(tmp_path):
    """'Quarter ended 30.09.2026 30.06.2026' must not hand its lead words to one column only."""
    lines = [
        (60, 30, "Statement of Profit and Loss"),
        (60, 44, "Standalone"),
        (455, 56, "(Rs. in crores)"),
        (250, 68, "Quarter ended 30.09.2026 30.06.2026"),
        (60, 96, "Revenue from operations"),
        (60, 109, "Other income"),
        (60, 122, "Total income"),
        (60, 135, "Profit before tax"),
        (60, 148, "Net profit"),
    ]
    for x, vals in (
        (330, ("100.00", "10.00", "110.00", "30.00", "25.00")),
        (400, ("90.00", "9.00", "99.00", "27.00", "22.00")),
    ):
        for n, v in enumerate(vals):
            lines.append((x, 96 + 13 * n, v))
    path = tmp_path / "pl.pdf"
    path.write_bytes(make_pdf(lines))
    page = read_pdf(path, use_ocr=False).pages[0]
    found = [b for b in extract_page_statements(1, page.words) if b.statement == "INCOME"]
    for block in found:  # whatever it concludes, it must not give only ONE column a period
        periods = [c.period.period_end for c in block.columns if c.usable]
        assert len(periods) in (0, 2)


# ---- bank-format balance sheet: two bare TOTAL rows and an apostrophe in a label -------------------


def bank_balance_sheet(tmp_path):
    right = (365, 429, 493)
    liabilities = [
        ("Capital", ("622.74", "621.63", "620.58")),
        ("Employees' Stock Options Outstanding", ("1,368.81", "1,342.16", "1,193.79")),
        ("Reserves and Surplus", ("2,18,126.61", "2,03,572.63", "1,89,326.58")),
        ("Deposits", ("14,52,066.21", "13,35,833.97", "12,03,486.91")),
        ("Borrowings", ("2,73,085.52", "2,35,270.61", "1,99,506.02")),
        ("Other Liabilities and Provisions", ("1,00,970.14", "1,10,208.88", "82,480.47")),
        ("TOTAL", ("20,46,240.03", "18,86,849.88", "16,76,614.35")),
    ]
    assets = [
        ("Cash and Balances with Reserve Bank of India", ("82,241.72", "78,627.36", "63,842.56")),
        (
            "Balances with Banks and Money at Call and Short Notice",
            ("44,311.20", "26,275.32", "19,392.94"),
        ),
        ("Investments", ("4,53,464.34", "4,45,032.80", "3,91,850.80")),
        ("Advances", ("13,73,726.80", "12,33,569.88", "11,16,703.01")),
        ("Fixed Assets", ("6,640.02", "6,549.36", "6,413.46")),
        ("Other Assets", ("85,855.95", "96,795.16", "78,411.58")),
        ("TOTAL", ("20,46,240.03", "18,86,849.88", "16,76,614.35")),
    ]
    lines = [
        (60, 30, "1. Consolidated Statement of Assets and Liabilities is given below:"),
        (455, 44, "(Rs. in crores)"),
        (306, 56, "As on 30.09.2026"), (370, 56, "As on 31.03.2026"), (434, 56, "As on 30.09.2025"),
        (60, 56, "Particulars"),
    ]  # fmt: skip
    y = 70
    for heading, rows in (("CAPITAL AND LIABILITIES", liabilities), ("ASSETS", assets)):
        lines.append((60, y, heading))
        y += 13
        for label, values in rows:
            lines.append((60, y, label))
            for x, v in zip(right, values, strict=True):
                lines.append((x - 3.7 * len(v), y, v))
            y += 13
    path = tmp_path / "bank_bs.pdf"
    path.write_bytes(make_pdf(lines))
    return read_pdf(path, use_ocr=False).pages[0]


def test_a_bank_balance_sheet_is_checked_through_its_two_total_rows(tmp_path):
    from app.docintel import validate

    (block,) = blocks(bank_balance_sheet(tmp_path))
    keys = {f.key for f in block.facts}
    assert {
        "total_capital_and_liabilities",
        "total_assets",
        "employee_stock_options_outstanding",
    } <= keys
    results = validate.run_checks(block)
    assert {r.name for r in results} >= {
        "total assets = total equity and liabilities",
        "bank: capital and liabilities add up",
        "bank: assets add up",
    }
    assert all(r.passed for r in results)
    assert all(validate.column_verified(results, c.index) for c in block.columns)
    assert block.basis == "CONSOLIDATED"


def test_a_bare_total_without_a_side_heading_is_not_guessed(tmp_path):
    lines = [
        (60, 30, "Consolidated Statement of Assets and Liabilities"),
        (455, 44, "(Rs. in crores)"),
        (330, 56, "As on 30.09.2026"),
        (60, 70, "Deposits"), (330, 70, "100.00"),
        (60, 83, "Borrowings"), (330, 83, "50.00"),
        (60, 96, "Other Liabilities"), (330, 96, "25.00"),
        (60, 109, "TOTAL"), (330, 109, "175.00"),
    ]  # fmt: skip
    path = tmp_path / "x.pdf"
    path.write_bytes(make_pdf(lines))
    page = read_pdf(path, use_ocr=False).pages[0]
    for block in blocks(page):
        assert "total_assets" not in {f.key for f in block.facts}
        assert "total_capital_and_liabilities" not in {f.key for f in block.facts}
