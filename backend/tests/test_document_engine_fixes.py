"""Extraction repairs found on real filings: mid-row labels, OCR-only unit lines, scan re-reads."""

from pathlib import Path

import pytest

from app.docintel import pdf as pdfmod
from app.docintel.layout import Word, group_lines, move_middle_labels
from app.docintel.statements import _strip_enumeration, extract_page_statements


def w(x0, y, text, width=None):
    width = width or 6 * len(text)
    return Word(x0, y, x0 + width, y + 8, text)


def middle_label_page(rows=10):
    """`[3 figures] LABEL [3 figures]`, the layout of a real Nestle results page."""
    words = [w(60, 20, "Particulars", 50)]
    for i, text in enumerate(("30.09.2026", "30.06.2026", "30.09.2025")):
        words.append(w(20 + 45 * i, 40, text, 40))
    for i, text in enumerate(("30.09.2026", "30.09.2025", "31.03.2026")):
        words.append(w(300 + 45 * i, 40, text, 40))
    labels = [
        "Domestic sales",
        "Export sales",
        "Revenue from Operations",
        "Other income",
        "Total Income",
        "Cost of materials consumed",
        "Employee benefits expense",
        "Finance costs",
        "Depreciation and amortisation",
        "Other expenses",
    ]
    for r in range(rows):
        y = 60 + 12 * r
        for i in range(3):
            words.append(w(20 + 45 * i, y, f"{100 + r}.{i}", 40))
        words.append(w(150, y, labels[r % len(labels)], 130))
        for i in range(3):
            words.append(w(300 + 45 * i, y, f"{200 + r}.{i}", 40))
    return words


def test_labels_between_two_groups_of_figures_move_to_the_left_edge():
    words = middle_label_page()
    moved = move_middle_labels(words)
    lines = group_lines(moved)
    row = next(ln for ln in lines if "Domestic" in ln.text)
    assert row.text.startswith("Domestic sales 100.0 100.1 100.2 200.0")  # label first, order kept
    figures = {(x.x0, x.y0, x.text) for x in words if x.text[0].isdigit()}
    assert figures <= {(x.x0, x.y0, x.text) for x in moved}  # figures never move


def test_an_ordinary_page_is_returned_untouched():
    normal = [w(20, 40 + 12 * r, "Revenue from operations", 120) for r in range(10)]
    normal += [w(200 + 45 * i, 40 + 12 * r, "1,234.5", 40) for r in range(10) for i in range(3)]
    assert move_middle_labels(normal) == normal


def test_serial_numbers_do_not_make_a_page_look_like_middle_labels():
    rows = []
    for r in range(10):
        y = 40 + 12 * r
        rows += [w(5, y, str(r + 1), 8), w(30, y, "Interest earned", 90)]
        rows += [w(200 + 45 * i, y, "1,234.5", 40) for i in range(3)]
    assert move_middle_labels(rows) == rows


def test_a_title_line_that_mixes_in_words_outside_the_label_column_is_not_reordered():
    words = middle_label_page()
    title = [w(10, 5, "Standalone", 60), w(100, 5, "Financial", 50), w(170, 5, "Results", 50)]
    moved = move_middle_labels(words + title)
    line = next(ln for ln in group_lines(moved) if "Standalone" in ln.text)
    assert line.text == "Standalone Financial Results"


def test_the_statement_reader_now_finds_rows_when_the_label_is_in_the_middle():
    words = middle_label_page()
    words += [w(100, 10, "Statement of Profit and Loss", 150)]
    blocks = extract_page_statements(1, words)
    assert blocks and blocks[0].statement == "INCOME"
    assert {f.key for f in blocks[0].facts} >= {"other_income", "finance_costs"}


# ---- OCR debris in labels ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("4 |interest earned (a}+(b}+(c}+(d)", "interest earned"),
        ("{a) |interest/ discount on advances/ bills", "interest/ discount on advances/ bills"),
        ("(a) |Employee cost", "Employee cost"),
        ("2 [Other income", "Other income"),
        ("6. Total income (1)+(2)", "Total income"),
        ("Profit for the period (A-B)", "Profit for the period (A-B)"),  # real wording stays
        ("Revenue from operations", "Revenue from operations"),
    ],
)
def test_ocr_ruling_marks_and_cross_references_are_stripped_from_labels(raw, clean):
    assert _strip_enumeration(raw) == clean


# ---- unit line only readable by OCR --------------------------------------------------------------------


def test_a_figure_page_without_a_unit_statement_is_flagged_for_a_unit_read():
    words = [(x.x0, x.y0, x.x1, x.y1, x.text) for x in middle_label_page()]
    text = "Domestic sales 100.0 ..."
    assert pdfmod.lacks_unit_statement(words, text) is True
    assert pdfmod.lacks_unit_statement(words, "(Rs. in million) Domestic sales") is False
    few = [(0, 0, 5, 5, "1,000"), (6, 0, 12, 5, "2,000")]
    assert pdfmod.lacks_unit_statement(few, "a covering letter") is False  # prose page: no OCR


def test_only_alphabetic_ocr_words_not_already_in_the_text_layer_are_added():
    text_words = [(10, 10, 60, 20, "Revenue"), (200, 10, 240, 20, "1,234.5")]

    class FakeOcrWord:
        def __init__(self, x0, text):
            self.x0, self.y0, self.x1, self.y1, self.text = x0, 0.0, x0 + 40, 9.0, text

    ocr = [
        FakeOcrWord(300, "in"),
        FakeOcrWord(330, "million)"),
        FakeOcrWord(15, "Revenue"),
        FakeOcrWord(250, "9,999.9"),
    ]
    ocr[2].y0, ocr[2].y1 = 10, 20
    merged, added = pdfmod.merge_ocr_labels(text_words, ocr)
    texts = [m[4] for m in merged]
    assert added == 2 and "in" in texts and "million)" in texts
    assert texts.count("Revenue") == 1  # already present, not duplicated
    assert "9,999.9" not in texts  # an OCR digit is never used


# ---- scanned pages ----------------------------------------------------------------------------------------


def test_the_malformed_word_share_separates_a_bad_scan_from_clean_text():
    assert (
        pdfmod.malformed_share("Particulars Quaner ended llJnauditedl lUnaudltedl Audited") > 0.03
    )
    assert pdfmod.malformed_share("Particulars Quarter ended Unaudited Audited Income") == 0.0
    assert pdfmod.malformed_share("") == 0.0


def test_the_more_confident_reading_of_a_poor_scan_wins(monkeypatch):
    from app.docintel.ocr import OcrWord

    def words(conf):
        return [OcrWord(0, 0, 10, 8, "x", conf=conf)]

    readings = {(300, 6): words(48), (450, 4): words(80), (450, 11): words(83), (300, 3): words(81)}
    calls = []

    def fake_ocr(png, *, dpi=300, psm=6):
        calls.append((dpi, psm))
        return readings[(dpi, psm)]

    monkeypatch.setattr(pdfmod, "ocr_png", fake_ocr)
    monkeypatch.setattr(pdfmod, "render_png", lambda page, dpi=300: b"")
    best = pdfmod.ocr_best(object())
    assert best[0].conf == 83 and calls[0] == (300, 6) and len(calls) == 4


def test_a_good_first_reading_is_not_repeated(monkeypatch):
    from app.docintel.ocr import OcrWord

    calls = []
    monkeypatch.setattr(
        pdfmod, "ocr_png", lambda png, **k: calls.append(k) or [OcrWord(0, 0, 9, 8, "x", conf=92)]
    )
    monkeypatch.setattr(pdfmod, "render_png", lambda page, dpi=300: b"")
    assert pdfmod.ocr_best(object())[0].conf == 92 and len(calls) == 1


# ---- the real filing (skipped where the stored file is not present) ------------------------------------

NESTLE = (
    Path(__file__).resolve().parents[2]
    / "data/documents/49/49501ea84c2d91cc319bb903838f1910e87aacb1e20414121265b1ce4b4c8e0b.pdf"
)


@pytest.mark.skipif(not NESTLE.exists(), reason="the stored Nestle results PDF is not present")
def test_the_real_nestle_results_pdf_now_yields_its_statements():
    content = pdfmod.read_pdf(NESTLE, use_ocr=False)
    found = {}
    for page in content.pages:
        for block in extract_page_statements(page.number, page.words):
            found[(page.number, block.statement, block.basis)] = len(block.facts)
    assert found[(2, "INCOME", "STANDALONE")] >= 25
    assert found[(7, "INCOME", "CONSOLIDATED")] >= 25
    assert found[(3, "BALANCE_SHEET", "STANDALONE")] >= 15
