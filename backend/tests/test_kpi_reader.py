"""Key-figure reader for press releases, key-parameter sheets and presentations (Phase 11)."""

from datetime import date
from decimal import Decimal as D

import pytest

from app.docintel import kpi
from app.docintel.pdf import read_pdf
from tests.test_research_vault import make_pdf

pymupdf = pytest.importorskip("pymupdf")


def page(tmp_path, lines):
    path = tmp_path / "p.pdf"
    path.write_bytes(make_pdf(lines))
    return read_pdf(path, use_ocr=False).pages[0]


def table(y0, header, rows, label_x=40, xs=(200, 250, 300, 350, 400)):
    """Lines for a header row and data rows at fixed columns."""
    out = [(label_x, y0, header[0])]
    out += [(xs[i], y0, h) for i, h in enumerate(header[1:])]
    for n, (label, *values) in enumerate(rows, start=1):
        y = y0 + 14 * n
        out.append((label_x, y, label))
        out += [(xs[i], y, v) for i, v in enumerate(values) if v]
    return out


def facts(blocks):
    out = {}
    for b in blocks:
        for f in b.facts:
            c = b.column(f.column_index)
            out[(f.key, f.dimension or "", c.period.period_end, c.period.duration_months)] = f.value
    return out


# ---- tables ---------------------------------------------------------------------------------------


def test_a_quarterly_ratio_table_is_read_with_its_periods(tmp_path):
    lines = table(
        100,
        ["Ratios (%)", "Q2FY26", "Q1FY27", "Q2FY27", "QoQ bps"],
        [
            ("GNPA", "1.73", "1.47", "1.39", "-8"),
            ("NNPA", "0.42", "0.38", "0.36", "-2"),
            ("CRAR", "14.62", "15.67", "16.09", "42"),
            ("CASA Ratio (%)", "39.63", "39.24", "38.32", "-92"),
        ],
    )
    p = page(tmp_path, lines)
    found = facts(kpi.finalise(kpi.extract_page_kpis(1, p.words, p.text)))
    q2 = date(2026, 9, 30)
    assert found[("gross_npa_ratio", "", q2, 3)] == D("1.39")
    assert found[("net_npa_ratio", "", q2, 3)] == D("0.36")
    assert found[("capital_adequacy_ratio", "", q2, 3)] == D("16.09")
    assert found[("casa_ratio", "", q2, 3)] == D("38.32")
    assert found[("gross_npa_ratio", "", date(2025, 9, 30), 3)] == D("1.73")  # prior-year column


def test_month_year_headers_are_point_in_time_and_amounts_carry_their_unit(tmp_path):
    lines = [(40, 90, "₹ in Crores")] + table(
        100,
        ["Balance Sheet", "Sep 25", "Jun 26", "Sep 26"],
        [
            ("Gross advances", "44,19,674", "50,47,222", "52,08,344"),
            ("Deposits", "55,91,700", "60,05,805", "62,77,919"),
        ],
    )
    p = page(tmp_path, lines)
    blocks = kpi.finalise(kpi.extract_page_kpis(1, p.words, p.text))
    assert blocks[0].unit == "crore"
    found = facts(blocks)
    sep26 = date(2026, 9, 30)
    assert found[("gross_advances", "", sep26, None)] == D("5208344")
    assert found[("total_deposits", "", sep26, None)] == D("6277919")
    assert blocks[0].column(2).period.point_in_time is True


def test_two_measures_of_nim_are_kept_apart_by_dimension(tmp_path):
    lines = table(
        100,
        ["HDFC BANK", "30-Sep-25", "30-Jun-26", "30-Sep-26"],
        [
            ("Net Interest Margin - assets (%)", "3.3", "3.3", "3.2"),
            ("Net Interest Margin - interest earning assets (%)", "3.4", "3.4", "3.3"),
            ("NIM, % (Domestic)", "3.08", "3.00", "3.11"),
        ],
        xs=(330, 390, 450),
    )
    p = page(tmp_path, lines)
    found = facts(kpi.finalise(kpi.extract_page_kpis(1, p.words, p.text)))
    d = date(2026, 9, 30)
    assert found[("net_interest_margin", "total_assets", d, None)] == D("3.2")
    assert found[("net_interest_margin", "interest_earning_assets", d, None)] == D("3.3")
    assert found[("net_interest_margin", "domestic", d, None)] == D("3.11")


def test_sub_portfolio_rows_and_different_measures_are_ignored(tmp_path):
    lines = table(
        100,
        ["Metrics", "30-Sep-25", "30-Jun-26", "30-Sep-26"],
        [
            ("Retail advances GNPA", "1.23%", "1.18%", "1.08%"),
            ("Total GNPA as a ratio of customer assets", "1.22%", "1.15%", "1.10%"),
            ("CASA deposits", "7.4%", "9.4%", "10.8%"),  # growth of CASA deposits, not the ratio
            ("Total GNPA as a ratio of gross advances", "1.24%", "1.17%", "1.11%"),
            ("CASA ratio", "33.9%", "32.3%", "31.6%"),
        ],
        xs=(330, 390, 450),
    )
    p = page(tmp_path, lines)
    found = facts(kpi.finalise(kpi.extract_page_kpis(1, p.words, p.text)))
    d = date(2026, 9, 30)
    assert found[("gross_npa_ratio", "", d, None)] == D("1.11")
    assert found[("casa_ratio", "", d, None)] == D("31.6")
    assert len({k[0] for k in found}) == 2  # nothing else slipped in


def test_a_row_with_one_number_under_many_headers_is_skipped_not_guessed(tmp_path):
    lines = [(40, 90, "₹ in Crores")] + table(
        100,
        ["Key metrics", "Q2FY27", "Q1FY27", "H1FY27"],
        [("Total Deposits", "14,52,066", "", ""), ("GNPA", "1.16", "1.20", "1.16")],
    )
    p = page(tmp_path, lines)
    blocks = kpi.extract_page_kpis(1, p.words, p.text)
    found = facts(blocks)
    assert not any(k[0] == "total_deposits" for k in found)
    assert any("KPI_ROW_SPARSE" == i["code"] for b in blocks for i in b.issues)
    assert any(k[0] == "gross_npa_ratio" for k in found)


def test_a_percentage_under_an_amount_label_is_a_growth_figure_not_the_amount(tmp_path):
    lines = table(
        100,
        ["Metrics", "Q2FY26", "Q1FY27", "Q2FY27"],
        [("Total Deposits", "21%", "6%", "23%")],
    )
    p = page(tmp_path, lines)
    assert not facts(kpi.extract_page_kpis(1, p.words, p.text))


def test_implausible_and_inconsistent_ratios_are_dropped_with_a_reason(tmp_path):
    lines = table(
        100,
        ["Ratios (%)", "Q2FY26", "Q1FY27", "Q2FY27"],
        [
            ("GNPA", "1.30", "1.10", "1.00"),
            ("NNPA", "0.40", "0.30", "1.20"),
            ("CASA Ratio (%)", "140.0", "39.0", "38.0"),
        ],
    )
    p = page(tmp_path, lines)
    blocks = kpi.finalise(kpi.extract_page_kpis(1, p.words, p.text))
    found = facts(blocks)
    q2 = date(2026, 9, 30)
    assert ("gross_npa_ratio", "", q2, 3) not in found  # net exceeds gross: neither is trusted
    assert ("net_npa_ratio", "", q2, 3) not in found
    assert ("casa_ratio", "", date(2025, 9, 30), 3) not in found  # 140 % is not a ratio
    assert found[("casa_ratio", "", q2, 3)] == D("38.0")
    assert any(i["code"] == "KPI_NPA_ORDER" for b in blocks for i in b.issues)


def test_a_gross_npa_ratio_that_disagrees_with_the_printed_amounts_is_dropped(tmp_path):
    t1 = table(
        100,
        ["Balance", "Sep 25", "Jun 26", "Sep 26"],
        [("Gross advances", "1,000", "1,000", "1,000"), ("GNPA", "100", "100", "100")],
    )
    t2 = table(
        200, ["Ratios (%)", "Q2FY26", "Q1FY27", "Q2FY27"], [("GNPA", "10.00", "10.00", "5.00")]
    )
    p = page(tmp_path, [(40, 80, "₹ in Crores"), *t1, *t2])
    blocks = kpi.finalise(kpi.extract_page_kpis(1, p.words, p.text))
    found = facts(blocks)
    assert ("gross_npa_ratio", "", date(2026, 9, 30), 3) not in found  # 100/1000 = 10 %, not 5 %
    assert any(i["code"] == "KPI_NPA_RECOMPUTE" for b in blocks for i in b.issues)


def test_amounts_without_a_stated_unit_are_not_read(tmp_path):
    lines = table(
        100, ["Balance", "Sep 25", "Jun 26", "Sep 26"], [("Deposits", "1,000", "1,100", "1,200")]
    )
    p = page(tmp_path, lines)
    blocks = kpi.extract_page_kpis(1, p.words, p.text)
    assert not facts(blocks)


# ---- sentences ------------------------------------------------------------------------------------


def test_sentences_with_an_explicit_as_of_date_are_read(tmp_path):
    text = [
        (
            40,
            80,
            "Total EOP Deposits were at Rs. 33,274 billion as of September 30, 2026, an increase of 18.8%.",
        ),
        (40, 94, "Gross advances were at Rs. 32,195 billion as of September 30, 2026."),
        (
            40,
            108,
            "The Bank's total Capital Adequacy Ratio (CAR) as per Basel III guidelines was at 18.9% as on September 30, 2026.",
        ),
        (40, 122, "Gross non-"),
        (40, 134, "performing assets were at 1.11% of gross advances as on September 30, 2026."),
    ]
    p = page(tmp_path, text)
    found = facts(kpi.extract_page_kpis(1, p.words, p.text))
    d = date(2026, 9, 30)
    assert found[("total_deposits", "", d, None)] == D("33274")
    assert found[("gross_advances", "", d, None)] == D("32195")
    assert found[("capital_adequacy_ratio", "", d, None)] == D("18.9")
    assert found[("gross_npa_ratio", "", d, None)] == D("1.11")  # across a line break


def test_a_sentence_without_a_date_is_not_read(tmp_path):
    p = page(
        tmp_path, [(40, 80, "Gross advances were at Rs. 32,195 billion, an increase of 16.3%.")]
    )
    assert not facts(kpi.extract_page_kpis(1, p.words, p.text))


def test_slide_bullets_take_their_period_from_a_heading_that_names_one_quarter(tmp_path):
    p = page(
        tmp_path,
        [
            (40, 40, "Major highlights for Q2FY27"),
            (40, 80, "GNPA at 1.16% down 30 bps YOY, NNPA at 0.35% down 9 bps YOY"),
            (40, 94, "Overall capital adequacy ratio (CAR) stood at 16.27%"),
        ],
    )
    found = facts(kpi.extract_page_kpis(1, p.words, p.text))
    q2 = date(2026, 9, 30)
    assert found[("gross_npa_ratio", "", q2, 3)] == D("1.16")
    assert found[("net_npa_ratio", "", q2, 3)] == D("0.35")
    assert found[("capital_adequacy_ratio", "", q2, 3)] == D("16.27")


def test_a_slide_that_names_two_quarters_or_two_values_is_left_alone(tmp_path):
    two_quarters = page(tmp_path, [(40, 40, "Q2FY27 versus Q1FY27"), (40, 80, "GNPA at 1.16%")])
    assert not facts(kpi.extract_page_kpis(1, two_quarters.words, two_quarters.text))
    two_values = page(
        tmp_path, [(40, 40, "Highlights Q2FY27"), (40, 80, "GNPA at 1.16% and GNPA at 1.50%")]
    )
    assert not facts(kpi.extract_page_kpis(1, two_values.words, two_values.text))


# ---- label matching -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "key", "dimension"),
    [
        ("NIM, % (Domestic)", "net_interest_margin", "domestic"),
        ("Net Interest Margin (NIM)", "net_interest_margin", ""),
        ("Whole Bank NIM", "net_interest_margin", "whole_bank"),
        ("Gross NPA Ratio", "gross_npa_ratio", ""),
        ("Net NPA ratio (%)", "net_npa_ratio", ""),
        ("Capital to Risk Weighted Assets Ratio (CRAR)", "capital_adequacy_ratio", ""),
        ("CASA Ratio (%)", "casa_ratio", ""),
        ("Gross advances", "gross_advances", ""),
        ("Total EOP Deposits", "total_deposits", ""),
    ],
)
def test_known_labels_match(label, key, dimension):
    kp, dim = kpi.match_kpi(label)
    assert kp.key == key and dim == dimension


@pytest.mark.parametrize(
    "label",
    ["Retail advances", "Tier-1 ratio", "CET-1 ratio", "PCR (with AUCA)", "Domestic CASA",
     "Credit costs as a % of advances", "Growth in deposits (YoY)", "CASA deposits"],
)  # fmt: skip
def test_other_labels_are_never_taken_for_a_key_figure(label):
    assert kpi.match_kpi(label) is None
