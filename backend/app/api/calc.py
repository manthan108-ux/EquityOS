"""Read-only API for calculated metrics, technical indicators and price adjustments (Phase 7)."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calc.catalogue import BY_CODE
from app.core.database import get_session
from app.models import (
    CalculationGap,
    CalculationRun,
    Company,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    PriceAdjustment,
    Security,
)

router = APIRouter(tags=["calculations"])
SessionDep = Annotated[Session, Depends(get_session)]


class MetricOut(BaseModel):
    evidence_id: int
    metric: str
    label: str | None
    category: str | None
    period_end: date | None
    period_type: str | None
    basis: str | None
    value: str
    unit: str
    quality: str | None
    blocked: bool
    formula: str | None
    definition: str | None
    notes: list[str]
    inputs: dict


class GapOut(BaseModel):
    metric: str
    period_end: date | None
    period_type: str | None
    basis: str | None
    reason: str
    missing: list[str] | None


class RunOut(BaseModel):
    run_id: int
    engine_version: str
    as_of: date
    profile: str
    calculated: int
    gaps: int
    summary: dict | None


class AdjustmentOut(BaseModel):
    security_id: int
    symbol: str
    ex_date: date
    factor: str
    kind: str
    origin: str
    status: str
    confidence: str
    detail: str


def _company(session: Session, company_id: uuid.UUID) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _metric_out(r: EvidenceRecord, resolution: EvidenceResolution | None) -> MetricOut:
    d = r.details or {}
    spec = BY_CODE.get(r.metric)
    return MetricOut(
        evidence_id=r.id,
        metric=r.metric,
        label=spec.label if spec else None,
        category=spec.category if spec else r.metric.split(".")[0],
        period_end=r.period_end,
        period_type=r.period_type,
        basis=r.basis,
        value=str(r.value),
        unit=r.unit or "",
        quality=resolution.quality.value if resolution else None,
        blocked=bool(resolution and resolution.blocked),
        formula=r.expression,
        definition=d.get("definition"),
        notes=list(d.get("notes") or []),
        inputs=d.get("inputs") or {},
    )


@router.get("/api/companies/{company_id}/metrics", response_model=list[MetricOut])
def metrics(
    company_id: uuid.UUID,
    session: SessionDep,
    prefix: str | None = None,
    basis: str | None = None,
    period_type: str | None = None,
    limit: int = 500,
) -> list[MetricOut]:
    """Calculated fundamentals and valuation multiples with formula, inputs and quality."""
    _company(session, company_id)
    query = select(EvidenceRecord).where(
        EvidenceRecord.company_id == company_id,
        EvidenceRecord.kind == "CALCULATION",
        EvidenceRecord.status == EvidenceStatus.ACTIVE,
    )
    if prefix:
        query = query.where(EvidenceRecord.metric.like(f"{prefix}%"))
    if basis:
        query = query.where(EvidenceRecord.basis == basis.upper())
    if period_type:
        query = query.where(EvidenceRecord.period_type == period_type.upper())
    rows = session.scalars(
        query.order_by(
            EvidenceRecord.metric, EvidenceRecord.basis, EvidenceRecord.period_end.desc()
        ).limit(min(max(limit, 1), 2000))
    ).all()
    resolutions = {
        r.selected_evidence_id: r
        for r in session.scalars(
            select(EvidenceResolution).where(EvidenceResolution.company_id == company_id)
        )
    }
    return [_metric_out(r, resolutions.get(r.id)) for r in rows]


@router.get("/api/companies/{company_id}/technicals", response_model=list[MetricOut])
def technicals(company_id: uuid.UUID, session: SessionDep) -> list[MetricOut]:
    _company(session, company_id)
    rows = session.scalars(
        select(EvidenceRecord)
        .where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.kind == "TECHNICAL",
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
        .order_by(EvidenceRecord.metric)
    ).all()
    resolutions = {
        r.selected_evidence_id: r
        for r in session.scalars(
            select(EvidenceResolution).where(EvidenceResolution.company_id == company_id)
        )
    }
    return [_metric_out(r, resolutions.get(r.id)) for r in rows]


def _latest_run(session: Session, company_id: uuid.UUID) -> CalculationRun:
    run = session.scalar(
        select(CalculationRun)
        .where(CalculationRun.company_id == company_id)
        .order_by(CalculationRun.id.desc())
        .limit(1)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="No calculation has been run for this company")
    return run


@router.get("/api/companies/{company_id}/calculation-run", response_model=RunOut)
def latest_run(company_id: uuid.UUID, session: SessionDep) -> RunOut:
    _company(session, company_id)
    run = _latest_run(session, company_id)
    return RunOut(
        run_id=run.id,
        engine_version=run.engine_version,
        as_of=run.as_of,
        profile=run.profile,
        calculated=run.calculated,
        gaps=run.gaps,
        summary=run.summary,
    )


@router.get("/api/companies/{company_id}/metrics/gaps", response_model=list[GapOut])
def gaps(
    company_id: uuid.UUID, session: SessionDep, prefix: str | None = None, limit: int = 1000
) -> list[GapOut]:
    """What could NOT be calculated in the latest run, and exactly why."""
    _company(session, company_id)
    run = _latest_run(session, company_id)
    query = select(CalculationGap).where(CalculationGap.run_id == run.id)
    if prefix:
        query = query.where(CalculationGap.metric.like(f"{prefix}%"))
    return [
        GapOut(
            metric=g.metric,
            period_end=g.period_end,
            period_type=g.period_type,
            basis=g.basis,
            reason=g.reason,
            missing=g.missing,
        )
        for g in session.scalars(
            query.order_by(CalculationGap.metric, CalculationGap.period_end).limit(
                min(max(limit, 1), 5000)
            )
        )
    ]


@router.get("/api/companies/{company_id}/price-adjustments", response_model=list[AdjustmentOut])
def price_adjustments(company_id: uuid.UUID, session: SessionDep) -> list[AdjustmentOut]:
    """Split/bonus events used to adjust prices, and unexplained reference-price gaps (not used)."""
    _company(session, company_id)
    securities = {
        s.id: s for s in session.scalars(select(Security).where(Security.company_id == company_id))
    }
    if not securities:
        return []
    rows = session.scalars(
        select(PriceAdjustment)
        .where(PriceAdjustment.security_id.in_(list(securities)))
        .order_by(PriceAdjustment.ex_date)
    )
    return [
        AdjustmentOut(
            security_id=a.security_id,
            symbol=securities[a.security_id].symbol,
            ex_date=a.ex_date,
            factor=str(a.factor),
            kind=a.kind.value,
            origin=a.origin.value,
            status=a.status.value,
            confidence=a.confidence,
            detail=a.detail,
        )
        for a in rows
    ]
