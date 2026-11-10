"""Builders for the research-workflow tests: companies with just the evidence a profile needs."""

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal as D

from app.models import (
    Document,
    EvidenceClass,
    EvidenceRecord,
    EvidenceResolution,
    Quality,
    ResolutionStatus,
)
from app.research import requirements as rq
from tests.test_sector import mk_company, size  # noqa: F401

TODAY = date(2026, 11, 8)
FY26 = date(2026, 3, 31)
FY25 = date(2025, 3, 31)
QUARTER = date(2026, 9, 30)
PRICE_DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def mk_document(session, company, doc_type="press_release"):
    sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    doc = Document(
        company_id=company.id,
        url=f"test://{sha[:8]}",
        title="test document",
        doc_type=doc_type,
        content_type="application/pdf",
        sha256=sha,
        size_bytes=10,
        storage_path=f"documents/{sha[:2]}/{sha}.pdf",
        association_status="CONFIRMED",
    )
    session.add(doc)
    session.flush()
    return doc


def evidence(
    session,
    company,
    metric,
    value=None,
    *,
    end=FY26,
    ptype="ANNUAL",
    basis="CONSOLIDATED",
    kind="FINANCIAL_STATEMENT",
    unit="inr",
    document=None,
    tier=2,
    quality=Quality.MEDIUM,
    blocked=False,
    text=None,
    page=3,
):
    cls = EvidenceClass.CALCULATION if kind == "CALCULATION" else EvidenceClass.FACT
    r = EvidenceRecord(
        company_id=company.id,
        evidence_class=cls,
        kind=kind,
        metric=metric,
        dimension="",
        period_end=end,
        period_type=ptype,
        basis=basis,
        value=None if value is None else D(str(value)),
        unit=unit if value is not None else None,
        expression="synthetic" if cls is EvidenceClass.CALCULATION else None,
        statement_text=text,
        provider_code="TEST",
        authority_tier=tier,
        source_url="test://",
        document_id=document.id if document else None,
        source_page=page if document else None,
        retrieved_at=NOW,
        details={"missing_inputs": ["synthetic"]} if cls is EvidenceClass.CALCULATION else None,
        content_hash=uuid.uuid4().hex,
        created_by="test",
        status_changed_at=NOW,
    )
    session.add(r)
    session.flush()
    session.add(
        EvidenceResolution(
            company_id=company.id,
            metric=metric,
            dimension="",
            period_end=end or date(1900, 1, 1),
            period_type=ptype or "",
            basis=basis or "",
            selected_evidence_id=r.id,
            status=ResolutionStatus.SINGLE_SOURCE,
            quality=quality,
            quality_reasons=[],
            alternatives=[],
            blocked=blocked,
            blocked_by=["TEST_RULE"] if blocked else None,
        )
    )
    session.flush()
    return r


# Values that must agree with the evidence engine's own identities (NII = earned - expended).
VALUE_OF = {"INCOME.interest_expended": 4, "INCOME.net_interest_income": 6}


def satisfy(session, company, req, document=None, **kw):
    """Add the single piece of evidence that makes `req` AVAILABLE."""
    metric = req.metrics[0]
    if req.period == "PRICE":
        kind = "PRICE" if metric.startswith("PRICE.") else "CALCULATION"
        return evidence(
            session, company, metric, 100, end=PRICE_DAY, ptype=None, basis=None,
            kind=kind, unit="inr", document=None, **kw,
        )  # fmt: skip
    if req.period == "NARRATIVE":
        return evidence(
            session, company, metric, None, end=None, ptype=None, basis=None,
            kind="DOCUMENT_SECTION", text="heading", document=document, **kw,
        )  # fmt: skip
    standalone = req.basis == "STANDALONE_OK"
    basis = "STANDALONE" if standalone else "CONSOLIDATED"
    if req.period in ("QUARTERLY", "LATEST"):
        return evidence(
            session, company, metric, 5, end=QUARTER, ptype="QUARTERLY", basis=basis,
            kind="FINANCIAL_STATEMENT", document=document, **kw,
        )  # fmt: skip
    kind = "CALCULATION" if req.extractor == rq.DERIVED else "FINANCIAL_STATEMENT"
    return evidence(
        session, company, metric, VALUE_OF.get(metric, 10), end=FY26, ptype="ANNUAL", basis=basis,
        kind=kind, document=document, **kw,
    )  # fmt: skip


def complete_company(session, name, sector, industry, *, skip=(), document=True):
    """A company holding evidence for EVERY requirement of its profile except those in `skip`."""
    company = mk_company(session, name, sector=sector, industry=industry)
    profile = rq.profile_for(sector, industry)
    doc = mk_document(session, company) if document else None
    for req in rq.requirements_for(profile):
        if req.id in skip or req.extractor == rq.NONE:
            continue
        # figures behind a filing carry the document, as extracted values do
        with_doc = (
            doc if req.id in ("revenue", "net_profit", "latest_quarter", "total_income") else None
        )
        satisfy(
            session, company, req, document=with_doc or (doc if req.period == "NARRATIVE" else None)
        )
    return company
