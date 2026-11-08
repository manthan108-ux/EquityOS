"""Evidence store against a real database: sync, resolution, validation, ledger, lineage, API."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.database import get_session
from app.evidence.engine import validate_company
from app.evidence.ledger import (
    LedgerError,
    NoUsableValue,
    price_evidence,
    register_assumption,
    register_calculation,
    register_interpretation,
    register_text_fact,
    resolved_value,
    withdraw,
)
from app.evidence.lineage import lineage_findings, lineage_tree
from app.evidence.sync import sync_company
from app.main import create_app
from app.models import (
    Basis,
    Company,
    Document,
    DocumentExtraction,
    EodPrice,
    EvidenceClass,
    EvidenceLink,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    ExtractedFact,
    ExtractedStatement,
    ExtractionStatus,
    FinancialFact,
    PeriodType,
    ResolutionStatus,
    Security,
    Severity,
    ShareholdingFact,
    Statement,
    ValidationFinding,
    ValidationRun,
    ValidationStatus,
)
from app.seeds.loader import seed_reference_data, seed_test_universe

CR = Decimal(10) ** 7
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
TODAY = date(2026, 11, 8)
FY26 = date(2026, 3, 31)
FY25 = date(2025, 3, 31)


@pytest.fixture
def co(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


@pytest.fixture
def other_co(session, co):
    return session.scalar(select(Company).where(Company.common_name == "HDFC Bank"))


_sha = iter(range(10_000))


def doc(session, company, status="CONFIRMED"):
    n = next(_sha)
    d = Document(
        company_id=company.id if company else None,
        url=f"https://example.invalid/{n}.pdf",
        title="t",
        doc_type="quarterly_result",
        content_type="application/pdf",
        sha256=f"{n:064d}",
        size_bytes=10,
        storage_path=f"documents/{n}.pdf",
        association_status=status,
        retrieved_at=NOW,
    )
    session.add(d)
    session.flush()
    return d


def fact(
    session,
    company,
    statement,
    item,
    value,
    *,
    end=FY26,
    ptype=PeriodType.ANNUAL,
    basis=Basis.CONSOLIDATED,
    unit="crore",
    provider="DOCUMENT_EXTRACTION",
    document=None,
    page=3,
    **extra,
):
    scale = {"crore": CR, "inr": Decimal(1), "inr_per_share": Decimal(1)}.get(unit)
    value = Decimal(str(value))
    row = FinancialFact(
        company_id=company.id,
        provider_code=provider,
        statement=Statement(statement),
        line_item=item,
        period_end=end,
        period_type=ptype,
        basis=basis,
        original_value=value,
        original_unit=unit,
        period_label=f"{end:%b %Y}",
        value_inr=value * scale if scale is not None else None,
        source_url=document.url if document else f"https://provider.invalid/{provider}",
        retrieved_at=NOW,
        document_id=document.id if document else None,
        source_page=page if document else None,
        **extra,
    )
    session.add(row)
    session.flush()
    return row


def balance_sheet(session, company, assets=5000, liabilities=5000, **kw):
    return (
        fact(session, company, "BALANCE_SHEET", "total_assets", assets, **kw),
        fact(session, company, "BALANCE_SHEET", "total_equity_and_liabilities", liabilities, **kw),
    )


def validate(session, company):
    return validate_company(session, company.id, today=TODAY, now=NOW)


def record(session, company, metric, **kw):
    query = select(EvidenceRecord).where(
        EvidenceRecord.company_id == company.id, EvidenceRecord.metric == metric
    )
    for k, v in kw.items():
        query = query.where(getattr(EvidenceRecord, k) == v)
    return session.scalar(query.order_by(EvidenceRecord.id.desc()).limit(1))


# ---- sync -----------------------------------------------------------------------------------------


def test_sync_creates_facts_with_full_provenance(session, co):
    d = doc(session, co)
    fact(session, co, "INCOME", "net_profit", 1234.5, document=d, page=7)
    report = sync_company(session, co.id, now=NOW)
    assert report.created == 1
    r = record(session, co, "INCOME.net_profit")
    assert r.evidence_class is EvidenceClass.FACT and r.status is EvidenceStatus.ACTIVE
    assert (r.value, r.unit) == (Decimal("12345000000.000000"), "inr")
    assert (r.original_value, r.original_unit) == (Decimal("1234.5"), "crore")
    assert (r.document_id, r.source_page, r.provider_code, r.authority_tier) == (
        d.id,
        7,
        "DOCUMENT_EXTRACTION",
        2,
    )
    assert r.source_url == d.url and r.basis == "CONSOLIDATED" and r.period_type == "ANNUAL"


def test_sync_is_idempotent(session, co):
    fact(session, co, "INCOME", "net_profit", 100)
    sync_company(session, co.id, now=NOW)
    again = sync_company(session, co.id, now=NOW)
    assert (again.created, again.unchanged, again.total_changes()) == (0, 1, 0)
    assert session.scalar(select(func.count()).select_from(EvidenceRecord)) == 1


def test_changed_source_value_supersedes_and_keeps_the_old_record(session, co):
    row = fact(session, co, "INCOME", "net_profit", 100)
    sync_company(session, co.id, now=NOW)
    first = record(session, co, "INCOME.net_profit")
    row.original_value, row.value_inr = Decimal(110), Decimal(110) * CR
    session.flush()
    report = sync_company(session, co.id, now=NOW)
    assert (report.created, report.superseded) == (1, 1)
    session.refresh(first)
    assert first.status is EvidenceStatus.SUPERSEDED and first.value == 100 * CR
    live = record(session, co, "INCOME.net_profit", status=EvidenceStatus.ACTIVE)
    assert live.id != first.id and live.value == 110 * CR


def test_removed_source_is_withdrawn_and_a_returning_value_is_reactivated(session, co):
    row = fact(session, co, "INCOME", "net_profit", 100)
    sync_company(session, co.id, now=NOW)
    original = record(session, co, "INCOME.net_profit")
    snapshot = dict(
        company_id=row.company_id,
        provider_code=row.provider_code,
        statement=row.statement,
        line_item=row.line_item,
        period_end=row.period_end,
        period_type=row.period_type,
        basis=row.basis,
        original_value=row.original_value,
        original_unit=row.original_unit,
        period_label=row.period_label,
        value_inr=row.value_inr,
        source_url=row.source_url,
        retrieved_at=row.retrieved_at,
    )
    session.delete(row)
    session.flush()
    assert sync_company(session, co.id, now=NOW).withdrawn == 1
    session.refresh(original)
    assert original.status is EvidenceStatus.WITHDRAWN
    session.add(FinancialFact(**snapshot))
    session.flush()
    report = sync_company(session, co.id, now=NOW)
    assert (report.reactivated, report.created) == (1, 0)
    session.refresh(original)
    assert original.status is EvidenceStatus.ACTIVE  # the same evidence, not a copy


def test_rows_with_unconvertible_units_are_reported_not_used(session, co):
    fact(session, co, "INCOME", "net_profit", 100, unit="furlongs")
    report = sync_company(session, co.id, now=NOW)
    assert report.created == 0 and "unit not convertible" in report.skipped[0]


def test_per_share_values_keep_their_unit(session, co):
    fact(session, co, "INCOME", "eps_basic", "45.6", unit="inr_per_share")
    sync_company(session, co.id, now=NOW)
    r = record(session, co, "INCOME.eps_basic")
    assert (r.value, r.unit) == (Decimal("45.600000"), "inr_per_share")


def _extraction(session, document):
    run = session.scalar(
        select(DocumentExtraction).where(
            DocumentExtraction.document_id == document.id,
            DocumentExtraction.superseded_at.is_(None),
        )
    )
    if run is None:
        run = DocumentExtraction(
            document_id=document.id, extractor_version="t", status=ExtractionStatus.COMPLETED
        )
        session.add(run)
        session.flush()
    return run


def cell(
    session,
    company,
    document,
    statement,
    item,
    value,
    *,
    raw=None,
    dimension=None,
    unit="crore",
    end=FY26,
    months=12,
    basis="CONSOLIDATED",
    promoted=True,
    status=ValidationStatus.PASSED,
    ocr=None,
    review=False,
    page=4,
    row=1,
    col=1,
):
    run = _extraction(session, document)
    st = session.scalar(
        select(ExtractedStatement).where(
            ExtractedStatement.extraction_id == run.id, ExtractedStatement.statement == statement
        )
    )
    if st is None:
        st = ExtractedStatement(
            extraction_id=run.id,
            page_number=page,
            statement=statement,
            basis=basis,
            unit=unit,
            title="t",
            detection="t",
            columns=[],
            rows_matched=1,
            rows_unmatched=0,
        )
        session.add(st)
        session.flush()
    value = Decimal(str(value))
    scale = CR if unit == "crore" else Decimal(1)
    f = ExtractedFact(
        extraction_id=run.id,
        statement_id=st.id,
        document_id=document.id,
        company_id=company.id,
        statement=statement,
        basis=basis,
        canonical_item=item,
        raw_label=item,
        page_number=page,
        row_index=row,
        column_index=col,
        column_header="h",
        period_end=end,
        duration_months=months,
        raw_value=raw or str(value),
        original_value=value,
        original_unit=unit,
        value_inr=value * scale if unit in ("crore", "inr") else None,
        validation_status=status,
        promoted=promoted,
        dimension=dimension,
        needs_review=review,
        ocr_confidence=Decimal(ocr) if ocr else None,
    )
    session.add(f)
    session.flush()
    return f


def test_sync_reads_printed_decimals_and_ocr_confidence_from_the_extraction(session, co):
    d = doc(session, co)
    fact(session, co, "INCOME", "net_profit", "1234.56", document=d)
    cell(session, co, d, "INCOME", "net_profit", "1234.56", raw="1,234.56", ocr="88.5")
    sync_company(session, co.id, now=NOW)
    r = record(session, co, "INCOME.net_profit")
    assert r.details["decimals"] == 2 and r.details["ocr_confidence"] == "88.50"


def test_sync_compares_the_same_cell_across_documents(session, co):
    d1, d2, d3 = doc(session, co), doc(session, co), doc(session, co)
    fact(session, co, "INCOME", "net_profit", 1000, document=d1)
    cell(session, co, d1, "INCOME", "net_profit", 1000)
    cell(session, co, d2, "INCOME", "net_profit", 1000, promoted=False)  # agrees
    cell(session, co, d3, "INCOME", "net_profit", 900, promoted=False)  # differs
    sync_company(session, co.id, now=NOW)
    details = record(session, co, "INCOME.net_profit").details
    assert (details["other_documents_agree"], details["other_documents_differ"]) == (1, 1)


def test_disclosures_are_mirrored_with_dimension_and_mismatched_documents_are_not(session, co):
    d = doc(session, co)
    bad = doc(session, co, status="MISMATCH")
    cell(session, co, d, "SEGMENT", "segment_revenue", 500, dimension="Retail")
    cell(session, co, d, "SEGMENT", "segment_revenue", 700, dimension="Digital", row=2)
    cell(session, co, bad, "SEGMENT", "segment_revenue", 999, dimension="Other")
    cell(session, co, d, "RATIOS", "debt_equity", "0.42", unit="ratio", row=3)
    sync_company(session, co.id, now=NOW)
    rows = session.scalars(select(EvidenceRecord).where(EvidenceRecord.kind == "DISCLOSURE")).all()
    assert sorted((r.metric, r.dimension) for r in rows) == [
        ("RATIOS.debt_equity", ""),
        ("SEGMENT.segment_revenue", "Digital"),
        ("SEGMENT.segment_revenue", "Retail"),
    ]
    ratio = next(r for r in rows if r.metric == "RATIOS.debt_equity")
    assert (ratio.unit, ratio.value) == ("ratio", Decimal("0.420000"))


def test_disclosure_without_a_stated_unit_is_flagged(session, co):
    d = doc(session, co)
    cell(session, co, d, "RATIOS", "mystery", 5, unit=None, row=9)
    sync_company(session, co.id, now=NOW)
    r = record(session, co, "RATIOS.mystery")
    assert r.unit == "unstated" and r.details["unit_unstated"] is True


def test_shareholding_facts_are_mirrored(session, co):
    session.add(
        ShareholdingFact(
            company_id=co.id,
            provider_code="UPSTOX_FUNDAMENTALS",
            category="promoters",
            period_end=date(2026, 9, 30),
            period_label="Sep 2026",
            percent=Decimal("50.3"),
            source_url="https://x.invalid",
            retrieved_at=NOW,
        )
    )
    session.flush()
    sync_company(session, co.id, now=NOW)
    r = record(session, co, "SHAREHOLDING.promoters.percent")
    assert (r.unit, r.value, r.authority_tier) == ("percent", Decimal("50.300000"), 5)


def derived_pair(session, co):
    h1 = fact(
        session,
        co,
        "INCOME",
        "total_income",
        300,
        end=date(2025, 9, 30),
        ptype=PeriodType.HALF_YEAR,
    )
    q1 = fact(
        session,
        co,
        "INCOME",
        "total_income",
        100,
        end=date(2025, 6, 30),
        ptype=PeriodType.QUARTERLY,
    )
    q2 = fact(
        session,
        co,
        "INCOME",
        "total_income",
        200,
        end=date(2025, 9, 30),
        ptype=PeriodType.QUARTERLY,
        provider="DERIVED_QUARTER",
        unit="inr",
        derived_from={
            "method": "Sep 2025 minus Jun 2025",
            "minuend": {"fact_id": h1.id},
            "subtrahend": {"fact_id": q1.id},
            "rounding_uncertainty_inr": "10000000",
        },
    )
    q2.original_value = q2.value_inr = 200 * CR
    session.flush()
    return h1, q1, q2


def test_derived_quarter_becomes_a_calculation_linked_to_its_inputs(session, co):
    derived_pair(session, co)
    report = sync_company(session, co.id, now=NOW)
    assert report.linked == 2
    calc = record(session, co, "INCOME.total_income", evidence_class=EvidenceClass.CALCULATION)
    links = {
        link.alias: session.get(EvidenceRecord, link.input_id)
        for link in session.scalars(select(EvidenceLink).where(EvidenceLink.evidence_id == calc.id))
    }
    assert links["later_cumulative"].period_type == "HALF_YEAR"
    assert links["earlier_cumulative"].period_end == date(2025, 6, 30)
    assert calc.expression == "later_cumulative - earlier_cumulative"
    assert calc.original_value is None  # a calculation has no "published" value
    # Re-syncing neither duplicates nor loses links.
    assert sync_company(session, co.id, now=NOW).linked == 0
    assert session.scalar(select(func.count()).select_from(EvidenceLink)) == 2


# ---- the validation engine ------------------------------------------------------------------------


def test_validation_resolves_values_and_records_a_run(session, co):
    d = doc(session, co)
    balance_sheet(session, co, document=d)
    report = validate(session, co)
    assert report.run.errors == 0 and report.run.evidence_checked == 2
    rows = session.scalars(
        select(EvidenceResolution).where(EvidenceResolution.company_id == co.id)
    ).all()
    assert {r.status for r in rows} == {ResolutionStatus.SINGLE_SOURCE}
    got = resolved_value(
        session,
        co.id,
        "BALANCE_SHEET.total_assets",
        FY26,
        period_type="ANNUAL",
        basis="CONSOLIDATED",
    )
    assert got.evidence.value == 5000 * CR and got.quality.value == "MEDIUM"
    assert any("primary source" in x for x in got.reasons)


def test_a_failed_identity_blocks_exactly_the_values_involved(session, co):
    d = doc(session, co)
    balance_sheet(session, co, assets=5000, liabilities=5300, document=d)
    fact(session, co, "INCOME", "net_profit", 10, document=d)  # unrelated, stays usable
    report = validate(session, co)
    errors = [f for f in report.findings if f.severity is Severity.ERROR]
    assert [f.rule for f in errors] == ["BALANCE_SHEET_IDENTITY"]
    with pytest.raises(NoUsableValue, match="blocked by validation"):
        resolved_value(
            session,
            co.id,
            "BALANCE_SHEET.total_assets",
            FY26,
            period_type="ANNUAL",
            basis="CONSOLIDATED",
        )
    assert (
        resolved_value(
            session, co.id, "INCOME.net_profit", FY26, period_type="ANNUAL", basis="CONSOLIDATED"
        ).evidence.value
        == 10 * CR
    )
    assert report.blocked == 2


def test_source_conflicts_and_authority(session, co):
    d = doc(session, co)
    fact(session, co, "INCOME", "net_profit", 100, document=d)
    fact(session, co, "INCOME", "net_profit", 150, provider="UPSTOX_FUNDAMENTALS")
    fact(session, co, "INCOME", "total_income", 100, document=d)
    fact(
        session, co, "INCOME", "total_income", 150, provider="COMPANY_IR", document=doc(session, co)
    )
    report = validate(session, co)
    by_metric = {r.metric: r for r in session.scalars(select(EvidenceResolution)).all()}
    won = by_metric["INCOME.net_profit"]
    assert won.status is ResolutionStatus.RESOLVED_BY_AUTHORITY
    assert (
        session.get(EvidenceRecord, won.selected_evidence_id).provider_code == "DOCUMENT_EXTRACTION"
    )
    assert {a["provider"] for a in won.alternatives} == {"UPSTOX_FUNDAMENTALS"}
    tie = by_metric["INCOME.total_income"]
    assert tie.status is ResolutionStatus.CONFLICT and tie.selected_evidence_id is None
    rules = {(f.severity, f.rule) for f in report.findings}
    assert (Severity.WARNING, "SOURCE_DISAGREEMENT") in rules
    assert (Severity.ERROR, "CROSS_SOURCE_CONFLICT") in rules
    with pytest.raises(NoUsableValue, match="CONFLICT"):
        resolved_value(
            session, co.id, "INCOME.total_income", FY26, period_type="ANNUAL", basis="CONSOLIDATED"
        )


def test_documents_of_another_company_or_failed_issuer_check_are_never_used(session, co, other_co):
    wrong_company = doc(session, other_co)
    mismatch = doc(session, co, status="MISMATCH")
    fact(session, co, "INCOME", "net_profit", 1, document=wrong_company)
    fact(session, co, "INCOME", "total_income", 1, document=mismatch)
    fact(session, co, "INCOME", "other_income", 1, document=doc(session, co), end=FY25)
    report = validate(session, co)
    rows = {r.metric: r for r in session.scalars(select(EvidenceResolution)).all()}
    assert rows["INCOME.net_profit"].status is ResolutionStatus.UNUSABLE
    assert rows["INCOME.total_income"].status is ResolutionStatus.UNUSABLE
    assert rows["INCOME.other_income"].status is ResolutionStatus.SINGLE_SOURCE
    messages = [f.message for f in report.findings if f.rule == "PROVENANCE"]
    assert any("not linked to this company" in m for m in messages)
    assert any("issuer check failed" in m for m in messages)


def test_each_run_is_kept_and_resolutions_are_rebuilt_not_duplicated(session, co):
    fact(session, co, "INCOME", "net_profit", 100, document=doc(session, co))
    validate(session, co)
    validate(session, co)
    assert session.scalar(select(func.count()).select_from(ValidationRun)) == 2
    assert session.scalar(select(func.count()).select_from(EvidenceResolution)) == 1
    assert session.scalar(select(func.count()).select_from(ValidationFinding)) >= 0


def test_scanned_values_awaiting_review_are_reported_not_hidden(session, co):
    d = doc(session, co)
    cell(session, co, d, "INCOME", "net_profit", 5, review=True, promoted=False)
    report = validate(session, co)
    finding = next(f for f in report.findings if f.rule == "NEEDS_REVIEW_PENDING")
    assert finding.severity is Severity.INFO and finding.details["count"] == 1


def test_validation_of_a_company_without_data_is_clean(session, co):
    report = validate(session, co)
    assert (report.run.evidence_checked, report.run.errors) == (0, 0)


# ---- the ledger: calculations ---------------------------------------------------------------------


@pytest.fixture
def profit_and_equity(session, co):
    d = doc(session, co)
    fact(session, co, "INCOME", "net_profit", 200, document=d)
    fact(session, co, "BALANCE_SHEET", "total_equity", 1000, document=d)
    fact(session, co, "INCOME", "net_profit", 150, document=d, end=FY25)
    fact(
        session,
        co,
        "INCOME",
        "net_profit",
        20,
        document=d,
        ptype=PeriodType.QUARTERLY,
        end=date(2026, 3, 31),
    )
    fact(session, co, "INCOME", "net_profit", 99, document=d, basis=Basis.STANDALONE)
    validate(session, co)
    pick = lambda metric, **kw: record(session, co, metric, **kw)  # noqa: E731
    return {
        "profit": pick(
            "INCOME.net_profit", period_end=FY26, basis="CONSOLIDATED", period_type="ANNUAL"
        ),
        "equity": pick("BALANCE_SHEET.total_equity"),
        "profit_prev": pick("INCOME.net_profit", period_end=FY25),
        "profit_q": pick("INCOME.net_profit", period_type="QUARTERLY"),
        "profit_standalone": pick("INCOME.net_profit", basis="STANDALONE"),
    }


def test_calculation_value_is_computed_by_the_engine_with_lineage(session, co, profit_and_equity):
    e = profit_and_equity
    calc = register_calculation(
        session,
        co.id,
        "RATIO.return_on_equity",
        "profit / equity * 100",
        {"profit": e["profit"].id, "equity": e["equity"].id},
        created_by="test",
    )
    assert (calc.value, calc.unit, calc.evidence_class) == (
        Decimal("20.000000"),
        "percent",
        EvidenceClass.CALCULATION,
    )
    assert (calc.basis, calc.period_type, calc.period_end) == ("CONSOLIDATED", "ANNUAL", FY26)
    assert calc.provider_code == "EVIDENCE_ENGINE" and calc.authority_tier == 2
    tree = lineage_tree(session, calc.id)
    assert {c.alias for c in tree.inputs} == {"profit", "equity"}
    again = register_calculation(
        session,
        co.id,
        "RATIO.return_on_equity",
        "profit / equity * 100",
        {"profit": e["profit"].id, "equity": e["equity"].id},
        created_by="test",
    )
    assert again.id == calc.id  # idempotent


def test_growth_needs_spans_periods_and_never_mixes_quarter_with_year(
    session, co, profit_and_equity
):
    e = profit_and_equity
    args = (session, co.id, "GROWTH.profit", "now / before - 1")
    ids = {"now": e["profit"].id, "before": e["profit_prev"].id}
    with pytest.raises(LedgerError, match="different period ends"):
        register_calculation(*args, ids, created_by="t")
    growth = register_calculation(*args, ids, created_by="t", spans_periods=True)
    assert (growth.value, growth.unit, growth.period_end) == (Decimal("0.333333"), "ratio", FY26)
    with pytest.raises(LedgerError, match="mix"):
        register_calculation(
            *args,
            {"now": e["profit"].id, "before": e["profit_q"].id},
            created_by="t",
            spans_periods=True,
        )


def test_consolidated_and_standalone_are_never_combined(session, co, profit_and_equity):
    e = profit_and_equity
    with pytest.raises(LedgerError, match="one basis only"):
        register_calculation(
            session,
            co.id,
            "RATIO.x",
            "a / b",
            {"a": e["profit"].id, "b": e["profit_standalone"].id},
            created_by="t",
        )


def test_calculation_refuses_unit_errors_unknown_inputs_and_foreign_evidence(
    session, co, other_co, profit_and_equity
):
    e = profit_and_equity
    with pytest.raises(LedgerError, match="refused"):
        register_calculation(
            session,
            co.id,
            "RATIO.x",
            "a * b",
            {"a": e["profit"].id, "b": e["equity"].id},
            created_by="t",
        )
    with pytest.raises(LedgerError, match="disagree"):
        register_calculation(
            session, co.id, "RATIO.x", "a / b", {"a": e["profit"].id}, created_by="t"
        )
    with pytest.raises(LedgerError, match="disagree"):
        register_calculation(
            session,
            co.id,
            "RATIO.x",
            "a",
            {"a": e["profit"].id, "z": e["equity"].id},
            created_by="t",
        )
    with pytest.raises(LedgerError, match="does not exist for this company"):
        register_calculation(
            session, other_co.id, "RATIO.x", "a", {"a": e["profit"].id}, created_by="t"
        )
    with pytest.raises(LedgerError, match="metric"):
        register_calculation(session, co.id, "nodot", "a", {"a": e["profit"].id}, created_by="t")
    zero = register_assumption(
        session,
        co.id,
        "ASSUME.zero",
        "zero for test",
        rationale="a deliberate zero for testing",
        created_by="t",
        value=Decimal(0),
        unit="inr",
    )
    with pytest.raises(LedgerError, match="division by zero"):
        register_calculation(
            session, co.id, "RATIO.x", "a / z", {"a": e["profit"].id, "z": zero.id}, created_by="t"
        )


def test_calculation_refuses_unvalidated_losing_and_blocked_inputs(session, co):
    d = doc(session, co)
    fact(session, co, "INCOME", "net_profit", 100, document=d)
    sync_company(session, co.id, now=NOW)
    fresh = record(session, co, "INCOME.net_profit")
    with pytest.raises(LedgerError, match="has not been validated"):
        register_calculation(session, co.id, "RATIO.x", "a", {"a": fresh.id}, created_by="t")
    # a weaker source that lost the resolution cannot be used
    fact(session, co, "INCOME", "net_profit", 130, provider="UPSTOX_FUNDAMENTALS")
    validate(session, co)
    loser = record(session, co, "INCOME.net_profit", provider_code="UPSTOX_FUNDAMENTALS")
    with pytest.raises(LedgerError, match="not the resolved value"):
        register_calculation(session, co.id, "RATIO.x", "a", {"a": loser.id}, created_by="t")
    # a blocked value cannot be used
    balance_sheet(session, co, assets=1, liabilities=5, document=d)
    validate(session, co)
    blocked = record(session, co, "BALANCE_SHEET.total_assets")
    with pytest.raises(LedgerError, match="blocked by validation"):
        register_calculation(session, co.id, "RATIO.x", "a", {"a": blocked.id}, created_by="t")


def test_recalculation_supersedes_the_earlier_calculation(session, co, profit_and_equity):
    e = profit_and_equity
    first = register_calculation(
        session,
        co.id,
        "RATIO.roe",
        "a / b",
        {"a": e["profit"].id, "b": e["equity"].id},
        created_by="t",
    )
    # the same quantity from a different formula replaces it
    second = register_calculation(
        session,
        co.id,
        "RATIO.roe",
        "a / b * 100",
        {"a": e["profit"].id, "b": e["equity"].id},
        created_by="t",
    )
    session.refresh(first)
    assert first.status is EvidenceStatus.SUPERSEDED and "recalculated" in first.status_reason
    assert second.status is EvidenceStatus.ACTIVE


# ---- assumptions and interpretations --------------------------------------------------------------


def test_assumption_must_be_stated_justified_and_attributed(session, co):
    ok = dict(rationale="Management guidance of mid-teens growth", created_by="forecast-agent")
    a = register_assumption(
        session,
        co.id,
        "FORECAST.growth",
        "Revenue grows 12% a year",
        value=Decimal("12"),
        unit="percent",
        **ok,
    )
    assert a.evidence_class is EvidenceClass.ASSUMPTION and a.details["rationale"]
    assert (
        register_assumption(
            session,
            co.id,
            "FORECAST.growth",
            "Revenue grows 12% a year",
            value=Decimal("12"),
            unit="percent",
            **ok,
        ).id
        == a.id
    )
    with pytest.raises(LedgerError, match="rationale"):
        register_assumption(session, co.id, "FORECAST.g", "x", rationale="short", created_by="t")
    with pytest.raises(LedgerError, match="state what"):
        register_assumption(session, co.id, "FORECAST.g", " ", **ok)
    with pytest.raises(LedgerError, match="both a value and its unit"):
        register_assumption(session, co.id, "FORECAST.g", "x", value=Decimal(1), **ok)
    with pytest.raises(LedgerError, match="who or what"):
        register_assumption(
            session, co.id, "FORECAST.g", "x", rationale=ok["rationale"], created_by=" "
        )


def test_assumption_can_feed_a_calculation_and_is_listed_as_such(session, co, profit_and_equity):
    e = profit_and_equity
    g = register_assumption(
        session,
        co.id,
        "FORECAST.growth",
        "growth of 10%",
        rationale="analyst assumption for tests",
        created_by="t",
        value=Decimal("0.10"),
        unit="ratio",
        supports=(e["profit"].id,),
    )
    forecast = register_calculation(
        session,
        co.id,
        "FORECAST.profit_next",
        "profit * (1 + g)",
        {"profit": e["profit"].id, "g": g.id},
        created_by="t",
    )
    assert forecast.value == Decimal("220.000000") * CR
    assert forecast.details["assumption_inputs"] == ["g"]


def test_interpretation_may_only_use_numbers_from_its_evidence(session, co, profit_and_equity):
    e = profit_and_equity
    roe = register_calculation(
        session,
        co.id,
        "RATIO.roe",
        "profit / equity * 100",
        {"profit": e["profit"].id, "equity": e["equity"].id},
        created_by="t",
    )
    ok = register_interpretation(
        session,
        co.id,
        "THESIS.returns",
        "Return on equity is 20% on net profit of ₹200 crore.",
        [roe.id, e["profit"].id],
        created_by="analyst-agent",
    )
    assert ok.evidence_class is EvidenceClass.INTERPRETATION
    assert len(ok.details["figures_traced"]) == 2
    with pytest.raises(LedgerError, match="not found in the cited evidence: '25%'"):
        register_interpretation(
            session, co.id, "THESIS.returns", "ROE is 25%.", [roe.id], created_by="a"
        )
    with pytest.raises(LedgerError, match="at least one"):
        register_interpretation(session, co.id, "THESIS.x", "A well run bank.", [], created_by="a")
    with pytest.raises(LedgerError, match="itself an interpretation"):
        register_interpretation(session, co.id, "THESIS.y", "As above.", [ok.id], created_by="a")


# ---- lineage re-checks ----------------------------------------------------------------------------


def test_input_superseded_after_the_calculation_is_caught_and_blocks_it(
    session, co, profit_and_equity
):
    e = profit_and_equity
    calc = register_calculation(
        session,
        co.id,
        "RATIO.roe",
        "p / q",
        {"p": e["profit"].id, "q": e["equity"].id},
        created_by="t",
    )
    claim = register_interpretation(
        session, co.id, "THESIS.r", "The ratio is 0.2.", [calc.id], created_by="a"
    )
    row = session.scalar(select(FinancialFact).where(FinancialFact.line_item == "total_equity"))
    row.original_value, row.value_inr = Decimal(1100), Decimal(1100) * CR
    session.flush()
    report = validate(session, co)
    rules = {f.rule for f in report.findings if f.severity is Severity.ERROR}
    assert {"CALC_INPUT_NOT_CURRENT", "CLAIM_CITES_BLOCKED"} <= rules
    resolution = session.scalar(
        select(EvidenceResolution).where(EvidenceResolution.selected_evidence_id == calc.id)
    )
    assert resolution.blocked and "CALC_INPUT_NOT_CURRENT" in resolution.blocked_by
    # the sound inputs stay usable; only the broken calculation is blocked
    assert not session.scalar(
        select(EvidenceResolution).where(EvidenceResolution.selected_evidence_id == e["profit"].id)
    ).blocked
    assert claim.status is EvidenceStatus.ACTIVE


def test_tampered_calculation_value_is_detected(session, co, profit_and_equity):
    e = profit_and_equity
    calc = register_calculation(
        session,
        co.id,
        "RATIO.roe",
        "p / q",
        {"p": e["profit"].id, "q": e["equity"].id},
        created_by="t",
    )
    calc.value = Decimal("0.9")
    session.flush()
    found = [f for f in lineage_findings(session, co.id) if f.rule == "CALC_RECOMPUTE_MISMATCH"]
    assert len(found) == 1 and found[0].blocks == [calc.id]


def test_calculation_without_inputs_or_with_missing_links_is_reported(session, co):
    session.add(
        EvidenceRecord(
            company_id=co.id,
            evidence_class=EvidenceClass.CALCULATION,
            kind="CALCULATION",
            metric="X.y",
            period_end=FY26,
            value=Decimal(1),
            unit="ratio",
            expression="a",
            provider_code="EVIDENCE_ENGINE",
            source_url="calculation:X.y",
            content_hash="h1",
        )
    )
    session.add(
        EvidenceRecord(
            company_id=co.id,
            evidence_class=EvidenceClass.CALCULATION,
            kind="CALCULATION",
            metric="X.z",
            period_end=FY26,
            value=Decimal(1),
            unit="ratio",
            expression="a",
            provider_code="EVIDENCE_ENGINE",
            source_url="calculation:X.z",
            content_hash="h2",
            details={"missing_inputs": ["a"]},
        )
    )
    session.flush()
    rules = {f.rule for f in lineage_findings(session, co.id)}
    assert rules == {"CALC_NO_INPUTS", "CALC_INPUT_MISSING"}


def test_claim_citing_withdrawn_evidence_is_flagged(session, co, profit_and_equity):
    e = profit_and_equity
    a = register_assumption(
        session,
        co.id,
        "ASSUME.x",
        "value of 5%",
        value=Decimal(5),
        unit="percent",
        rationale="a reason long enough",
        created_by="t",
    )
    claim = register_interpretation(
        session, co.id, "THESIS.x", "We assume 5%.", [a.id], created_by="t"
    )
    withdraw(session, a.id, "no longer valid", by="tester")
    assert session.get(EvidenceRecord, a.id).status is EvidenceStatus.WITHDRAWN
    rules = {f.rule for f in lineage_findings(session, co.id)}
    assert "CLAIM_CITES_STALE" in rules
    assert claim.id and e


def test_withdraw_applies_only_to_ledger_evidence(session, co):
    fact(session, co, "INCOME", "net_profit", 1)
    sync_company(session, co.id, now=NOW)
    mirrored = record(session, co, "INCOME.net_profit")
    with pytest.raises(LedgerError, match="correcting their source"):
        withdraw(session, mirrored.id, "because", by="t")
    with pytest.raises(LedgerError, match="reason"):
        a = register_assumption(
            session, co.id, "ASSUME.q", "text", rationale="a long enough one", created_by="t"
        )
        withdraw(session, a.id, " ", by="t")


def test_resolved_value_explains_every_failure(session, co):
    with pytest.raises(NoUsableValue, match="no evidence stored"):
        resolved_value(session, co.id, "INCOME.net_profit", FY26)


# ---- prices ---------------------------------------------------------------------------------------


def test_price_evidence_is_marked_unadjusted_and_idempotent(session, co, other_co):
    sec = session.scalar(select(Security).where(Security.company_id == co.id))
    session.add(
        EodPrice(
            security_id=sec.id,
            trade_date=date(2026, 11, 6),
            series="EQ",
            close=Decimal("1500.25"),
            source_url="https://nse.invalid/bhav.csv",
            file_sha256="f" * 64,
            retrieved_at=NOW,
        )
    )
    session.flush()
    p = price_evidence(session, co.id, sec.id, date(2026, 11, 6))
    assert (p.value, p.unit, p.provider_code, p.authority_tier) == (
        Decimal("1500.250000"),
        "inr_per_share",
        "MARKET_DATA",
        1,
    )
    assert p.details["unadjusted"] is True
    assert price_evidence(session, co.id, sec.id, date(2026, 11, 6)).id == p.id
    mc = register_calculation(session, co.id, "PRICE.double", "p * 2", {"p": p.id}, created_by="t")
    assert mc.value == Decimal("3000.500000")
    with pytest.raises(LedgerError, match="no stored close"):
        price_evidence(session, co.id, sec.id, date(2026, 11, 5))
    with pytest.raises(LedgerError, match="does not belong"):
        price_evidence(session, other_co.id, sec.id, date(2026, 11, 6))


# ---- database guarantees --------------------------------------------------------------------------


def _bad(co, **kw):
    base = dict(company_id=co.id, kind="X", metric="A.b", content_hash=uuid.uuid4().hex)
    return EvidenceRecord(**{**base, **kw})


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(evidence_class=EvidenceClass.FACT, value=Decimal(1), unit="inr"),  # no provenance
        dict(evidence_class=EvidenceClass.FACT, provider_code="P", source_url="u"),  # no value
        dict(evidence_class=EvidenceClass.ASSUMPTION),  # no text
        dict(evidence_class=EvidenceClass.INTERPRETATION, statement_text=""),
        dict(evidence_class=EvidenceClass.CALCULATION, value=Decimal(1), unit="inr"),  # no formula
        dict(
            evidence_class=EvidenceClass.ASSUMPTION, statement_text="x", value=Decimal(1)
        ),  # no unit
    ],
)
def test_database_refuses_evidence_that_breaks_the_rules(session, co, kwargs):
    with pytest.raises(IntegrityError):
        session.add(_bad(co, **kwargs))
        session.flush()


def test_a_version_of_a_source_row_exists_once_and_links_cannot_point_at_themselves(session, co):
    one = dict(
        evidence_class=EvidenceClass.FACT,
        provider_code="P",
        source_url="u",
        value=Decimal(1),
        unit="inr",
        source_table="t",
        source_id="k",
        content_hash="same",
    )
    session.add(_bad(co, **one))
    session.flush()
    with pytest.raises(IntegrityError):
        session.add(_bad(co, **one))
        session.flush()


def test_self_link_is_refused(session, co):
    r = _bad(co, evidence_class=EvidenceClass.ASSUMPTION, statement_text="s")
    session.add(r)
    session.flush()
    with pytest.raises(IntegrityError):
        session.add(EvidenceLink(evidence_id=r.id, input_id=r.id, role="SUPPORTS"))
        session.flush()


# ---- API --------------------------------------------------------------------------------------------


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_api_exposes_evidence_resolved_values_validation_and_lineage(session, co, client):
    d = doc(session, co)
    balance_sheet(session, co, document=d)
    fact(session, co, "INCOME", "net_profit", 200, document=d)
    fact(session, co, "BALANCE_SHEET", "total_equity", 1000, document=d)
    validate(session, co)
    profit = record(session, co, "INCOME.net_profit")
    equity = record(session, co, "BALANCE_SHEET.total_equity")
    calc = register_calculation(
        session, co.id, "RATIO.roe", "p / q * 100", {"p": profit.id, "q": equity.id}, created_by="t"
    )
    base = f"/api/companies/{co.id}"

    listed = client.get(f"{base}/evidence", params={"metric": "INCOME.net_profit"}).json()
    assert [r["id"] for r in listed] == [profit.id] and listed[0]["page"] == 3
    assert (
        client.get(f"{base}/evidence", params={"evidence_class": "calculation"}).json()[0]["id"]
        == calc.id
    )
    assert client.get(f"{base}/evidence", params={"evidence_class": "nope"}).status_code == 422
    assert client.get(f"{base}/evidence", params={"status": "nope"}).status_code == 422

    detail = client.get(f"/api/evidence/{calc.id}").json()
    assert detail["evidence"]["expression"] == "p / q * 100"
    assert {n["alias"] for n in detail["lineage"]["inputs"]} == {"p", "q"}
    assert client.get(f"/api/evidence/{profit.id}").json()["used_by"] == [calc.id]
    assert client.get("/api/evidence/999999").status_code == 404

    resolved = client.get(f"{base}/resolved", params={"metric": "INCOME.net_profit"}).json()
    assert resolved[0]["status"] == "SINGLE_SOURCE" and resolved[0]["selected"]["id"] == profit.id
    assert resolved[0]["quality"] == "MEDIUM" and resolved[0]["blocked"] is False

    validation = client.get(f"{base}/validation").json()
    assert validation["errors"] == 0 and validation["evidence_checked"] >= 3
    assert client.get(f"{base}/validation", params={"severity": "bogus"}).status_code == 422

    summary = client.get(f"{base}/evidence/summary").json()
    assert summary["by_class"]["FACT"] == 4 and summary["by_class"]["CALCULATION"] == 1
    assert summary["last_run_id"] == validation["run_id"] and summary["blocked"] == 0


def test_api_404s_and_no_validation_yet(session, co, client):
    ghost = uuid.uuid4()
    for path in ("evidence", "resolved", "validation", "evidence/summary"):
        assert client.get(f"/api/companies/{ghost}/{path}").status_code == 404
    assert client.get(f"/api/companies/{co.id}/validation").status_code == 404
    assert client.get(f"/api/companies/{co.id}/evidence/summary").json()["last_run_id"] is None


def test_api_is_read_only(client, co):
    assert client.post(f"/api/companies/{co.id}/evidence").status_code in (404, 405)


def test_a_shareholding_category_in_shares_and_in_percent_is_not_a_conflict(session, co):
    d = doc(session, co)
    cell(
        session,
        co,
        d,
        "SHAREHOLDING",
        "mutual_funds",
        1_000_000,
        unit="shares",
        months=None,
        end=date(2026, 3, 31),
        row=1,
        basis=None,
    )
    cell(
        session,
        co,
        d,
        "SHAREHOLDING",
        "mutual_funds",
        "12.5",
        unit="percent",
        months=None,
        end=date(2026, 3, 31),
        row=1,
        col=2,
        basis=None,
    )
    report = validate(session, co)
    assert not [f for f in report.findings if f.severity is Severity.ERROR]
    metrics = {r.metric for r in session.scalars(select(EvidenceResolution)).all()}
    assert metrics == {"SHAREHOLDING.mutual_funds.shares", "SHAREHOLDING.mutual_funds.percent"}


# ---- narrative (text) facts -----------------------------------------------------------------------


def test_a_fact_may_be_narrative_but_not_empty(session, co):
    ok = EvidenceRecord(
        company_id=co.id,
        evidence_class=EvidenceClass.FACT,
        kind="TEXT",
        metric="NEWS.headline",
        statement_text="Bank announces results",
        provider_code="NEWS_FEEDS",
        source_url="https://n",
        content_hash="t1",
    )
    session.add(ok)
    session.flush()
    with pytest.raises(IntegrityError):
        session.add(
            EvidenceRecord(
                company_id=co.id,
                evidence_class=EvidenceClass.FACT,
                kind="TEXT",
                metric="NEWS.headline",
                statement_text="",
                provider_code="NEWS_FEEDS",
                source_url="https://n",
                content_hash="t2",
            )
        )
        session.flush()


def test_document_sections_are_mirrored_as_text_facts(session, co):
    from app.models import DocumentSection

    d, bad = doc(session, co), doc(session, co, status="MISMATCH")
    for document, heading in ((d, "Independent Auditor's Report"), (bad, "Other entity's report")):
        run = _extraction(session, document)
        session.add(
            DocumentSection(
                extraction_id=run.id,
                document_id=document.id,
                kind="AUDITOR_REPORT",
                heading=heading,
                start_page=12,
                end_page=15,
                text="x" * 100,
                flags={"opinion": "unmodified", "quotes": ["Key Audit Matters"]},
            )
        )
    session.flush()
    report = sync_company(session, co.id, now=NOW)
    r = record(session, co, "SECTION.auditor_report")
    assert report.created == 1 and r.statement_text == "Independent Auditor's Report"
    assert (r.value, r.document_id, r.source_page, r.authority_tier) == (None, d.id, 12, 2)
    assert r.details["flags"]["opinion"] == "unmodified" and r.details["end_page"] == 15
    # text facts are not numbers: validation neither resolves nor blocks them
    assert validate(session, co).run.evidence_checked == 0


def test_register_text_fact_validates_deduplicates_and_takes_the_providers_tier(session, co):
    seed_reference_data(session)
    kw = dict(provider_code="MINT_RSS", source_url="https://news.invalid/a", created_by="t")
    a = register_text_fact(session, co.id, "NEWS.headline", "Company raises capital", **kw)
    assert (a.kind, a.authority_tier, a.value) == ("TEXT", 5, None)
    assert (
        register_text_fact(session, co.id, "NEWS.headline", "Company raises capital", **kw).id
        == a.id
    )
    unknown = register_text_fact(
        session,
        co.id,
        "NEWS.headline",
        "Other",
        provider_code="NOBODY",
        source_url="https://x",
        created_by="t",
    )
    assert unknown.authority_tier == 6
    with pytest.raises(LedgerError, match="needs text"):
        register_text_fact(session, co.id, "NEWS.headline", "  ", **kw)
    with pytest.raises(LedgerError, match="where it came from"):
        register_text_fact(
            session, co.id, "NEWS.headline", "x", provider_code="P", source_url=" ", created_by="t"
        )
    with pytest.raises(LedgerError, match="metric"):
        register_text_fact(session, co.id, "headline", "x", **kw)
    with pytest.raises(LedgerError, match="does not belong"):
        register_text_fact(session, co.id, "NEWS.headline", "x", document_id=uuid.uuid4(), **kw)


def test_interpretation_may_cite_narrative_evidence_but_not_add_numbers(session, co):
    news = register_text_fact(
        session,
        co.id,
        "NEWS.headline",
        "Management guides for stable margins",
        provider_code="NEWS_FEEDS",
        source_url="https://n",
        created_by="t",
    )
    ok = register_interpretation(
        session,
        co.id,
        "THESIS.margins",
        "Management expects stable margins.",
        [news.id],
        created_by="a",
    )
    assert ok.evidence_class is EvidenceClass.INTERPRETATION
    with pytest.raises(LedgerError, match="'15%'"):
        register_interpretation(
            session,
            co.id,
            "THESIS.margins",
            "Management expects margins of 15%.",
            [news.id],
            created_by="a",
        )
    with pytest.raises(LedgerError, match="no numeric value"):
        register_calculation(session, co.id, "RATIO.x", "a", {"a": news.id}, created_by="t")


# ---- review queue -----------------------------------------------------------------------------------


def test_review_queue_lists_withheld_scanned_values_and_open_findings(session, co, client):
    d = doc(session, co)
    cell(session, co, d, "INCOME", "net_profit", 5, review=True, promoted=False, page=9)
    cell(session, co, d, "INCOME", "total_income", 9, review=True, promoted=False, page=9, row=2)
    balance_sheet(session, co, assets=5000, liabilities=5300, document=d)  # an ERROR finding
    validate(session, co)
    body = client.get(f"/api/companies/{co.id}/review-queue").json()
    assert body["scanned_values_waiting"] == 2 and body["errors"] == 1
    kinds = [i["kind"] for i in body["items"]]
    assert kinds.count("SCANNED_VALUE") == 2 and kinds.count("FINDING") == 1
    scanned = next(i for i in body["items"] if i["kind"] == "SCANNED_VALUE")
    assert scanned["page"] == 9 and scanned["document_id"] == str(d.id)
    assert "label" in scanned["detail"] and "NOT in the evidence store" in body["note"]
    finding = next(i for i in body["items"] if i["kind"] == "FINDING")
    assert finding["severity"] == "ERROR" and "BALANCE_SHEET_IDENTITY" in finding["summary"]


def test_review_queue_is_empty_for_a_clean_company_and_404_for_unknown(session, co, client):
    body = client.get(f"/api/companies/{co.id}/review-queue").json()
    assert body["items"] == [] and body["scanned_values_waiting"] == 0
    assert client.get(f"/api/companies/{uuid.uuid4()}/review-queue").status_code == 404
