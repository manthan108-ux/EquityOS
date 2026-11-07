"""Phase 5: document intelligence (layout, periods, units, line items, checks, pipeline)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.docintel import detect, taxonomy, validate
from app.docintel.layout import (
    Line,
    Word,
    find_columns,
    group_lines,
    merge_split_numbers,
    parse_cell,
    split_row,
)
from app.docintel.pdf import PageData, PdfContent
from app.docintel.periods import parse_column_header
from app.docintel.pipeline import PROVIDER_CODE, extract_document
from app.docintel.statements import extract_page_statements
from app.models import (
    Basis,
    Company,
    DatasetVerification,
    Document,
    DocumentExtraction,
    DocumentPage,
    ExtractedFact,
    ExtractionStatus,
    FinancialFact,
    PeriodType,
    Statement,
    ValidationStatus,
)
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.docintel_pages import INCOME_ROWS, Page, balance_sheet_page, income_page, words

# ------------------------------------------------------------------------------- periods


@pytest.mark.parametrize(
    ("header", "end", "months", "audited"),
    [
        ("Quarter ended 30.09.2026 (Unaudited)", date(2026, 9, 30), 3, False),
        ("Three months ended September 30, 2026", date(2026, 9, 30), 3, None),
        ("Six months ended 30-Sep-26 Unaudited", date(2026, 9, 30), 6, False),
        ("Nine months ended 31/12/2025", date(2025, 12, 31), 9, None),
        ("Year ended 31.03.2026 (Audited)", date(2026, 3, 31), 12, True),
        ("Year ended 31st March, 2026", date(2026, 3, 31), 12, None),
        (
            "Corresponding 3 months ended 30.09.2025 in the previous year",
            date(2025, 9, 30),
            3,
            None,
        ),
        ("Q2 FY2027", date(2026, 9, 30), 3, None),
        ("Q4FY26", date(2026, 3, 31), 3, None),
        ("H1 FY27", date(2026, 9, 30), 6, None),
        ("9M FY2026", date(2025, 12, 31), 9, None),
        ("FY2026", date(2026, 3, 31), 12, None),
        ("FY 2025-26", date(2026, 3, 31), 12, None),
        ("Year ended 2025-26", date(2026, 3, 31), 12, None),
        ("Reviewed quarter ended Jun 30, 2026", date(2026, 6, 30), 3, False),
        # ICICI Bank style: date split over lines plus a fiscal label without "FY"
        (
            "Three months ended September 30, 2026 (Q2-2027) (Unaudited)",
            date(2026, 9, 30),
            3,
            False,
        ),
        ("Period ended September 30, 2026 (H1-2027) (Unaudited)", date(2026, 9, 30), 6, False),
        ("Period ended March 31, 2026 (FY2026) (Audited)", date(2026, 3, 31), 12, True),
        ("Period ended June 30, 2027 (Q2-2027)", date(2027, 6, 30), 3, None),  # calendar reading
    ],
)
def test_column_periods_are_read_from_real_header_styles(header, end, months, audited):
    period = parse_column_header(header)
    assert period.ok, period.problem
    assert (period.period_end, period.duration_months, period.audited) == (end, months, audited)


def test_balance_sheet_headers_are_points_in_time():
    for header in ("As at March 31, 2026", "As at 31.03.2026", "At 31 March 2026"):
        period = parse_column_header(header)
        assert period.point_in_time and period.period_end == date(2026, 3, 31), header
        assert period.duration_months is None


@pytest.mark.parametrize(
    ("header", "problem"),
    [
        ("Quarter ended", "no date in header"),
        ("30.09.2026", "duration not stated"),
        ("Quarter ended 30.09.2026 31.03.2026", "more than one date in header"),
        ("Quarter ended 15.09.2026", "not a month end"),
        ("Three months ended / Year ended 31.03.2026", "more than one duration in header"),
        ("Q2 2027", "no date in header"),  # without "FY" this could be a calendar quarter
        ("FY2025-27", "no date in header"),  # not consecutive years
        ("Dividend declared 12", "no date in header"),  # 'declared' is not December
        ("Quarter ended June 30", "no date in header"),  # no year: never June 2030
        ("Period ended September 30, 2026", "duration not stated"),
        ("Period ended September 30, 2026 (Q3-2027)", "duration not stated"),  # label disagrees
        ("Quarter ended September 30, 2026 (H1-2027)", "contradict"),
    ],
)
def test_unreadable_periods_are_refused_not_guessed(header, problem):
    period = parse_column_header(header)
    assert not period.ok
    assert problem in (period.problem or "")


# ------------------------------------------------------------------------------- detection


@pytest.mark.parametrize(
    ("text", "unit"),
    [
        ("(₹ in crore)", "crore"),
        ("(Rs. in Lakhs)", "lakh"),
        ("(` in crore)", "crore"),  # rupee glyph extracted as a backtick
        ("All amounts are in ₹ million unless otherwise stated", "million"),
        ("Amounts in INR Crores", "crore"),
        ("(₹ '000)", "thousand"),
        ("in ₹ billion", "billion"),
    ],
)
def test_units_are_recognised(text, unit):
    assert detect.find_units(text) == {unit}


def test_prose_amounts_are_not_units():
    assert detect.find_units("The Board declared a dividend of ₹ 10 per share") == set()
    assert detect.find_units("a loss of ₹ 500 crore was recognised") == set()


def test_basis_and_titles():
    assert detect.find_basis("Statement of Standalone Audited Results") == {"STANDALONE"}
    assert detect.find_basis("CONSOLIDATED BALANCE SHEET") == {"CONSOLIDATED"}
    assert (
        detect.classify_title("Consolidated Balance Sheet as at March 31, 2026") == "BALANCE_SHEET"
    )
    assert detect.classify_title("Statement of Assets and Liabilities") == "BALANCE_SHEET"
    assert detect.classify_title("Standalone Statement of Cash Flows") == "CASH_FLOW"
    assert (
        detect.classify_title("Statement of Profit and Loss for the year ended 31 March 2026")
        == "INCOME"
    )
    assert (
        detect.classify_title("Audited Financial Results for the quarter and year ended")
        == "INCOME"
    )
    # prose and segment tables are not statement titles
    assert (
        detect.classify_title("The above financial results were reviewed by the Audit Committee")
        is None
    )
    assert detect.classify_title("Segment wise financial results") is None
    assert detect.classify_title("Notes to the Balance Sheet") is None


# ------------------------------------------------------------------------------- taxonomy


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("I. Revenue from operations", "revenue_from_operations"),
        ("Total income (I+II)", "total_income"),
        ("Interest earned (a)+(b)+(c)+(d)", "interest_earned"),
        ("Profit/(loss) before tax (V-VI)", "profit_before_tax"),
        ("(Loss)/profit for the period", "net_profit"),
        (
            "Net Profit / (Loss) from ordinary activities after tax",
            "net_profit_from_ordinary_activities",
        ),
        ("13 Tax expense (j)+(k)", "tax_expense"),
        ("j) Current tax", "current_tax"),
        ("Less: Share of profit of minority shareholders", "minority_share_of_profit"),
        ("Add: Share of profit in associates", "share_of_profit_of_associates_and_jvs"),
        ("Proﬁt before tax", "profit_before_tax"),  # ligature as printed in the PDF
        ("Provisions (other than tax) and contingencies", "provisions_excluding_tax"),
        # bank annual accounts: includes tax, so it is a DIFFERENT item
        ("Provisions and contingencies (refer note 18.44)", "provisions_and_contingencies"),
        ("Interest earned 13", "interest_earned"),  # trailing schedule number
        (
            "Operating Profit before provisions and contingencies",
            "operating_profit_before_provisions",
        ),
        ("Depreciation, depletion and amortisation expense", "depreciation_and_amortisation"),
        ("(a) Basic (₹)", "eps_basic"),
        ("Diluted (not annualised)", "eps_diluted"),
        ("Paid-up equity share capital (Face value ₹ 2 each)", "paid_up_equity_share_capital"),
        ("Total Comprehensive Income for the period", "total_comprehensive_income"),
        ("Employee benefits expense", "employee_benefits_expense"),
        ("Payments to and provisions for employees", "employee_benefits_expense"),
    ],
)
def test_income_labels_map_to_canonical_items(label, key):
    assert taxonomy.match_label(label, taxonomy.INCOME, taxonomy.Context()) == key


@pytest.mark.parametrize(
    ("label", "key"),
    [
        (
            "Net cash (used in)/generated from operating activities (A)",
            "net_cash_from_operating_activities",
        ),
        ("Net cash used in investing activities", "net_cash_from_investing_activities"),
        ("Net cash flow from financing activities (C)", "net_cash_from_financing_activities"),
        ("Net increase/(decrease) in cash and cash equivalents (A+B+C)", "net_change_in_cash"),
        ("Cash and cash equivalents at the beginning of the year", "cash_at_beginning"),
        ("Cash and cash equivalents at the end of the year (refer note 12)", "cash_at_end"),
    ],
)
def test_cash_flow_labels(label, key):
    assert taxonomy.match_label(label, taxonomy.CASH_FLOW, taxonomy.Context()) == key


def test_unknown_labels_stay_unmatched_and_statement_types_do_not_leak():
    ctx = taxonomy.Context()
    assert (
        taxonomy.match_label("Gain on sale of a subsidiary's brand", taxonomy.INCOME, ctx) is None
    )
    assert taxonomy.match_label("Revenue from operations", taxonomy.BALANCE_SHEET, ctx) is None
    assert taxonomy.match_label("Total assets", taxonomy.INCOME, ctx) is None


def test_balance_sheet_sections_qualify_ambiguous_items():
    ctx = taxonomy.Context()
    bs = taxonomy.BALANCE_SHEET
    assert taxonomy.match_label("Borrowings", bs, ctx) == "borrowings"  # banks: no sections
    taxonomy.observe_heading("Non-current liabilities", bs, ctx)
    assert taxonomy.match_label("Borrowings", bs, ctx) == "non_current_borrowings"
    taxonomy.observe_heading("Current liabilities", bs, ctx)
    assert taxonomy.match_label("(i) Borrowings", bs, ctx) == "current_borrowings"
    taxonomy.observe_heading("EQUITY AND LIABILITIES", bs, ctx)
    assert taxonomy.match_label("Borrowings", bs, ctx) == "borrowings"


def test_attribution_lines_follow_their_heading():
    ctx = taxonomy.Context()
    inc = taxonomy.INCOME
    assert taxonomy.match_label("Owners of the Company", inc, ctx) is None  # no anchor yet
    taxonomy.match_label("Net profit for the period", inc, ctx)
    taxonomy.match_label("Total comprehensive income for the period", inc, ctx)
    taxonomy.observe_heading("Profit attributable to:", inc, ctx)
    assert taxonomy.match_label("Owners of the Company", inc, ctx) == "net_profit_owners"
    taxonomy.observe_heading("Total comprehensive income attributable to:", inc, ctx)
    assert (
        taxonomy.match_label("Non-controlling interests", inc, ctx)
        == "total_comprehensive_income_non_controlling"
    )
    # an explicit prefix wins over context
    assert (
        taxonomy.match_label("Profit attributable to owners of the Company", inc, ctx)
        == "net_profit_owners"
    )


# ------------------------------------------------------------------------------- layout


def test_cells_parse_indian_formats_and_never_turn_nil_into_zero():
    assert parse_cell("1,23,456.78") == Decimal("123456.78")
    assert parse_cell("(1,234.50)") == Decimal("-1234.50")
    assert parse_cell("−1.5") == Decimal("-1.5")
    assert parse_cell("12.5%") == Decimal("12.5")
    for nil in ("-", "–", "—", "Nil", "NA", ""):
        assert parse_cell(nil) is None
    assert parse_cell("30.09.2026") is None


def test_lines_group_by_vertical_position_and_split_numbers_rejoin():
    ws = [Word(100, 10, 120, 18, "B"), Word(10, 10.6, 50, 18.6, "A"), Word(10, 30, 40, 38, "C")]
    lines = group_lines(ws)
    assert [ln.text for ln in lines] == ["A B", "C"]
    merged = merge_split_numbers(
        Line([Word(0, 0, 5, 8, "("), Word(6, 0, 40, 8, "1,234.5"), Word(41, 0, 45, 8, ")")])
    )
    assert [w.text for w in merged.words] == ["(1,234.5)"]


def test_serial_number_columns_are_not_value_columns():
    page = Page()
    for i, (label, value) in enumerate(
        [("Revenue", "100.00"), ("Other income", "5.00"), ("Total", "105.00"), ("Tax", "1.00")]
    ):
        page.raw(words(0, 40, str(i + 1)), words(0, 60, label), words(0, 400, value, right=True))
    columns = find_columns(group_lines(page.items))
    assert len(columns) == 1 and columns[0].x1 == 400


def test_prose_crossing_value_columns_is_not_a_row():
    page = (
        Page()
        .row("Revenue from operations", "1.00", "2.00", "3.00")
        .row("Other income", "1.00", "2.00", "3.00")
        .row("Total income", "2.00", "4.00", "6.00")
    )
    page.text(
        "The figures for the quarter ended 31 March are the balancing figures between audited numbers."
    )
    lines = group_lines(page.items)
    columns = find_columns(lines)
    assert len(columns) == 3
    assert split_row(lines[-1], columns, 0) is None


# ------------------------------------------------------------------------------- statements


def _block(items, statement=None):
    blocks = extract_page_statements(1, items)
    assert blocks, "no statement recognised"
    return blocks[0] if statement is None else next(b for b in blocks if b.statement == statement)


def test_income_statement_page_is_read_with_periods_unit_and_basis():
    block = _block(income_page())
    assert (block.statement, block.basis, block.unit) == ("INCOME", "CONSOLIDATED", "crore")
    periods = [(c.period.period_end, c.period.duration_months, c.usable) for c in block.columns]
    assert periods == [
        (date(2026, 9, 30), 3, True),
        (date(2025, 9, 30), 3, True),
        (date(2026, 3, 31), 12, True),
    ]
    assert block.value("revenue_from_operations", 0).value == Decimal("1000.00")
    assert block.value("exceptional_items", 2).value == Decimal("-50.00")
    assert block.value("exceptional_items", 0).value is None  # '-' is nil, not zero
    assert block.value("net_profit_owners", 2).value == Decimal("690.00")
    assert block.value("eps_diluted", 1).value == Decimal("17.40")
    assert block.unmatched_labels == []
    fact = block.value("total_income", 0)
    assert fact.raw_label == "Total income (I+II)" and fact.bbox is not None


def test_all_checks_pass_on_a_consistent_statement():
    block = _block(income_page())
    results = validate.run_checks(block)
    assert results and all(r.passed for r in results)
    assert all(validate.column_verified(results, c) for c in range(3))
    names = {r.name for r in results if r.column == 2}
    assert "profit before tax = profit before exceptional items + exceptional items" in names


def test_a_misread_number_fails_its_checks():
    rows = [
        r if r[0] != "Other income" else ("Other income", "50.00", "40.00", "300.00")
        for r in INCOME_ROWS
    ]
    block = _block(income_page(rows=rows))
    results = validate.run_checks(block)
    failed = [r for r in results if not r.passed]
    assert failed and {r.column for r in failed} == {2}
    statuses = validate.fact_statuses(block, results)
    assert statuses[("other_income", 2)][0] is ValidationStatus.FAILED
    assert statuses[("other_income", 0)][0] is ValidationStatus.PASSED
    assert statuses[("eps_basic", 0)][0] is ValidationStatus.UNCHECKED


def test_balance_sheet_with_note_column():
    block = _block(balance_sheet_page())
    assert (block.statement, block.basis, block.unit) == ("BALANCE_SHEET", "STANDALONE", "lakh")
    assert [c.is_note for c in block.columns] == [True, False, False]
    assert [c.period.period_end for c in block.columns[1:]] == [
        date(2026, 3, 31),
        date(2025, 3, 31),
    ]
    assert block.value("non_current_borrowings", 1).value == Decimal("1500.00")
    assert block.value("current_borrowings", 1).value == Decimal("600.00")
    assert block.value("non_current_investments", 1).value == Decimal("1000.00")
    assert not [f for f in block.facts if f.column_index == 0]  # note numbers are never values
    assert all(r.passed for r in validate.run_checks(block))


def test_unbalanced_balance_sheet_is_caught():
    block = _block(balance_sheet_page(total_equity_liab="8,400.00"))
    failed = {r.name for r in validate.run_checks(block) if not r.passed}
    assert "total assets = total equity and liabilities" in failed


def test_missing_unit_and_basis_are_reported_not_assumed():
    block = _block(
        income_page(title="Statement of Unaudited Financial Results for the quarter", unit=None)
    )
    assert block.unit is None and block.basis is None
    codes = {i["code"] for i in block.issues}
    assert {"UNIT_UNKNOWN", "BASIS_UNKNOWN"} <= codes


def test_columns_with_the_same_period_are_both_refused():
    block = _block(income_page(second_header=("30.09.2026", "30.09.2026", "31.03.2026")))
    assert [c.usable for c in block.columns] == [False, False, True]
    assert block.columns[0].problem == "two columns have the same period"


def test_a_page_without_statement_lines_yields_nothing():
    page = (
        Page()
        .text("Statement of Profit and Loss")
        .text("Directors' Report")
        .row("Number of meetings held", "4", "5", "6")
    )
    assert extract_page_statements(1, page.items) == []


# ------------------------------------------------------------------------------- pipeline (DB)


@pytest.fixture
def company(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


def _document(session, company, *, sha="a", published=None, doc_type="quarterly_result"):
    doc = Document(
        company_id=company.id if company else None,
        url=f"https://example.invalid/{sha}.pdf",
        title="results",
        doc_type=doc_type,
        content_type="application/pdf",
        sha256=(sha * 64)[:64],
        size_bytes=100,
        storage_path=f"documents/{sha}.pdf",
        published_at=published,
    )
    session.add(doc)
    session.flush()
    return doc


def _content(*pages):
    return PdfContent(
        page_count=len(pages),
        pages=[
            PageData(
                i + 1, "\n".join(ln.text for ln in group_lines(p)), p, 500, False, "text_layer"
            )
            for i, p in enumerate(pages)
        ],
    )


def test_pipeline_stages_everything_and_promotes_only_validated_quarter_and_year(
    session, company, tmp_path
):
    doc = _document(session, company)
    report = extract_document(
        session, doc, tmp_path, content=_content(income_page(), balance_sheet_page())
    )
    run = report.extraction
    assert run.status is ExtractionStatus.COMPLETED and run.statements_found == 2
    assert (
        session.scalar(
            select(func.count())
            .select_from(DocumentPage)
            .where(DocumentPage.extraction_id == run.id)
        )
        == 2
    )

    facts = session.scalars(
        select(FinancialFact).where(FinancialFact.provider_code == PROVIDER_CODE)
    ).all()
    assert facts and run.facts_promoted == len(facts)
    revenue_q = next(
        f
        for f in facts
        if f.line_item == "revenue_from_operations"
        and f.period_type is PeriodType.QUARTERLY
        and f.period_end == date(2026, 9, 30)
    )
    assert revenue_q.basis is Basis.CONSOLIDATED and revenue_q.statement is Statement.INCOME
    assert revenue_q.original_value == Decimal("1000.00") and revenue_q.original_unit == "crore"
    assert revenue_q.value_inr == Decimal("10000000000")
    assert revenue_q.document_id == doc.id and revenue_q.source_page == 1
    assert revenue_q.period_label == "Q2FY2027"
    eps = next(
        f for f in facts if f.line_item == "eps_basic" and f.period_type is PeriodType.ANNUAL
    )
    assert eps.original_unit == "inr_per_share" and eps.value_inr == Decimal("69.00")
    bs = next(
        f for f in facts if f.line_item == "total_assets" and f.period_end == date(2026, 3, 31)
    )
    assert bs.statement is Statement.BALANCE_SHEET and bs.basis is Basis.STANDALONE
    assert bs.period_type is PeriodType.ANNUAL and bs.value_inr == Decimal("850000000")
    # nil cells are staged but never promoted
    nil = session.scalars(
        select(ExtractedFact).where(
            ExtractedFact.extraction_id == run.id,
            ExtractedFact.canonical_item == "exceptional_items",
            ExtractedFact.period_end == date(2026, 9, 30),
        )
    ).one()
    assert nil.original_value is None and not nil.promoted and "blank" in nil.not_promoted_reason
    verified = session.scalars(
        select(DatasetVerification.dataset).where(
            DatasetVerification.provider_code == PROVIDER_CODE
        )
    ).all()
    assert {"INCOME_QUARTERLY", "INCOME_ANNUAL", "BALANCE_SHEET"} <= set(verified)


def test_failed_checks_and_unknown_basis_block_promotion(session, company, tmp_path):
    rows = [
        r if r[0] != "Other income" else ("Other income", "50.00", "40.00", "300.00")
        for r in INCOME_ROWS
    ]
    doc = _document(session, company)
    run = extract_document(
        session, doc, tmp_path, content=_content(income_page(rows=rows))
    ).extraction
    staged = session.scalars(
        select(ExtractedFact).where(ExtractedFact.extraction_id == run.id)
    ).all()
    year = [f for f in staged if f.column_index == 2]
    assert not any(f.promoted for f in year if f.validation_status is ValidationStatus.FAILED)
    # UNCHECKED values in a column with a failed check are not trusted either
    eps_year = next(f for f in year if f.canonical_item == "eps_basic")
    assert (
        not eps_year.promoted
        and eps_year.not_promoted_reason == "no arithmetic check verified this column"
    )
    assert any(f.promoted for f in staged if f.column_index == 0)
    assert any(i["code"] == "CHECK_FAILED" for i in run.issues)

    doc2 = _document(session, company, sha="b")
    run2 = extract_document(
        session,
        doc2,
        tmp_path,
        content=_content(
            income_page(title="Statement of Unaudited Financial Results for the quarter")
        ),
    ).extraction
    assert run2.facts_extracted > 0 and run2.facts_promoted == 0
    reasons = set(
        session.scalars(
            select(ExtractedFact.not_promoted_reason).where(ExtractedFact.extraction_id == run2.id)
        )
    )
    assert "consolidated/standalone basis not stated" in reasons


def test_six_month_columns_are_staged_but_not_promoted(session, company, tmp_path):
    doc = _document(session, company)
    page = income_page(
        second_header=("30.09.2026", "30.09.2025", "30.09.2026"),
        spanning=(("Three months ended", 400.0), ("Six months ended", 520.0)),
    )
    run = extract_document(session, doc, tmp_path, content=_content(page)).extraction
    half = session.scalars(
        select(ExtractedFact).where(
            ExtractedFact.extraction_id == run.id, ExtractedFact.column_index == 2
        )
    ).all()
    assert half and all(
        f.duration_months == 6 and not f.promoted for f in half if f.original_value is not None
    )
    assert "6-month period" in next(
        f.not_promoted_reason for f in half if f.original_value is not None
    )


def test_rerun_is_idempotent_and_newer_document_wins_restatements(session, company, tmp_path):
    old = _document(session, company, sha="c", published=date(2025, 10, 20))
    old_page = income_page(second_header=("30.09.2025", "30.09.2024", "31.03.2025"))
    extract_document(session, old, tmp_path, content=_content(old_page))
    count_after_old = session.scalar(
        select(func.count())
        .select_from(FinancialFact)
        .where(FinancialFact.provider_code == PROVIDER_CODE)
    )

    extract_document(session, old, tmp_path, content=_content(old_page))  # re-run: same facts
    assert (
        session.scalar(
            select(func.count())
            .select_from(FinancialFact)
            .where(FinancialFact.provider_code == PROVIDER_CODE)
        )
        == count_after_old
    )

    # The newer filing restates the 30.09.2025 quarter (other income 40 -> 45, totals adjusted).
    restated = []
    for label, *vals in INCOME_ROWS:
        if label == "Other income":
            vals = ["50.00", "45.00", "200.00"]
        elif label == "Total income (I+II)":
            vals = ["1,050.00", "945.00", "4,000.00"]
        elif label == "Total expenses":
            vals = ["750.00", "705.00", "3,000.00"]
        elif label == "(e) Other expenses":
            vals = ["200.00", "182.00", "810.00"]
        restated.append((label, *vals))
    new = _document(session, company, sha="d", published=date(2026, 10, 20))
    extract_document(session, new, tmp_path, content=_content(income_page(rows=restated)))
    q = session.scalars(
        select(FinancialFact).where(
            FinancialFact.provider_code == PROVIDER_CODE,
            FinancialFact.line_item == "other_income",
            FinancialFact.period_end == date(2025, 9, 30),
            FinancialFact.period_type == PeriodType.QUARTERLY,
        )
    ).one()
    assert q.original_value == Decimal("45.00") and q.document_id == new.id
    note = session.scalars(
        select(ExtractedFact.validation_notes).where(
            ExtractedFact.document_id == new.id,
            ExtractedFact.canonical_item == "other_income",
            ExtractedFact.period_end == date(2025, 9, 30),
        )
    ).one()
    assert any("restated" in n for n in note)

    # Re-running the OLD document afterwards must not overwrite the newer, restated value.
    extract_document(session, old, tmp_path, content=_content(old_page))
    session.refresh(q)
    assert q.original_value == Decimal("45.00")


def test_document_without_company_is_extracted_but_never_promoted(session, tmp_path):
    seed_reference_data(session)
    doc = _document(session, None, sha="e")
    run = extract_document(session, doc, tmp_path, content=_content(income_page())).extraction
    assert run.facts_extracted > 0 and run.facts_promoted == 0


def test_unreadable_inputs_are_recorded_not_raised(session, company, tmp_path):
    doc = _document(session, company, sha="f")
    run = extract_document(session, doc, tmp_path).extraction  # file does not exist
    assert run.status is ExtractionStatus.FAILED and run.error == "stored file is missing"

    html = _document(session, company, sha="g")
    html.content_type = "text/html"
    assert (
        extract_document(session, html, tmp_path).extraction.status is ExtractionStatus.UNSUPPORTED
    )

    scans = PdfContent(
        page_count=2,
        pages=[PageData(1, "", [], 0, True, "none"), PageData(2, "", [], 0, True, "none")],
    )
    run = extract_document(
        session, _document(session, company, sha="h"), tmp_path, content=scans
    ).extraction
    assert run.status is ExtractionStatus.NEEDS_OCR and run.pages_needing_ocr == 2
    assert any(i["code"] == "NO_TEXT_LAYER" for i in run.issues)


def test_paths_outside_the_data_directory_are_refused(session, company, tmp_path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4")
    doc = _document(session, company, sha="i")
    doc.storage_path = "../outside.pdf"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    run = extract_document(session, doc, data_dir).extraction
    assert run.status is ExtractionStatus.FAILED
    assert (
        session.scalar(
            select(func.count())
            .select_from(DocumentExtraction)
            .where(DocumentExtraction.document_id == doc.id)
        )
        == 1
    )


# ------------------------------------------------------------------------------- API


def test_document_api_shows_extraction_checks_and_page_text(session, company, tmp_path):
    from fastapi.testclient import TestClient

    from app.core.database import get_session
    from app.main import create_app

    doc = _document(session, company, sha="k")
    extract_document(session, doc, tmp_path, content=_content(income_page()))
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)

    listing = client.get(f"/api/companies/{company.id}/documents").json()
    assert listing[0]["latest_extraction"]["status"] == "COMPLETED"
    detail = client.get(f"/api/documents/{doc.id}/extraction").json()
    st = detail["statements"][0]
    assert st["statement"] == "INCOME" and st["unit"] == "crore"
    assert all(c["passed"] for c in st["checks"])
    assert "cell is blank, nil or not a number" in detail["not_promoted"]
    page = client.get(f"/api/documents/{doc.id}/pages/1").json()
    assert "Revenue" in page["text"]
    assert client.get(f"/api/documents/{doc.id}/pages/99").status_code == 404
    assert client.get(f"/api/documents/{uuid.uuid4()}/extraction").status_code == 404


def test_document_extraction_provider_reports_validated_periods(
    session, company, tmp_path, monkeypatch
):
    from app.docintel import provider as provider_module
    from app.providers.base import CompanyRef, Dataset, Outcome

    doc = _document(session, company, sha="l")
    extract_document(session, doc, tmp_path, content=_content(income_page()))
    monkeypatch.setattr(provider_module, "pending_documents", lambda *_: [])
    ref = CompanyRef(company.id, company.legal_name, company.common_name, None, None, None, None)
    p = provider_module.DocumentExtractionProvider()
    quarterly = p.acquire(session, ref, Dataset.INCOME_QUARTERLY)
    assert quarterly.outcome is Outcome.OK and quarterly.periods == ["Q2FY2027", "Q2FY2026"]
    assert p.acquire(session, ref, Dataset.CASH_FLOW).outcome is Outcome.NO_DATA
    assert p.acquire(session, ref, Dataset.SHAREHOLDING).outcome is Outcome.UNSUPPORTED


# ------------------------------------------------------------------------------- entity check


def test_subsidiary_statements_are_never_attributed_to_the_parent(session, company, tmp_path):
    doc = _document(session, company, sha="m")
    page = income_page(company="ICICI Prudential Asset Management Company Limited")
    run = extract_document(session, doc, tmp_path, content=_content(page)).extraction
    assert run.facts_extracted > 0 and run.facts_promoted == 0
    assert any(i["code"] == "ENTITY_UNCONFIRMED" for i in run.issues)
    reasons = set(
        session.scalars(
            select(ExtractedFact.not_promoted_reason).where(ExtractedFact.extraction_id == run.id)
        )
    )
    assert "entity not confirmed: the page header names another entity, not this company" in reasons


def test_entity_rules():
    from app.docintel.entity import check_entity

    names = {"reliance industries limited", "reliance industries", "reliance"}
    header = "RELIANCE INDUSTRIES LIMITED\nStatement of results"
    assert check_entity(header, "", names).how == "page header"
    review = "Statement of results\nsubject to limited review"
    assert (
        check_entity(review, "Reliance Industries Ltd. Annual Report", names).how
        == "document cover"
    )
    subsidiary = "Jio Platforms Limited\nBalance Sheet"
    assert not check_entity(subsidiary, "Reliance Industries Limited", names).confirmed
    assert not check_entity("Balance Sheet", "Some other cover", names).confirmed
    # A subsidiary's deck: its cover names the subsidiary, a disclaimer later names the parent.
    deck_cover = "Jio Financial Services Limited\nCorporate Office: Mumbai"
    later_pages = deck_cover + "\nSafe harbor ... Reliance Industries Limited is the promoter"
    check = check_entity("Profit and loss (Rs mn)", later_pages, names, first_page_text=deck_cover)
    assert not check.confirmed and check.how == "the document is issued by another entity"
    own_cover = "Reliance Industries Limited\nAnnual Report"
    assert check_entity("Balance Sheet", own_cover, names, first_page_text=own_cover).confirmed
    # whole words only: "reliance" inside another word is not a mention
    assert not check_entity("Balance Sheet", "Unreliance Corp report", {"reliance"}).confirmed


def test_ir_links_labelled_for_a_group_company_are_recognised():
    from app.providers.company_ir import names_group_entity

    names = {"icici bank limited", "icici bank"}
    assert (
        names_group_entity("ICICI Prudential AMC: Investor Presentation", names)
        == "ICICI Prudential AMC"
    )
    assert names_group_entity("ICICI Bank: Financial Results for Q2", names) is None
    assert names_group_entity("Q2 FY2027: Results", names) is None  # not a name
    assert (
        names_group_entity("Financial Results: September 2026", names) is None
    )  # different first word
    assert names_group_entity("Annual Report 2025-26", names) is None  # no prefix


# ------------------------------------------------------------------------------- real-world layouts


def test_bank_results_layout_split_dates_fiscal_labels_and_wrapped_labels():
    from tests.docintel_pages import bank_results_page

    block = _block(bank_results_page())
    assert (block.statement, block.basis, block.unit) == ("INCOME", "STANDALONE", "crore")
    assert [(c.period.period_end, c.period.duration_months) for c in block.columns] == [
        (date(2026, 9, 30), 3),
        (date(2025, 9, 30), 3),
        (date(2026, 3, 31), 12),
    ]
    assert [c.period.audited for c in block.columns] == [False, False, True]
    # three-line upper-case labels with serial numbers are joined, longest reading first
    assert block.value("total_expenditure_excl_provisions", 0).value == Decimal("80.00")
    assert block.value("operating_profit_before_provisions", 0).value == Decimal("40.00")
    assert block.value("provisions_excluding_tax", 0).value == Decimal("5.00")
    assert block.value("profit_before_tax", 2).value == Decimal("130.00")
    assert block.value("net_profit_from_ordinary_activities", 1).value == Decimal("23.00")
    assert block.value("exceptional_items", 0).value is None  # '..' is nil
    results = validate.run_checks(block)
    assert results and all(r.passed for r in results), [r.form for r in results if not r.passed]


def test_header_rows_of_years_and_date_fragments_are_not_data():
    assert parse_cell("30,") is None and parse_cell("2026") == Decimal("2026")
    from app.docintel.layout import Row
    from app.docintel.statements import _header_row

    assert _header_row(Row(0, "No. Particulars", {0: "30, 2026", 1: "2026"}))
    assert _header_row(Row(0, "", {0: "2026", 1: "2025"}))
    assert not _header_row(Row(0, "Interest earned", {0: "49,110.00"}))


def test_repeated_header_flags_split_per_column():
    from app.docintel.layout import Column
    from app.docintel.statements import _split_by_columns

    cols = [Column(0, 100, 140, 5), Column(1, 150, 190, 5)]
    merged = Line([Word(101, 0, 139, 8, "(Unaudited)"), Word(151, 0, 189, 8, "(Unaudited)")])
    assert [p.text for p in _split_by_columns(merged, cols)] == ["(Unaudited)", "(Unaudited)"]
    spanning = Line(
        [
            Word(110, 0, 135, 8, "Three"),
            Word(138, 0, 165, 8, "months"),
            Word(168, 0, 189, 8, "ended"),
        ]
    )
    assert len(_split_by_columns(spanning, cols)) == 1  # a spanning header is never cut


def test_statement_continuing_on_the_next_page_is_checked_as_one():
    from app.docintel.statements import merge_continuations
    from tests.docintel_pages import cash_flow_pages

    first, second = cash_flow_pages()
    blocks = extract_page_statements(1, first) + extract_page_statements(2, second)
    merged = merge_continuations(blocks)
    assert len(merged) == 1 and merged[0].continued_pages == [2]
    block = merged[0]
    assert block.value("net_change_in_cash", 0).page_number == 2
    assert block.value("net_cash_from_operating_activities", 0).page_number == 1
    names = {r.name for r in validate.run_checks(block) if r.passed}
    assert "net change in cash = operating + investing + financing" in names
    assert "closing cash = opening cash + net change" in names


def test_rows_with_amounts_and_footnote_sentences_are_not_titles():
    assert (
        detect.classify_title("Balance carried over to balance sheet 1,285,442,549 1,023,911,675")
        is None
    )
    assert (
        detect.classify_title(
            "The Schedules referred to above form an integral part of the Balance Sheet."
        )
        is None
    )
    assert detect.classify_title("CONSOLIDATED CASH FLOW STATEMENTS") == "CASH_FLOW"


def test_segment_tables_are_not_statements():
    page = income_page(
        title="Unaudited Consolidated Segment Information for the quarter ended 30 September 2026"
    )
    assert extract_page_statements(1, page) == []


def test_half_year_is_six_months_not_twelve():
    period = parse_column_header("Half Year Ended 30th Sep'26")
    assert (period.period_end, period.duration_months) == (date(2026, 9, 30), 6)


def test_confirm_only_sum_failure_never_discredits_values():
    rows = [
        r if r[0] != "(e) Other expenses" else ("(e) Other expenses", "210.00", "177.00", "810.00")
        for r in INCOME_ROWS
    ]
    block = _block(income_page(rows=rows))  # component lines no longer add up in column 0
    results = validate.run_checks(block)
    sums = [r for r in results if r.confirm_only and r.column == 0]
    assert sums and not sums[0].passed
    statuses = validate.fact_statuses(block, results)
    assert statuses[("total_expenses", 0)][0] is ValidationStatus.PASSED  # its real checks hold
    assert statuses[("employee_benefits_expense", 0)][0] is ValidationStatus.UNCHECKED
    assert validate.column_verified(results, 0)
    assert statuses[("employee_benefits_expense", 1)][0] is ValidationStatus.PASSED  # sum holds


def test_scanned_pages_promote_only_check_confirmed_values(session, company, tmp_path):
    doc = _document(session, company, sha="n")
    page = income_page()
    text = "\n".join(ln.text for ln in group_lines(page))
    content = PdfContent(page_count=1, pages=[PageData(1, text, page, 500, False, "embedded_ocr")])
    run = extract_document(session, doc, tmp_path, content=content).extraction
    assert any(i["code"] == "EMBEDDED_OCR_TEXT" for i in run.issues)
    staged = session.scalars(
        select(ExtractedFact).where(ExtractedFact.extraction_id == run.id)
    ).all()
    promoted = {f.validation_status for f in staged if f.promoted}
    assert promoted == {ValidationStatus.PASSED}
    eps = next(f for f in staged if f.canonical_item == "eps_basic" and f.column_index == 0)
    assert not eps.promoted and "scanned page" in eps.not_promoted_reason


def test_document_period_is_filled_from_its_own_statements_never_overwritten(
    session, company, tmp_path
):
    doc = _document(session, company, sha="o")
    assert doc.financial_period is None
    run = extract_document(session, doc, tmp_path, content=_content(income_page())).extraction
    assert doc.financial_period == "Q2FY2027"
    assert any(i["code"] == "PERIOD_FROM_CONTENT" for i in run.issues)
    other = _document(session, company, sha="p")
    other.financial_period = "Q1FY2027"  # set at ingestion: kept as is
    extract_document(session, other, tmp_path, content=_content(income_page()))
    assert other.financial_period == "Q1FY2027"
