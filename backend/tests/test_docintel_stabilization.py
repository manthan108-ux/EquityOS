"""Phase 5 stabilization: storage, association, periods, OCR safeguards, disclosures, runs."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.storage import StoragePathError, resolve_inside, storage_report
from app.docintel import disclosures as disc
from app.docintel import validate
from app.docintel.association import (
    IssuerIndex,
    check_issuer,
    clean_entity,
    load_group_map,
    relation_code,
    unlink_document,
)
from app.docintel.derive import PROVIDER_CODE as DERIVED
from app.docintel.derive import derive_quarters
from app.docintel.layout import Word
from app.docintel.ocr import (
    OcrWord,
    ambiguous_characters,
    cross_check,
    parse_tsv,
    repair_header,
    repair_month,
)
from app.docintel.pdf import PageData, PdfContent
from app.docintel.pipeline import extract_document
from app.docintel.provider import pending_documents
from app.docintel.statements import extract_page_statements, inherit_context
from app.models import (
    Basis,
    Company,
    Document,
    DocumentExtraction,
    DocumentLinkEvent,
    DocumentSection,
    ExtractedFact,
    ExtractionStatus,
    FinancialFact,
    GroupEntity,
    PeriodType,
    Statement,
)
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.docintel_pages import income_page
from tests.docintel_tables import (
    ratios_page,
    related_party_transactions_page,
    roster_page,
    schedule_page,
    segment_page,
    shareholding_page,
)


@pytest.fixture
def company(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


def _doc(session, company, sha="a", **extra):
    doc = Document(
        company_id=company.id if company else None,
        url=f"https://example.invalid/{sha}.pdf",
        title="t",
        doc_type="quarterly_result",
        content_type="application/pdf",
        sha256=(sha * 64)[:64],
        size_bytes=10,
        storage_path=f"documents/{sha}.pdf",
        **extra,
    )
    session.add(doc)
    session.flush()
    return doc


def _content(*pages, method="text_layer", **kw):
    from app.docintel.layout import group_lines

    return PdfContent(
        page_count=len(pages),
        pages=[
            PageData(i + 1, "\n".join(ln.text for ln in group_lines(p)), p, 500, False, method)
            for i, p in enumerate(pages)
        ],
        **kw,
    )


def _page_data(words, number=1, text=""):
    return PageData(number, text, words, 500, False, "text_layer")


# ------------------------------------------------------------------------------- storage


def test_paths_cannot_escape_the_data_directory(tmp_path):
    (tmp_path / "documents").mkdir()
    assert resolve_inside(tmp_path, "documents/x.pdf") == (tmp_path / "documents/x.pdf").resolve()
    for bad in ("../outside.pdf", "documents/../../outside.pdf", "/etc/passwd"):
        with pytest.raises(StoragePathError):
            resolve_inside(tmp_path, bad)


def test_storage_report_finds_missing_changed_and_unreferenced_files(session, company, tmp_path):
    import hashlib

    good, changed, missing = (_doc(session, company, sha=s) for s in "abc")
    (tmp_path / "documents").mkdir()
    content = b"%PDF-1.4 good"
    good.storage_path, good.sha256 = "documents/good.pdf", hashlib.sha256(content).hexdigest()
    (tmp_path / "documents/good.pdf").write_bytes(content)
    changed.storage_path = "documents/changed.pdf"
    (tmp_path / "documents/changed.pdf").write_bytes(b"%PDF-1.4 different")
    missing.storage_path = "documents/gone.pdf"
    (tmp_path / "documents/orphan.pdf").write_bytes(b"%PDF-1.4 orphan")
    report = storage_report(session, tmp_path, verify_hashes=True)
    assert report.documents == 3 and report.writable
    assert report.missing_files == [str(missing.id)]
    assert report.hash_mismatches == [str(changed.id)]
    assert report.unreferenced_files == 1 and not report.healthy


# ------------------------------------------------------------------------------- tables


def _checks(block):
    return validate.run_checks(block)


def test_segment_table_reads_dimensions_and_every_identity_holds():
    (block,) = extract_page_statements(1, segment_page())
    assert block.statement == "SEGMENT" and block.basis == "STANDALONE" and block.unit == "crore"
    rows = block.dimensional("segment_revenue", 0)
    assert [(f.dimension, f.value) for f in rows] == [
        ("Retail Banking", Decimal("100.00")),
        ("Treasury", Decimal("60.00")),
    ]
    assert block.value("segment_revenue_inter_segment", 0).value == Decimal("40.00")
    assert [f.dimension for f in block.dimensional("segment_assets", 0)][-1] == "Unallocated"
    results = _checks(block)
    assert results and all(r.passed for r in results)


def test_segment_total_that_does_not_add_up_is_caught_to_the_unit():
    (block,) = extract_page_statements(1, segment_page(assets_total="2,001.00"))
    failed = {r.name for r in _checks(block) if not r.passed}
    assert "segments add up to the total (segment_assets)" in failed
    statuses = validate.fact_statuses(block, _checks(block))
    assert statuses[("segment_assets", 0)][0].value == "FAILED"


def test_ratio_table_headed_above_its_title_inherits_basis_from_the_page_before():
    (income,) = extract_page_statements(1, income_page())
    (ratios,) = extract_page_statements(2, ratios_page())
    assert ratios.statement == "RATIOS" and ratios.basis is None
    assert any(c.usable for c in ratios.columns)  # headings were read above the title
    inherit_context([income, ratios])
    assert ratios.basis == "CONSOLIDATED" and "basis from page 1" in ratios.inherited
    # a wrapped label is joined with the line above it, and units follow the kind of value
    assert ratios.value("government_shareholding_pct", 0).value == Decimal("0.22")
    assert ratios.value("capital_adequacy_ratio", 1).value == Decimal("15.76")
    assert ratios.value("net_worth", 0).value == Decimal("349436.61")


def test_schedules_split_by_title_and_totals_are_checked():
    borrowings, other = extract_page_statements(1, schedule_page())
    assert borrowings.dimension == "SCHEDULE 4 - BORROWINGS"
    assert other.dimension == "SCHEDULE 5 - OTHER LIABILITIES"
    assert all(r.passed for r in _checks(borrowings)) and _checks(borrowings)
    # sub-items carry their heading; the nil '-' counts as nil in the sum, never as a number
    key = "borrowings_in_india__reserve_bank_of_india"
    assert borrowings.value(key, 0).value is None
    assert borrowings.value("total_borrowings", 1).value == Decimal("310")
    # the footnote sentence under the table did not become a value column
    assert len([c for c in borrowings.columns if c.usable]) == 2
    bad_borrowings, bad_other = extract_page_statements(1, schedule_page(other_total="720"))
    assert not all(r.passed for r in _checks(bad_other))
    assert all(r.passed for r in _checks(bad_borrowings))


def test_a_different_schedule_on_the_next_page_is_not_a_continuation():
    from app.docintel.statements import merge_continuations

    first = extract_page_statements(1, schedule_page())[0]
    second = extract_page_statements(2, schedule_page())[1]
    assert len(merge_continuations([first, second])) == 2


def test_shareholding_must_add_up_exactly():
    (good,) = disc.extract_shareholding(1, shareholding_page())
    assert good.facts and all(passed for _, passed, *_ in disc.shareholding_sums(good))
    assert "bodies_corporate_includes_government_companies_clearing_members_and_othe" in {
        f.key for f in good.facts
    }
    (bad,) = disc.extract_shareholding(1, shareholding_page(total="999"))
    sums = {col: passed for col, passed, *_ in disc.shareholding_sums(bad)}
    assert sums[0] is False  # one share out: share counts have no rounding slack
    assert sums[1] is True


def test_group_roster_reads_names_and_relationship_words_but_not_transaction_tables():
    rows = disc.find_group_entities([_page_data(roster_page())])
    assert [(r.entity_name, r.relationship) for r in rows] == [
        ("XYZ Asset Management Company Limited", "Subsidiary"),
        ("XYZ Home Finance Limited", "Subsidiary"),
        ("Arteria Technologies Private Limited", "Associate"),
        (
            "XYZ Pension Fund Management Limited (erstwhile XYZ Prudential Pension Funds "
            "Management Company Limited)",
            "Subsidiary",
        ),
        ("Comm Trade Services Limited", "Other related entity"),
    ]
    assert disc.find_group_entities([_page_data(related_party_transactions_page())]) == []


def _text_pages(*texts):
    return [PageData(i + 1, t, [], len(t), False, "text_layer") for i, t in enumerate(texts)]


def test_sections_are_found_by_heading_and_the_auditor_flags_quote_the_report():
    report = (
        "INDEPENDENT AUDITOR'S REPORT\nOpinion\nIn our opinion the statements give a true and "
        "fair view of the state of affairs.\nKey Audit Matters\nRevenue recognition\n"
    )
    pages = _text_pages(
        "Directors report text",
        "MANAGEMENT DISCUSSION & ANALYSIS\nThe economy grew.",
        report,
        "INDEPENDENT AUDITOR'S REPORT (Contd.)\nEmphasis of Matter\nA fire at a plant.\n",
        "BALANCE SHEET\nAs at March 31, 2026",
    )
    sections = disc.find_sections(pages)
    assert [(s.kind, s.start_page, s.end_page) for s in sections] == [
        ("MANAGEMENT_DISCUSSION", 2, 2),
        ("AUDITOR_REPORT", 3, 4),
    ]
    flags = sections[1].flags
    assert flags["opinion"] == "unmodified" and flags["key_audit_matters"] is True
    assert (
        flags["emphasis_of_matter"] is True
        and flags["going_concern_uncertainty_mentioned"] is False
    )
    assert flags["quotes"] and flags["quotes"][0].lower().startswith("in our opinion")


def test_a_qualified_opinion_is_flagged_only_from_its_explicit_heading():
    text = "Qualified Opinion\nBasis for Qualified Opinion\nWe could not verify inventory.\n"
    assert disc.auditor_flags(text)["opinion"] == "qualified"
    assert disc.auditor_flags("nothing relevant here")["opinion"] == "unknown"


# ------------------------------------------------------------------------------- pipeline


def test_disclosure_tables_are_stored_as_validated_disclosures_not_financial_facts(
    session, company, tmp_path
):
    doc = _doc(session, company, sha="d1")
    content = _content(income_page(), segment_page(), schedule_page(), shareholding_page())
    # the segment page is a different layout from the income page, so give it the same periods
    run = extract_document(session, doc, tmp_path, content=content).extraction
    assert run.status is ExtractionStatus.COMPLETED
    staged = session.scalars(
        select(ExtractedFact).where(ExtractedFact.extraction_id == run.id)
    ).all()
    kinds = {f.statement for f in staged}
    assert {"INCOME", "SEGMENT", "NOTE", "SHAREHOLDING"} <= kinds
    segment = [f for f in staged if f.statement == "SEGMENT" and f.dimension == "Retail Banking"]
    assert segment and all(f.promoted for f in segment if f.original_value is not None)
    holding = [
        f for f in staged if f.statement == "SHAREHOLDING" and f.canonical_item == "promoters"
    ]
    assert {f.original_unit for f in holding} == {"shares", "percent"} and all(
        f.promoted for f in holding
    )
    # none of them became a primary financial fact
    assert (
        session.scalar(
            select(func.count())
            .select_from(FinancialFact)
            .where(FinancialFact.line_item == "segment_revenue")
        )
        == 0
    )


def test_sections_and_group_entities_are_persisted_with_the_page(session, company, tmp_path):
    doc = _doc(session, company, sha="d2", association_status="CONFIRMED")
    pages = _content(income_page(), roster_page())
    pages.pages[0].text = "Reliance Industries Limited\n" + pages.pages[0].text
    pages.pages[1].text = "MANAGEMENT DISCUSSION & ANALYSIS\nText"
    run = extract_document(session, doc, tmp_path, content=pages).extraction
    entities = session.scalars(select(GroupEntity).where(GroupEntity.extraction_id == run.id)).all()
    assert {(e.entity_name, e.relationship, e.page_number) for e in entities} >= {
        ("XYZ Home Finance Limited", "Subsidiary", 2)
    }
    assert (
        session.scalar(
            select(func.count())
            .select_from(DocumentSection)
            .where(DocumentSection.extraction_id == run.id)
        )
        == 1
    )
    mismatched = _doc(session, company, sha="d3", association_status="MISMATCH")
    run2 = extract_document(
        session, mismatched, tmp_path, content=_content(roster_page())
    ).extraction
    assert not session.scalars(
        select(GroupEntity).where(GroupEntity.extraction_id == run2.id)
    ).all()


def test_rerun_supersedes_but_never_deletes_and_failures_are_recorded(
    session, company, tmp_path, monkeypatch
):
    doc = _doc(session, company, sha="d4")
    first = extract_document(session, doc, tmp_path, content=_content(income_page())).extraction
    second = extract_document(session, doc, tmp_path, content=_content(income_page())).extraction
    session.refresh(first)
    assert first.superseded_at is not None and second.superseded_at is None
    assert session.get(DocumentExtraction, first.id) is not None  # kept for the audit trail

    import app.docintel.pipeline as pipeline

    def boom(*_a, **_k):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(pipeline, "extract_page_statements", boom)
    failed = extract_document(session, doc, tmp_path, content=_content(income_page())).extraction
    assert (
        failed.status is ExtractionStatus.COMPLETED
        or failed.status is ExtractionStatus.NO_STATEMENTS
    )
    assert any(i["code"] == "PAGE_ERROR" for i in failed.issues)  # one bad page is isolated
    assert "secret internals" not in str(failed.issues)

    monkeypatch.setattr(pipeline, "_persist_narrative", boom)
    monkeypatch.setattr(pipeline, "inherit_context", boom)
    hard = extract_document(session, doc, tmp_path, content=_content(income_page())).extraction
    assert hard.status is ExtractionStatus.FAILED and hard.error.startswith(
        "unexpected RuntimeError"
    )
    session.refresh(second)  # a failed run does not supersede the last good one
    assert (
        session.scalar(
            select(func.count())
            .select_from(DocumentExtraction)
            .where(
                DocumentExtraction.document_id == doc.id, DocumentExtraction.superseded_at.is_(None)
            )
        )
        >= 1
    )


def test_pending_documents_retries_failures_and_skips_other_entities(session, company):
    done, failed, foreign = (_doc(session, company, sha=s) for s in ("e1", "e2", "e3"))
    foreign.association_status = "MISMATCH"
    for doc, status in ((done, ExtractionStatus.COMPLETED), (failed, ExtractionStatus.FAILED)):
        session.add(
            DocumentExtraction(
                document_id=doc.id,
                extractor_version="5.2",
                status=status,
                issues=[],
                finished_at=doc.retrieved_at,
            )
        )
    session.flush()
    assert [d.id for d in pending_documents(session, company.id)] == [failed.id]


# ------------------------------------------------------------------------------- OCR safeguards


def test_ocr_helpers():
    tsv = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n"
    tsv += (
        "5\t1\t1\t1\t1\t1\t300\t600\t150\t60\t96.5\t1,234.50\n5\t1\t1\t1\t1\t2\t0\t0\t5\t5\t-1\t\n"
    )
    (word,) = parse_tsv(tsv, points_per_pixel=72 / 300)
    assert (round(word.x0, 1), round(word.y0, 1), word.conf) == (72.0, 144.0, 96.5)
    assert [ambiguous_characters(t) for t in ("1O,5O0.00", "l,234", "1,234.50", "Total")] == [
        True,
        True,
        False,
        False,
    ]
    assert (
        repair_month("Seo") == "Sep" and repair_month("Sep") is None and repair_month("Xyz") is None
    )
    fixed, notes = repair_header("Quarter Ended 30% Seo'26")
    assert fixed == "Quarter Ended 30th Sep'26" and len(notes) == 2
    assert repair_header("15% Sep'26")[0] == "15th Sep'26"  # still refused later: not a month end


@pytest.mark.parametrize(
    ("value", "seen", "verdict"),
    [
        (Decimal(95754), "95.754", "AGREE"),
        (Decimal(29550), "29,550]", "AGREE"),
        (Decimal("12.5"), "12.5", "AGREE"),
        (Decimal(23196), "23,106", "DISAGREE"),
        (Decimal(-5428), "5,428", "DISAGREE"),  # a lost sign is a disagreement
        (Decimal("95.754"), "95,754", "DISAGREE"),  # a real decimal is not a thousands comma
    ],
)
def test_second_ocr_reading_must_agree(value, seen, verdict):
    box = (8, 8, 62, 20)
    assert cross_check(value, box, [OcrWord(10, 10, 60, 18, seen, 95.0)])[0] == verdict
    assert cross_check(value, box, [])[0] == "NOT_FOUND"


def _ocr_copy(words, change=None):
    """The same page 'read' a second time, optionally with one number changed."""
    out = []
    for w in words:
        text = change.get(w.text, w.text) if change else w.text
        out.append(OcrWord(w.x0, w.y0, w.x1, w.y1, text, 95.0))
    return out


def _embedded(words):
    return _content(words, method="embedded_ocr")


def test_scanned_values_need_a_check_and_an_agreeing_second_reading(session, company, tmp_path):
    words = income_page()
    clean = _embedded(words)
    clean.ocr_page = lambda _n: _ocr_copy(words)
    clean.ocr_engine = "tesseract test"
    run = extract_document(
        session, _doc(session, company, sha="o1"), tmp_path, content=clean
    ).extraction
    assert run.facts_promoted > 0 and run.ocr_engine == "tesseract test"
    confirmed = session.scalars(
        select(ExtractedFact).where(
            ExtractedFact.extraction_id == run.id, ExtractedFact.promoted.is_(True)
        )
    ).all()
    assert any(
        "independent second OCR reading" in " ".join(f.validation_notes or []) for f in confirmed
    )

    doubtful = _embedded(words)
    doubtful.ocr_page = lambda _n: _ocr_copy(words, {"1,000.00": "7,000.00"})
    run2 = extract_document(
        session, _doc(session, company, sha="o2"), tmp_path, content=doubtful
    ).extraction
    flagged = session.scalars(
        select(ExtractedFact).where(
            ExtractedFact.extraction_id == run2.id, ExtractedFact.needs_review.is_(True)
        )
    ).all()
    assert flagged and {f.original_value for f in flagged} == {Decimal("1000.00")}
    assert not any(f.promoted for f in flagged)
    assert not flagged[0].promoted and flagged[0].not_promoted_reason.startswith(
        "second OCR reading"
    )

    no_second_reading = _embedded(words)  # Tesseract unavailable: the check-only rule applies
    run3 = extract_document(
        session, _doc(session, company, sha="o3"), tmp_path, content=no_second_reading
    ).extraction
    staged = session.scalars(
        select(ExtractedFact).where(ExtractedFact.extraction_id == run3.id)
    ).all()
    assert not [f for f in staged if f.promoted and f.validation_status.value != "PASSED"]
    assert any(
        (f.not_promoted_reason or "").startswith("scanned page") for f in staged
    )  # unchecked values stay out


def test_low_confidence_and_lookalike_characters_are_flagged(session, company, tmp_path):
    words = income_page()
    scan = PdfContent(page_count=1, pages=[], ocr_engine="tesseract test")
    low = [
        OcrWord(w.x0, w.y0, w.x1, w.y1, w.text, 40.0 if w.text == "1,000.00" else 96.0)
        for w in words
    ]
    scan.pages = [PageData(1, "Reliance Industries Limited", low, 500, False, "ocr", 90.0)]
    run = extract_document(
        session, _doc(session, company, sha="o4"), tmp_path, content=scan
    ).extraction
    reasons = {
        f.not_promoted_reason
        for f in session.scalars(select(ExtractedFact).where(ExtractedFact.extraction_id == run.id))
        if f.needs_review
    }
    assert any(r and r.startswith("low OCR confidence") for r in reasons)
    look = [
        OcrWord(w.x0, w.y0, w.x1, w.y1, "1O0.00" if w.text == "1,000.00" else w.text, 96.0)
        for w in words
    ]
    scan.pages = [PageData(1, "Reliance Industries Limited", look, 500, False, "ocr", 95.0)]
    run2 = extract_document(
        session, _doc(session, company, sha="o5"), tmp_path, content=scan
    ).extraction
    flagged = session.scalars(
        select(ExtractedFact).where(
            ExtractedFact.extraction_id == run2.id, ExtractedFact.needs_review.is_(True)
        )
    ).all()
    assert any("look-alike" in (f.not_promoted_reason or "") for f in flagged)


# ------------------------------------------------------------------------------- derivation


def _fact(
    session,
    company,
    line,
    end,
    ptype,
    value,
    *,
    basis=Basis.CONSOLIDATED,
    unit="crore",
    doc=None,
    label="x",
):
    from app.normalization import normalized_value

    row = FinancialFact(
        company_id=company.id,
        provider_code="DOCUMENT_EXTRACTION",
        statement=Statement.INCOME,
        line_item=line,
        period_end=end,
        period_type=ptype,
        basis=basis,
        original_value=Decimal(value),
        original_unit=unit,
        period_label=label,
        value_inr=normalized_value(Decimal(value), unit),
        source_url="u",
        retrieved_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        document_id=doc.id if doc else None,
        source_page=1,
    )
    session.add(row)
    session.flush()
    return row


def test_standalone_quarters_are_derived_only_from_nested_cumulative_figures(session, company):
    q1, h1 = date(2026, 6, 30), date(2026, 9, 30)
    _fact(
        session,
        company,
        "revenue_from_operations",
        q1,
        PeriodType.QUARTERLY,
        "100",
        label="Q1FY2027",
    )
    _fact(
        session,
        company,
        "revenue_from_operations",
        h1,
        PeriodType.HALF_YEAR,
        "250",
        label="H1FY2027",
    )
    _fact(session, company, "eps_basic", q1, PeriodType.QUARTERLY, "1.0")
    _fact(session, company, "eps_basic", h1, PeriodType.HALF_YEAR, "2.5")
    # consolidated H1 must not be paired with a STANDALONE Q1
    _fact(session, company, "other_income", q1, PeriodType.QUARTERLY, "5", basis=Basis.STANDALONE)
    _fact(session, company, "other_income", h1, PeriodType.HALF_YEAR, "9")
    report = derive_quarters(session, company.id)
    outcomes = {(i.line_item, i.basis, i.outcome) for i in report.items}
    assert ("revenue_from_operations", Basis.CONSOLIDATED, "DERIVED") in outcomes
    assert not any(i.line_item == "eps_basic" for i in report.items)  # per-share is not additive
    assert ("other_income", Basis.CONSOLIDATED, "SKIPPED") in outcomes
    derived = session.scalar(select(FinancialFact).where(FinancialFact.provider_code == DERIVED))
    assert derived.period_label == "Q2FY2027" and derived.value_inr == Decimal(150) * 10**7
    assert (
        derived.original_unit == "inr" and derived.derived_from["minuend"]["period"] == "H1FY2027"
    )
    assert derive_quarters(session, company.id).count("DERIVED") == 1  # idempotent: one row
    assert (
        session.scalar(
            select(func.count())
            .select_from(FinancialFact)
            .where(FinancialFact.provider_code == DERIVED)
        )
        == 1
    )


def test_reported_quarters_are_never_overwritten_only_cross_checked(session, company):
    q1, q2, h1 = date(2026, 6, 30), date(2026, 9, 30), date(2026, 9, 30)
    _fact(session, company, "total_income", q1, PeriodType.QUARTERLY, "100.00")
    _fact(session, company, "total_income", q2, PeriodType.QUARTERLY, "150.00")
    _fact(session, company, "total_income", h1, PeriodType.HALF_YEAR, "250.00")
    _fact(session, company, "profit_before_tax", q1, PeriodType.QUARTERLY, "10")
    _fact(session, company, "profit_before_tax", q2, PeriodType.QUARTERLY, "20")
    _fact(session, company, "profit_before_tax", h1, PeriodType.HALF_YEAR, "40")  # 10+20 != 40
    report = derive_quarters(session, company.id)
    got = {i.line_item: i.outcome for i in report.items}
    assert got == {"total_income": "CONSISTENT", "profit_before_tax": "INCONSISTENT"}
    assert (
        session.scalar(
            select(func.count())
            .select_from(FinancialFact)
            .where(FinancialFact.provider_code == DERIVED)
        )
        == 0
    )


def test_q3_and_q4_follow_the_same_rule(session, company):
    _fact(session, company, "net_profit", date(2025, 9, 30), PeriodType.HALF_YEAR, "300")
    _fact(session, company, "net_profit", date(2025, 12, 31), PeriodType.NINE_MONTH, "480")
    _fact(session, company, "net_profit", date(2026, 3, 31), PeriodType.ANNUAL, "700")
    derive_quarters(session, company.id)
    rows = {
        r.period_label: r.value_inr
        for r in session.scalars(
            select(FinancialFact).where(FinancialFact.provider_code == DERIVED)
        )
    }
    assert rows == {"Q3FY2026": Decimal(180) * 10**7, "Q4FY2026": Decimal(220) * 10**7}


# ------------------------------------------------------------------------------- association


def _index(session, *companies):
    index = IssuerIndex()
    for c in companies:
        index.add(c.id, c.legal_name, c.legal_name)
    return index


def test_issuer_checks(session, company):
    other = session.scalar(select(Company).where(Company.common_name == "TCS"))
    index = _index(session, company, other)
    own = {"reliance industries limited", "reliance industries", "reliance"}
    page = lambda header: [header + "\n" + "text " * 60]  # noqa: E731
    assert (
        check_issuer(company.id, page("Reliance Industries Limited\nResults"), index, own).status
        == "CONFIRMED"
    )
    named = check_issuer(company.id, page("Tata Consultancy Services Limited\nResults"), index, own)
    assert (
        named.mismatch
        and named.relation == "SEPARATELY_LISTED"
        and named.other_company_id == other.id
    )
    # an exchange named as the addressee of a covering letter is not the issuer
    letter = "To,\nBSE Limited\nSir,\nReliance Industries Limited submits results"
    assert check_issuer(company.id, page(letter), index, own).status == "CONFIRMED"
    group = check_issuer(company.id, page("Reliance Retail Ventures Limited\nResults"), index, own)
    assert group.mismatch and group.relation == "POSSIBLE_GROUP_ENTITY"
    assert (
        check_issuer(company.id, page("Acme Widgets Limited\nResults"), index, own).relation
        == "OTHER_ENTITY"
    )
    assert check_issuer(company.id, [""], index, own).status == "UNVERIFIED"
    assert (
        clean_entity("com cin l17110mh1973plc019786 october 30 2026 bse limited") == "bse limited"
    )
    assert [
        relation_code(w)
        for w in (
            "Fellow Subsidiary",
            "Wholly-Owned Subsidiary",
            "Associate",
            "Other related entity",
        )
    ] == [
        "FELLOW_SUBSIDIARY",
        "SUBSIDIARY",
        "ASSOCIATE",
        "OTHER_RELATED_ENTITY",
    ]


def test_a_stated_relationship_from_the_companys_own_filing_is_reported(session, company):
    other = session.scalar(select(Company).where(Company.common_name == "TCS"))
    index = _index(session, company, other)
    doc = _doc(session, company, sha="g1")
    run = DocumentExtraction(
        document_id=doc.id, extractor_version="5.2", status=ExtractionStatus.COMPLETED, issues=[]
    )
    session.add(run)
    session.flush()
    session.add(
        GroupEntity(
            company_id=company.id,
            extraction_id=run.id,
            document_id=doc.id,
            page_number=228,
            entity_name=other.legal_name,
            normalized_name=other.legal_name.lower().replace("ltd.", "limited"),
            relationship="Subsidiary",
        )
    )
    session.flush()
    from app.core.text import normalize_text

    session.execute(
        GroupEntity.__table__.update().values(normalized_name=normalize_text(other.legal_name))
    )
    result = check_issuer(
        company.id,
        ["Tata Consultancy Services Limited\n" + "text " * 60],
        index,
        {"reliance industries"},
        load_group_map(session, company.id),
    )
    assert result.relation == "SUBSIDIARY"
    assert result.evidence["stated_relationship"]["page"] == 228
    assert "own filing lists it as 'Subsidiary'" in result.note


def test_unlinking_keeps_the_file_withdraws_facts_and_writes_the_audit_trail(session, company):
    doc = _doc(session, company, sha="u1")
    _fact(session, company, "total_income", date(2026, 9, 30), PeriodType.QUARTERLY, "10", doc=doc)
    report = unlink_document(
        session, doc, reason="issued by another entity", actor="user-approved:test"
    )
    session.refresh(doc)
    assert doc.company_id is None and doc.storage_path == "documents/u1.pdf"
    assert report.facts_withdrawn == 1
    event = session.scalar(select(DocumentLinkEvent).where(DocumentLinkEvent.document_id == doc.id))
    assert event.action == "UNLINKED" and event.company_before == company.id
    assert event.actor == "user-approved:test"
    assert event.evidence["withdrawn_facts"][0]["line_item"] == "total_income"


def test_coverage_ignores_documents_from_another_entity(session, company):
    from app.coverage import build_coverage

    ok = _doc(session, company, sha="v1", financial_period="Q2FY2027")
    ok.doc_type = "quarterly_result"
    bad = _doc(
        session, company, sha="v2", financial_period="Q1FY2027", association_status="MISMATCH"
    )
    bad.doc_type = "quarterly_result"
    report = build_coverage(session, company.id, date(2026, 11, 7))
    docs = next(d for d in report.datasets if d.dataset == "QUARTERLY_RESULT_DOCS")
    assert "Q2FY2027" in docs.available_periods and "Q1FY2027" not in docs.available_periods
    assert any(
        "company website" in lim or "has not been checked" in lim for lim in docs.limitations
    )


# ------------------------------------------------------------------------------- bytes of real PDFs

pymupdf = pytest.importorskip("pymupdf")


def _pdf(path, lines):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=9, fontname="helv")
    doc.save(path)
    doc.close()


def test_ingestion_refuses_or_unlinks_documents_from_other_entities(session, company, tmp_path):
    from app.providers.documents import AssociationMismatch, store_document_bytes
    from app.providers.manual import ingest_document_file

    other = session.scalar(select(Company).where(Company.common_name == "TCS"))
    own_pdf, foreign_pdf = tmp_path / "own.pdf", tmp_path / "foreign.pdf"
    _pdf(own_pdf, [(60, 60, "Reliance Industries Limited"), (60, 90, "Annual results " * 8)])
    _pdf(foreign_pdf, [(60, 60, f"{other.legal_name}"), (60, 90, "Annual results " * 8)])
    root = tmp_path / "data"
    root.mkdir()

    doc, created = ingest_document_file(
        session, own_pdf, company_id=company.id, doc_type="annual_report", data_dir=root
    )
    assert created and doc.company_id == company.id and doc.association_status == "CONFIRMED"
    assert (
        session.scalar(
            select(DocumentLinkEvent.action).where(DocumentLinkEvent.document_id == doc.id)
        )
        == "LINKED"
    )

    with pytest.raises(AssociationMismatch):
        ingest_document_file(
            session, foreign_pdf, company_id=company.id, doc_type="annual_report", data_dir=root
        )
    assert (
        not list((root / "documents").rglob("*"))
        or len(list((root / "documents").rglob("*.pdf"))) == 1
    )

    forced, _ = ingest_document_file(
        session,
        foreign_pdf,
        company_id=company.id,
        doc_type="annual_report",
        data_dir=root,
        force_association=True,
    )
    assert forced.company_id == company.id and forced.association_status == "FORCED"

    # automated downloads: stored but NOT attributed, and the refusal is audited
    third = tmp_path / "third.pdf"
    _pdf(third, [(60, 60, f"{other.legal_name}"), (60, 90, "A different set of words " * 6)])
    stray, _ = store_document_bytes(
        session,
        root,
        third.read_bytes(),
        url="https://x.invalid/a.pdf",
        doc_type="annual_report",
        source_code="COMPANY_IR",
        company_id=company.id,
    )
    assert stray.company_id is None and stray.association_status == "MISMATCH"
    refused = session.scalar(
        select(DocumentLinkEvent).where(DocumentLinkEvent.document_id == stray.id)
    )
    assert refused.action == "REFUSED" and refused.company_before == company.id


def test_storage_api_reports_the_shared_state(session, company, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core import storage
    from app.core.database import get_session
    from app.main import create_app

    monkeypatch.setattr(storage, "data_root", lambda: tmp_path)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    body = TestClient(app).get("/api/storage/status?verify_hashes=true").json()
    assert body["root"] == str(tmp_path) and body["documents"] == 0 and body["healthy"] is True
    _ = uuid, Word


def test_inbox_ingests_real_pdfs_refuses_only_wrong_issuer_and_bad_files(
    session, company, tmp_path
):
    from app.docintel.inbox import guess_doc_type, inbox_dir, ingest_inbox

    root = tmp_path / "data"
    folder = inbox_dir(root, "reliance")
    folder.mkdir(parents=True)
    other = session.scalar(select(Company).where(Company.common_name == "TCS"))
    _pdf(
        folder / "RIL-annual-report-2026.pdf",
        [(60, 60, "Reliance Industries Limited"), (60, 90, "Annual Report words " * 6)],
    )
    _pdf(
        folder / "subsidiary-results.pdf",
        [(60, 60, other.legal_name), (60, 90, "Results for the quarter ended " * 4)],
    )
    _pdf(
        folder / "mystery.pdf",
        [(60, 60, "Reliance Industries Limited"), (60, 90, "Nothing identifiable here " * 5)],
    )
    (folder / "notes.txt").write_text("not a pdf", encoding="utf-8")
    (folder / "fake.pdf").write_bytes(b"MZ not really a pdf")
    results = {o.file: o for o in ingest_inbox(session, company.id, root, "reliance")}
    assert results["RIL-annual-report-2026.pdf"].status == "STORED"
    assert results["RIL-annual-report-2026.pdf"].doc_type == "annual_report"
    assert (
        results["subsidiary-results.pdf"].status == "REFUSED"
        and "not from the selected company" in results["subsidiary-results.pdf"].detail
    )
    # a document that cannot be typed is stored as company material, not refused for that reason
    assert results["mystery.pdf"].status == "STORED" and results["mystery.pdf"].doc_type == "other"
    assert (folder / "done" / "mystery.pdf").exists()
    assert results["notes.txt"].status == "REFUSED" and results["fake.pdf"].status == "REFUSED"
    assert (folder / "done" / "RIL-annual-report-2026.pdf").exists()
    assert (
        (folder / "rejected" / "subsidiary-results.pdf.reason.txt")
        .read_text()
        .startswith("this document is not from")
    )
    assert not list(folder.glob("*.pdf"))  # nothing is ingested twice
    assert guess_doc_type("Q2-results.pdf", []) == "quarterly_result"
    assert guess_doc_type("x.pdf", ["Safe Harbor statement"]) == "investor_presentation"
    with pytest.raises(ValueError):
        inbox_dir(root, "../..")


def test_disclosure_endpoints_return_validated_values_with_pages(session, company, tmp_path):
    from fastapi.testclient import TestClient

    from app.core.database import get_session
    from app.main import create_app

    doc = _doc(session, company, sha="api1", association_status="CONFIRMED")
    pages = _content(income_page(), segment_page(), shareholding_page(), roster_page())
    run = extract_document(session, doc, tmp_path, content=pages).extraction
    assert run.status is ExtractionStatus.COMPLETED
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    segments = client.get(f"/api/companies/{company.id}/disclosures?kind=segment").json()
    assert segments and {s["statement"] for s in segments} == {"SEGMENT"}
    retail = [
        s for s in segments if s["dimension"] == "Retail Banking" and s["item"] == "segment_revenue"
    ]
    assert retail and retail[0]["document_id"] == str(doc.id) and retail[0]["page_number"] == 2
    holding = client.get(f"/api/companies/{company.id}/disclosures?kind=SHAREHOLDING").json()
    assert {h["unit"] for h in holding} == {"shares", "percent"}
    assert client.get(f"/api/companies/{company.id}/disclosures?kind=bogus").status_code == 422
    roster = client.get(f"/api/companies/{company.id}/group-entities").json()
    assert ("XYZ Home Finance Limited", "Subsidiary") in {
        (r["entity_name"], r["relationship"]) for r in roster
    }
    # values from a document issued by another entity are never served for this company
    doc.association_status = "MISMATCH"
    session.flush()
    assert client.get(f"/api/companies/{company.id}/disclosures").json() == []
    assert client.get(f"/api/documents/{doc.id}/sections").status_code == 200
    assert client.get(f"/api/documents/{uuid.uuid4()}/sections").status_code == 404


# ---- fiscal alignment: a quarter column that picked up "Year ended" must be withheld --------------


def test_fiscal_alignment_problem_rules():
    from datetime import date

    from app.docintel.periods import fiscal_alignment_problem as problem

    assert problem(date(2026, 3, 31), 12) is None
    assert problem(date(2026, 9, 30), 6) is None
    assert problem(date(2026, 12, 31), 9) is None
    for month_end in (date(2026, 6, 30), date(2026, 9, 30), date(2026, 12, 31), date(2026, 3, 31)):
        assert problem(month_end, 3) is None
    assert "year period cannot end on 30 June 2025" in problem(date(2025, 6, 30), 12)
    assert problem(date(2026, 6, 30), 6) is not None  # no six-month period ends in June
    assert problem(date(2026, 3, 31), 9) is not None
    assert problem(None, 12) is None and problem(date(2026, 6, 30), None) is None


def test_a_header_that_claims_a_year_ending_in_june_is_not_used():
    from app.docintel.layout import Column, Line, Word
    from app.docintel.statements import _read_columns

    def words(text, x0, x1, y):
        return Line([Word(x0, y, x1, y + 8, text)])

    columns = [
        Column(0, 372, 405, 4),
        Column(1, 426, 459, 4),
        Column(2, 480, 514, 4),
        Column(3, 534, 568, 4),
    ]
    header = [
        words("Three months ended", 399, 467, 91),
        words("Year ended", 524, 560, 91),
        Line(
            [
                Word(366, 103, 393, 111, "June 30,"),
                Word(417, 103, 450, 111, "March 31,"),
                Word(474, 103, 501, 111, "June 30,"),
                Word(526, 103, 558, 111, "March 31,"),
            ]
        ),
        Line(
            [
                Word(372, 113, 387, 121, "2026"),
                Word(426, 113, 442, 121, "2026"),
                Word(480, 113, 496, 121, "2025"),
                Word(534, 113, 550, 121, "2026"),
            ]
        ),
    ]
    infos = _read_columns(header, columns, "INCOME")
    usable = [
        (i.period.period_end.isoformat(), i.period.duration_months) for i in infos if i.usable
    ]
    assert ("2025-06-30", 12) not in usable  # the misread "year ended 30 June 2025"
    assert ("2026-03-31", 12) in usable
    wrong = next(i for i in infos if i.period.period_end and i.period.period_end.year == 2025)
    assert wrong.problem  # withheld, whichever safeguard caught it

    # A single, unambiguous "Year ended June 30, 2025" is caught by the fiscal-alignment rule itself.
    one = [Column(0, 100, 140, 4)]
    lines = [
        words("Year ended", 100, 140, 20),
        words("June 30,", 100, 140, 30),
        words("2025", 100, 140, 40),
    ]
    (info,) = _read_columns(lines, one, "INCOME")
    assert not info.usable and "cannot end on 30 June 2025" in info.problem
