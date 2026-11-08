"""Investor-page link classification: procedural documents are not financial results."""

import pytest

from app.providers.company_ir import extract_ir_links

BASE = "https://www.example.com/investors"


def kind_of(text: str, path: str) -> str:
    html = f'<a href="{path}">{text}</a>'
    (link,) = extract_ir_links(html, BASE)
    return link.kind


@pytest.mark.parametrize(
    "text,path",
    [
        (
            "Voting results and scrutinizer's report 2026",
            "/pdfs/voting-results-and-scrutinizer-s-report-2026.pdf",
        ),
        ("Postal ballot results", "/pdfs/postal-ballot-results.pdf"),
        ("Notice of AGM", "/pdfs/notice-of-agm-results.pdf"),
        ("Intimation of results date", "/pdfs/intimation-board-meeting-results.pdf"),
        (
            "Newspaper advertisement of financial results",
            "/pdfs/newspaper-advertisement-results.pdf",
        ),
    ],
)
def test_procedural_documents_are_not_financial_results(text, path):
    assert kind_of(text, "/investors" + path) != "financial_results"


@pytest.mark.parametrize(
    "text,path",
    [
        ("Quarterly results Q2 FY27", "/pdfs/quarterly-results-q2-fy27.pdf"),
        ("Unaudited financial results for the quarter ended September 30, 2026", "/pdfs/q2.pdf"),
        ("Consolidated financial statement", "/pdfs/consolidated-financial-statement-q2fy27.pdf"),
        ("Press release Q3 FY2026", "/pdfs/ITC-Press-Release-Q3-FY2026-financial-results.pdf"),
    ],
)
def test_real_results_are_still_financial_results(text, path):
    assert kind_of(text, "/investors" + path) == "financial_results"


def test_agm_is_a_whole_word_and_the_pattern_has_no_control_characters():
    from pathlib import Path

    import app.providers.company_ir as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert not [c for c in source if ord(c) < 32 and c not in "\n\r\t"]
    # "AGM" alone excludes a document; "agmatic" inside a longer word must not
    assert kind_of("AGM results", "/investors/agm.pdf") != "financial_results"
    assert kind_of("Quarterly results", "/investors/pragmatic-quarterly-results.pdf") == (
        "financial_results"
    )


def test_ocr_unit_line_with_a_misread_rupee_sign():
    from app.docintel.detect import find_units

    assert find_units("Particulars\nf crore \nQuarter ended", ocr=True) == {"crore"}
    assert find_units("x\n? lakh\n", ocr=True) == {"lakh"}
    # born-digital text is never given this latitude, and a normal sentence does not match
    assert find_units("Particulars\nf crore \n", ocr=False) == set()
    assert find_units("net profit rose by 2 crore", ocr=True) == set()
    assert find_units("The company earned crore\n", ocr=True) == set()


# ---- inbox: documents are typed by their content, never refused for a hard-to-read name ---------


from app.docintel.inbox import classify_content, guess_doc_type  # noqa: E402

TCS_RESULTS = [
    "Year ended\nJune 30,\nRevenue from operations 72,275\nOther income 1,568\nTOTAL INCOME 73,843\n"
    "Profit before tax 17,000\nEarnings per equity share",
    "SEGMENT REVENUE\nBanking 27,990",
    "Select explanatory notes to the Statement of Audited Consolidated Interim Financial Results",
]
BALANCE_SHEET = ["1\n(` crore)\nASSETS\nTotal assets 99\nEquity share capital 362\nBalance Sheet"]
PRESS_RELEASE = [
    "Information Classification :Public\nPress Release\nMaruti Suzuki Annual Integrated Report "
    "2025-26\nNew Delhi, 8 August 2026: Maruti Suzuki has released its Annual Integrated Report"
]
MARKET_REPORT = ["The AI\nImpact\nImperatives,\n2026\nAn HCLTech enterprise AI\nmarket report"]


def test_statements_without_a_title_page_are_results_whatever_the_file_is_called():
    assert guess_doc_type("Consolidated and Standalone.pdf", TCS_RESULTS, 5) == "quarterly_result"
    assert guess_doc_type("Standalone.pdf", BALANCE_SHEET, 57) == "quarterly_result"
    assert classify_content(BALANCE_SHEET, 400) == "annual_report"  # long and statement-heavy


def test_a_short_press_release_is_not_an_annual_report_despite_its_file_name():
    name = "Press Release -Maruti Suzuki Annual Integrated Report 2025-26.pdf"
    assert guess_doc_type(name, PRESS_RELEASE, 2) == "press_release"
    # a long document with the same words really is a report
    assert guess_doc_type(name, PRESS_RELEASE, 300) == "annual_report"


def test_an_unrecognised_document_is_stored_as_other_not_refused():
    assert guess_doc_type("the-ai-impact-imperatives-2026.pdf", MARKET_REPORT, 40) == "other"
    assert guess_doc_type("x.pdf", [], None) == "other"


def test_the_name_still_helps_when_the_content_says_nothing():
    assert guess_doc_type("AR-2025-26.pdf", [""], 300) == "annual_report"
    assert guess_doc_type("Q2-results.pdf", [], None) == "quarterly_result"
    assert classify_content([""], 10) is None
