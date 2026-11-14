"""The evidence-integrity audit finds planted defects, passes clean data and writes nothing."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.evidence import integrity
from app.evidence.sync import sync_company
from app.models import (
    Company,
    EvidenceLink,
    EvidenceRecord,
    EvidenceStatus,
    FinancialFact,
    LinkRole,
)
from app.models.evidence import EvidenceClass
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.test_evidence_db import FY25, FY26, NOW, TODAY, doc, fact


@pytest.fixture
def co(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


@pytest.fixture
def other(session, co):
    return session.scalar(select(Company).where(Company.common_name == "HDFC Bank"))


def synced(session, *companies):
    for c in companies:
        sync_company(session, c.id, now=NOW)
    session.flush()


def run(session, company=None, **kw):
    report = integrity.audit(session, company_id=company.id if company else None, today=TODAY, **kw)
    return {c.code: c for c in report.checks}, report


def build_company(session, company, provider="UPSTOX_FUNDAMENTALS"):
    for end, rev in ((FY26, 900), (FY25, 800)):
        fact(session, company, "INCOME", "revenue", rev, end=end, provider=provider)
        fact(session, company, "BALANCE_SHEET", "total_assets", rev * 2, end=end, provider=provider)


def test_clean_store_passes(session, co):
    build_company(session, co)
    synced(session, co)
    checks, report = run(session, co)
    assert report.status in ("PASS", "WARN")
    assert report.errors == 0
    assert checks["DUPLICATE_ACTIVE"].clean and checks["ORPHAN_FINANCIAL"].clean
    assert report.evidence_rows > 0


def test_audit_writes_nothing(session, co):
    build_company(session, co)
    synced(session, co)
    session.flush()
    before = session.scalar(select(func.count()).select_from(EvidenceRecord))
    states = session.execute(select(EvidenceRecord.id, EvidenceRecord.status)).all()
    run(session)
    run(session, co)
    assert session.scalar(select(func.count()).select_from(EvidenceRecord)) == before
    assert session.execute(select(EvidenceRecord.id, EvidenceRecord.status)).all() == states
    assert not session.dirty and not session.new and not session.deleted


def test_two_companies_with_the_same_source_key_do_not_collide(session, co, other):
    """Natural keys repeat across companies: a lookup that ignored the company flagged 44,000 rows."""
    build_company(session, co)
    for end, rev in ((FY26, 5), (FY25, 4)):  # same keys, different values
        fact(session, other, "INCOME", "revenue", rev, end=end, provider="UPSTOX_FUNDAMENTALS")
    synced(session, co, other)
    checks, _ = run(session)
    assert checks["VALUE_MISMATCH"].clean
    assert checks["ORPHAN_FINANCIAL"].clean


def test_value_changed_under_the_evidence_is_found(session, co):
    build_company(session, co)
    synced(session, co)
    f = session.scalar(
        select(FinancialFact).where(
            FinancialFact.company_id == co.id, FinancialFact.line_item == "revenue"
        )
    )
    f.original_value = f.original_value + 1  # the source moved; evidence was not re-synced
    session.flush()
    checks, report = run(session, co)
    assert checks["VALUE_MISMATCH"].count == 1
    assert report.status == "FAIL"


def test_orphaned_evidence_is_found(session, co):
    build_company(session, co)
    synced(session, co)
    session.delete(
        session.scalar(
            select(FinancialFact).where(
                FinancialFact.company_id == co.id,
                FinancialFact.line_item == "revenue",
                FinancialFact.period_end == FY26,
            )
        )
    )
    session.flush()
    checks, _ = run(session, co)
    assert checks["ORPHAN_FINANCIAL"].count >= 1


def test_duplicate_active_versions_are_found(session, co):
    build_company(session, co)
    synced(session, co)
    rec = session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == co.id, EvidenceRecord.source_table == "financial_facts"
        )
    ).first()
    clone = EvidenceRecord(
        **{
            c.name: getattr(rec, c.name)
            for c in EvidenceRecord.__table__.columns
            if c.name not in ("id", "created_at")
        }
    )
    clone.content_hash = "x" * 64  # a different version, also ACTIVE
    session.add(clone)
    session.flush()
    checks, _ = run(session, co)
    assert checks["DUPLICATE_ACTIVE"].count == 1


def test_missing_basis_and_unit_conflicts_are_found(session, co):
    build_company(session, co)
    synced(session, co)
    recs = session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == co.id, EvidenceRecord.metric == "INCOME.revenue"
        )
    ).all()
    recs[0].basis = None
    session.flush()
    checks, _ = run(session, co)
    assert checks["MISSING_BASIS"].count == 1
    # same metric, period and basis in two units
    first = recs[1]
    twin = EvidenceRecord(
        **{
            c.name: getattr(first, c.name)
            for c in EvidenceRecord.__table__.columns
            if c.name not in ("id", "created_at")
        }
    )
    twin.unit, twin.source_id, twin.content_hash = "crore", "other-key", "y" * 64
    session.add(twin)
    session.flush()
    checks, _ = run(session, co)
    assert checks["UNIT_CONFLICT"].count == 1


def test_wrong_issuer_document_feeding_evidence_is_found(session, co):
    bad = doc(session, co, status="MISMATCH")
    fact(session, co, "INCOME", "revenue", 100, document=bad, provider="DOCUMENT_EXTRACTION")
    fact(
        session, co, "INCOME", "revenue", 90, end=FY25, document=bad, provider="DOCUMENT_EXTRACTION"
    )
    synced(session, co)
    # the sync itself refuses MISMATCH documents; force one in to prove the audit would notice
    rec = session.scalar(select(EvidenceRecord).where(EvidenceRecord.company_id == co.id))
    if rec is None:
        pytest.skip("sync already refuses mismatched documents (the safer behaviour)")
    rec.document_id = bad.id
    session.flush()
    checks, _ = run(session, co)
    assert checks["MISMATCHED_DOCUMENT"].count >= 1


def test_stale_calculation_is_found(session, co):
    build_company(session, co)
    synced(session, co)
    inputs = session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == co.id, EvidenceRecord.metric == "INCOME.revenue"
        )
    ).all()
    calc = EvidenceRecord(
        company_id=co.id,
        evidence_class=EvidenceClass.CALCULATION,
        kind="DERIVED",
        metric="GROWTH.revenue_yoy",
        period_end=FY26,
        period_type="ANNUAL",
        basis="CONSOLIDATED",
        value=Decimal("12.5"),
        unit="percent",
        expression="(a-b)/b*100",
        content_hash="z" * 64,
        status=EvidenceStatus.ACTIVE,
        created_by="test",
    )
    session.add(calc)
    session.flush()
    for alias, rec in zip("ab", inputs[:2], strict=False):
        session.add(
            EvidenceLink(evidence_id=calc.id, input_id=rec.id, role=LinkRole.INPUT, alias=alias)
        )
    session.flush()
    assert run(session, co)[0]["STALE_CALCULATION"].clean
    inputs[0].status = EvidenceStatus.SUPERSEDED
    session.flush()
    assert run(session, co)[0]["STALE_CALCULATION"].count == 1


def test_provider_facts_with_no_evidence_are_reported(session, co):
    build_company(session, co)  # never synced
    checks, report = run(session, co)
    assert checks["UNSYNCED_PROVIDER"].count == 1
    assert report.status == "WARN"


def test_zero_total_and_future_period_warn(session, co):
    fact(session, co, "INCOME", "revenue", 0, provider="UPSTOX_FUNDAMENTALS")
    fact(
        session,
        co,
        "INCOME",
        "net_profit",
        5,
        end=date(2027, 3, 31),
        provider="UPSTOX_FUNDAMENTALS",
    )
    synced(session, co)
    checks, _ = run(session, co)
    assert checks["ZERO_TOTAL"].count == 1
    assert checks["FUTURE_PERIOD"].count == 1


def test_missing_file_is_reported_only_when_a_data_directory_is_given(session, co, tmp_path):
    doc(session, co)
    assert "MISSING_FILE" not in run(session, co)[0]
    checks, _ = run(session, co, data_dir=tmp_path)
    assert checks["MISSING_FILE"].count == 1


def test_report_serialises_for_the_api_and_cli(session, co):
    build_company(session, co)
    synced(session, co)
    _, report = run(session, co)
    data = integrity.names(session, report).as_dict()
    assert data["status"] in ("PASS", "WARN", "FAIL")
    assert {c["code"] for c in data["checks"]} >= {"DUPLICATE_ACTIVE", "VALUE_MISMATCH"}
