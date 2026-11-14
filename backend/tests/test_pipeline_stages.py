"""Pipeline stages: documents are read, provider facts become evidence, readiness reflects both."""

# ruff: noqa: F811
from datetime import date

import pytest
from sqlalchemy import func, select

from app.docintel import doctype
from app.models import (
    Company,
    Document,
    DocumentExtraction,
    DocumentLinkEvent,
    EvidenceRecord,
    ExtractionStatus,
    FinancialFact,
    PipelineStageState,
    ResearchReport,
    ResearchSession,
)
from app.models.pipeline import FAILED, SKIPPED, SUCCEEDED
from app.pipeline import backlog, runner
from app.pipeline import stages as pst
from app.providers.documents import store_document_bytes
from app.research import readiness as rd
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.research_helpers import FY25, complete_company
from tests.test_evidence_db import fact
from tests.test_research_vault import make_pdf, results_pdf, scanned_pdf
from tests.test_sector import seeded  # noqa: F401

IT = ("it_services", "it_services_and_consulting")


@pytest.fixture
def reliance(session):
    seed_reference_data(session)
    seed_test_universe(session)
    session.commit()
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "documents").mkdir()
    return tmp_path


def store(session, data_dir, company, pdf, *, url, doc_type="quarterly_result", title="Results"):
    doc, _ = store_document_bytes(
        session,
        data_dir,
        pdf,
        url=url,
        doc_type=doc_type,
        source_code="COMPANY_IR",
        company_id=company.id,
        title=title,
    )
    session.commit()
    return doc


def go(session, company, stages, data_dir=None, **kw):
    return runner.run_company(session, company, stages=stages, data_dir=data_dir, **kw)


def state(session, company, stage):
    return session.get(PipelineStageState, (company.id, stage))


# ---- extract: fetched documents are read -------------------------------------------------------------


def test_a_fetched_document_is_extracted_and_its_figures_reach_the_fact_table(
    session, reliance, data_dir
):
    doc = store(session, data_dir, reliance, results_pdf(), url="https://www.ril.example/q2.pdf")
    assert session.scalar(select(func.count()).select_from(DocumentExtraction)) == 0
    res = go(session, reliance, ("extract",), data_dir)
    assert res.statuses["extract"] == SUCCEEDED and "1 completed" in res.steps["extract"]
    run = session.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == doc.id))
    assert run.status is ExtractionStatus.COMPLETED
    promoted = session.scalars(
        select(FinancialFact).where(FinancialFact.document_id == doc.id)
    ).all()
    assert promoted, "extracted figures must be promoted into the fact table"
    assert doc.sha256 and doc.url == "https://www.ril.example/q2.pdf"  # provenance untouched


def test_an_unchanged_document_is_not_read_again(session, reliance, data_dir, monkeypatch):
    store(session, data_dir, reliance, results_pdf(), url="https://www.ril.example/q2.pdf")
    go(session, reliance, ("extract",), data_dir)
    from app.docintel import pipeline as dp

    calls = []
    real = dp.extract_document
    monkeypatch.setattr(dp, "extract_document", lambda *a, **k: calls.append(1) or real(*a, **k))
    second = go(session, reliance, ("extract",), data_dir)
    assert not calls and second.steps["extract"] == "no document is waiting to be read"
    assert second.statuses["extract"] == SKIPPED


def test_a_document_issued_by_another_company_stays_unlinked_and_unread(
    session, reliance, data_dir
):
    pdf = results_pdf(issuer="ICICI Prudential Asset Management Company Limited")
    doc = store(session, data_dir, reliance, pdf, url="https://www.ril.example/amc.pdf")
    assert doc.company_id is None and doc.association_status == "MISMATCH"
    res = go(session, reliance, ("extract",), data_dir)
    assert res.steps["extract"] == "no document is waiting to be read"
    assert session.scalar(select(func.count()).select_from(DocumentExtraction)) == 0


def test_a_compliance_report_filed_as_an_annual_report_is_retyped_not_extracted(
    session, reliance, data_dir
):
    pdf = make_pdf(
        [(60, 60, "Reliance Industries Limited"), (60, 80, "Secretarial Compliance Report")]
    )
    doc = store(
        session,
        data_dir,
        reliance,
        pdf,
        url="https://www.ril.example/Secretarial-Compliance-Report-FY26.pdf",
        doc_type="annual_report",
        title="Secretarial Compliance Report FY26",
    )
    res = go(session, reliance, ("extract",), data_dir)
    session.refresh(doc)
    assert doc.doc_type == "other" and doc.company_id == reliance.id
    assert "1 retyped as not financial" in res.steps["extract"]
    assert session.scalar(select(func.count()).select_from(DocumentExtraction)) == 0
    event = session.scalar(
        select(DocumentLinkEvent).where(
            DocumentLinkEvent.document_id == doc.id, DocumentLinkEvent.action == "RETYPED"
        )
    )
    assert event is not None and event.evidence["from"] == "annual_report"


def test_an_image_only_pdf_is_recorded_as_needing_ocr_where_ocr_is_missing(
    session, reliance, data_dir, monkeypatch
):
    from app.docintel import pdf as pdfmod
    from app.docintel import provider as provmod

    monkeypatch.setattr(pdfmod, "ocr_available", lambda: False)
    monkeypatch.setattr(provmod, "ocr_available", lambda: False)
    doc = store(session, data_dir, reliance, scanned_pdf(), url="https://www.ril.example/scan.pdf")
    res = go(session, reliance, ("extract",), data_dir)
    run = session.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == doc.id))
    assert run.status is ExtractionStatus.NEEDS_OCR
    assert "need OCR" in res.steps["extract"] or "needs_ocr" in res.steps["extract"].lower()
    # it is retried automatically once OCR exists: pending_documents lists it again
    from app.docintel.provider import pending_documents

    monkeypatch.setattr(provmod, "ocr_available", lambda: True)
    assert doc in pending_documents(session, reliance.id)


def test_an_extraction_failure_is_recorded_retried_and_eventually_given_up(
    session, reliance, data_dir, monkeypatch
):
    doc = store(session, data_dir, reliance, results_pdf(), url="https://www.ril.example/q2.pdf")
    from app.docintel import pipeline as dp

    def broken(session_, document, *a, **k):
        row = DocumentExtraction(
            document_id=document.id,
            extractor_version=dp.EXTRACTOR_VERSION,
            status=ExtractionStatus.FAILED,
            issues=[],
            error="boom",
        )
        session_.add(row)
        session_.flush()
        return type("R", (), {"extraction": row})()

    monkeypatch.setattr(dp, "extract_document", broken)
    first = go(session, reliance, ("extract",), data_dir)
    st = state(session, reliance, "extract")
    assert first.statuses["extract"] == FAILED and st.error and st.next_retry_at is not None
    assert str(doc.id) in st.detail["failed_documents"]
    for _ in range(pst.MAX_EXTRACT_ATTEMPTS):  # every retry fails again ...
        go(session, reliance, ("extract",), data_dir)
    last = go(session, reliance, ("extract",), data_dir)  # ... until the document is given up on
    assert str(doc.id) in state(session, reliance, "extract").detail["gave_up_on"]
    assert last.statuses["extract"] == SUCCEEDED
    monkeypatch.undo()
    forced = go(
        session, reliance, ("extract",), data_dir, force=True
    )  # an operator can still force it
    assert forced.statuses["extract"] == SUCCEEDED
    assert session.scalar(select(FinancialFact).where(FinancialFact.document_id == doc.id))


def test_the_backlog_finds_companies_with_unread_documents_until_they_are_read(
    session, reliance, data_dir
):
    store(session, data_dir, reliance, results_pdf(), url="https://www.ril.example/q2.pdf")
    assert reliance in backlog.unread_documents(session)
    assert backlog.summary(session)["companies_with_unread_documents"] == 1
    go(session, reliance, ("extract",), data_dir)
    assert reliance not in backlog.unread_documents(session)
    assert backlog.summary(session)["companies_with_unread_documents"] == 0


# ---- evidence: provider facts become validated statement evidence -----------------------------------


def seed_facts(session, company, *, net_profit=True):
    fact(
        session, company, "INCOME", "revenue_from_operations", 1000, provider="UPSTOX_FUNDAMENTALS"
    )
    if net_profit:
        fact(session, company, "INCOME", "net_profit", 120, provider="UPSTOX_FUNDAMENTALS")
    session.commit()


def statement_evidence(session, company):
    return session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company.id,
            EvidenceRecord.kind == "FINANCIAL_STATEMENT",
            EvidenceRecord.status == "ACTIVE",
        )
    ).all()


def test_provider_facts_are_not_evidence_until_the_evidence_stage_runs(session, reliance):
    """The audit: 117 refreshed companies had facts but no evidence, so readiness said BLOCKED."""
    seed_facts(session, reliance)
    assert statement_evidence(session, reliance) == []
    assert rd.evaluate(session, reliance).eligibility == rd.BLOCKED
    assert reliance in backlog.missing_evidence(session)
    res = go(session, reliance, ("evidence", "readiness"))
    assert res.steps["evidence"] == "validated" and statement_evidence(session, reliance)
    assert reliance not in backlog.missing_evidence(session)
    snap = state(session, reliance, "readiness").detail
    assert snap["eligibility"] != rd.BLOCKED and snap["provider_backed"] >= 1


def test_the_stage_records_raw_facts_and_statement_evidence_separately(session, reliance):
    seed_facts(session, reliance)
    go(session, reliance, ("evidence",))
    detail = state(session, reliance, "evidence").detail
    assert detail["provider_facts"] == 2 and detail["statement_evidence"] >= 2
    assert detail["statement_evidence_before"] == 0
    facts = session.scalar(select(func.count()).select_from(FinancialFact))
    assert facts == 2  # the raw rows are never replaced by evidence


def test_sources_periods_units_and_basis_are_preserved_in_the_evidence(session, reliance):
    seed_facts(session, reliance)
    go(session, reliance, ("evidence",))
    rec = next(r for r in statement_evidence(session, reliance) if r.metric.endswith("net_profit"))
    assert rec.provider_code == "UPSTOX_FUNDAMENTALS" and rec.basis == "CONSOLIDATED"
    assert rec.period_end == date(2026, 3, 31) and rec.period_type == "ANNUAL"
    assert rec.source_url.startswith("https://provider.invalid") and rec.original_unit == "crore"


def test_a_metric_the_provider_did_not_return_stays_missing_not_zero(session, reliance):
    seed_facts(session, reliance, net_profit=False)
    go(session, reliance, ("evidence", "readiness"))
    assert not [r for r in statement_evidence(session, reliance) if r.metric.endswith("net_profit")]
    profit = rd.evaluate(session, reliance).by_id("net_profit")
    assert profit.status == rd.MISSING and profit.value is None


def test_the_evidence_stage_does_not_repeat_when_nothing_changed(session, reliance, monkeypatch):
    seed_facts(session, reliance)
    go(session, reliance, ("evidence",))
    n = len(statement_evidence(session, reliance))
    from app.evidence import engine

    calls = []
    real = engine.validate_company
    monkeypatch.setattr(
        engine, "validate_company", lambda *a, **k: calls.append(1) or real(*a, **k)
    )
    again = go(session, reliance, ("evidence",))
    assert not calls and again.statuses["evidence"] == SKIPPED
    assert again.steps["evidence"].startswith("unchanged since")
    assert len(statement_evidence(session, reliance)) == n


def test_a_new_fact_triggers_revalidation_without_duplicating_evidence(session, reliance):
    seed_facts(session, reliance)
    go(session, reliance, ("evidence",))
    before = len(statement_evidence(session, reliance))
    fact(session, reliance, "INCOME", "total_income", 1100, provider="UPSTOX_FUNDAMENTALS")
    session.commit()
    res = go(session, reliance, ("evidence",))
    assert res.steps["evidence"] == "validated"
    assert len(statement_evidence(session, reliance)) == before + 1  # only the new fact is added
    forced = go(session, reliance, ("evidence",), force=True)  # re-syncing is idempotent
    assert forced.steps["evidence"] == "validated"
    assert len(statement_evidence(session, reliance)) == before + 1


def test_extracted_document_figures_enter_the_evidence_store(session, reliance, data_dir):
    doc = store(session, data_dir, reliance, results_pdf(), url="https://www.ril.example/q2.pdf")
    res = go(session, reliance, ("extract", "evidence", "readiness"), data_dir)
    assert all(v == SUCCEEDED for v in res.statuses.values()), res.steps
    docs = [r for r in statement_evidence(session, reliance) if r.document_id == doc.id]
    assert docs and all(r.source_page for r in docs)
    snap = state(session, reliance, "readiness").detail
    assert snap["document_backed"] >= 1  # the report can now cite the fetched document


def test_the_calculate_stage_turns_evidence_into_the_metrics_readiness_asks_for(session, reliance):
    """ABB India: 'inputs are present but the calculation has not produced it'."""
    fact(
        session, reliance, "INCOME", "revenue_from_operations", 1000, provider="UPSTOX_FUNDAMENTALS"
    )
    fact(
        session,
        reliance,
        "INCOME",
        "revenue_from_operations",
        800,
        end=FY25,
        provider="UPSTOX_FUNDAMENTALS",
    )
    session.commit()
    go(session, reliance, ("evidence",))
    before = rd.evaluate(session, reliance).by_id("revenue_growth")
    assert before.status == rd.MISSING and before.action in ("RECALCULATE", "UPLOAD")
    res = go(session, reliance, ("calculate",))
    assert res.statuses["calculate"] == SUCCEEDED and "calculated" in res.steps["calculate"]
    growth = rd.evaluate(session, reliance).by_id("revenue_growth")
    assert growth.ok and round(float(growth.value), 1) == 25.0  # 1000 vs 800
    again = go(session, reliance, ("calculate",))
    assert again.statuses["calculate"] == SKIPPED and "unchanged" in again.steps["calculate"]


def test_nothing_is_calculated_without_validated_evidence(session, reliance):
    res = go(session, reliance, ("calculate",))
    assert res.statuses["calculate"] == SKIPPED and "no validated" in res.steps["calculate"]


# ---- readiness and report -------------------------------------------------------------------------


def test_the_readiness_stage_keeps_a_snapshot_with_the_reasons(session, reliance):
    seed_facts(session, reliance)
    go(session, reliance, ("evidence", "readiness"))
    snap = state(session, reliance, "readiness").detail
    assert set(snap) >= {"eligibility", "score", "counts", "reasons", "missing", "evaluated_at"}
    assert all("reason" in m for m in snap["missing"])


def test_the_report_stage_never_starts_a_report_nobody_asked_for(session, reliance):
    res = go(session, reliance, ("evidence", "readiness", "report"))
    assert (
        res.statuses["report"] == SKIPPED and "no report has been requested" in res.steps["report"]
    )


def test_an_existing_report_gets_a_new_version_only_when_its_inputs_changed(seeded):
    from app.research import report as rp

    co = complete_company(seeded, "Report Co", *IT)
    sess = ResearchSession(company_id=co.id, owner="t", query="Report Co", status="READY")
    seeded.add(sess)
    seeded.flush()
    rp.generate(seeded, sess, "COMPLETE", "t")
    seeded.commit()
    first = go(seeded, co, ("readiness", "report"))
    assert first.statuses["report"] == SUCCEEDED
    versions = lambda: seeded.scalar(  # noqa: E731
        select(func.count()).select_from(ResearchReport).where(ResearchReport.session_id == sess.id)
    )
    n = versions()
    second = go(seeded, co, ("readiness", "report"))
    assert second.steps["report"].startswith("readiness unchanged") and versions() == n


# ---- the type check is conservative ---------------------------------------------------------------


def test_the_type_check_leaves_unreadable_files_to_the_extractor(session, reliance, tmp_path):
    junk = tmp_path / "x.pdf"
    junk.write_bytes(b"not a pdf")
    doc = Document(
        company_id=reliance.id,
        url="https://x/y.pdf",
        doc_type="annual_report",
        content_type="application/pdf",
        sha256="b" * 64,
        size_bytes=9,
        storage_path="documents/bb/y.pdf",
        association_status="UNVERIFIED",
    )
    session.add(doc)
    session.flush()
    verdict = doctype.review_type(session, doc, junk)
    assert not verdict.changed and doc.doc_type == "annual_report"
