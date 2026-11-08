"""Read-only API for the evidence store, resolved values and validation results (Phase 6)."""

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.evidence.lineage import Node, dependents, lineage_tree
from app.models import (
    Company,
    EvidenceRecord,
    EvidenceResolution,
    ValidationFinding,
    ValidationRun,
)

router = APIRouter(tags=["evidence"])
SessionDep = Annotated[Session, Depends(get_session)]


class EvidenceOut(BaseModel):
    id: int
    evidence_class: str
    kind: str
    metric: str
    dimension: str
    period_end: date | None
    period_type: str | None
    basis: str | None
    value: str | None
    unit: str | None
    original_value: str | None
    original_unit: str | None
    expression: str | None
    statement_text: str | None
    provider: str | None
    authority_tier: int | None
    document_id: uuid.UUID | None
    page: int | None
    source_url: str | None
    status: str
    status_reason: str | None
    created_by: str
    created_at: datetime


class LineageNode(BaseModel):
    evidence_id: int
    evidence_class: str
    metric: str
    value: str | None
    unit: str | None
    period_end: str | None
    status: str
    provider: str | None
    document_id: str | None
    page: int | None
    alias: str | None
    role: str | None
    inputs: list["LineageNode"]


class EvidenceDetail(BaseModel):
    evidence: EvidenceOut
    details: dict | None
    lineage: LineageNode
    used_by: list[int]


class ResolvedOut(BaseModel):
    metric: str
    dimension: str
    period_end: date
    period_type: str
    basis: str
    status: str
    quality: str
    quality_reasons: list[str]
    selected: EvidenceOut | None
    alternatives: list[dict]
    max_difference_pct: str | None
    blocked: bool
    blocked_by: list[str] | None
    note: str | None


class FindingOut(BaseModel):
    rule: str
    severity: str
    message: str
    metric: str | None
    period_end: date | None
    evidence_ids: list[int]
    details: dict | None


class ValidationOut(BaseModel):
    run_id: int
    rules_version: str
    started_at: datetime
    as_of: date
    evidence_checked: int
    errors: int
    warnings: int
    infos: int
    summary: dict | None
    findings: list[FindingOut]


class EvidenceSummary(BaseModel):
    by_class: dict[str, int]
    by_status: dict[str, int]
    resolutions: dict[str, int]
    quality: dict[str, int]
    blocked: int
    last_run_id: int | None
    last_run_errors: int | None
    last_run_warnings: int | None


def _out(r: EvidenceRecord) -> EvidenceOut:
    return EvidenceOut(
        id=r.id,
        evidence_class=r.evidence_class.value,
        kind=r.kind,
        metric=r.metric,
        dimension=r.dimension,
        period_end=r.period_end,
        period_type=r.period_type,
        basis=r.basis,
        value=str(r.value) if r.value is not None else None,
        unit=r.unit,
        original_value=str(r.original_value) if r.original_value is not None else None,
        original_unit=r.original_unit,
        expression=r.expression,
        statement_text=r.statement_text,
        provider=r.provider_code,
        authority_tier=r.authority_tier,
        document_id=r.document_id,
        page=r.source_page,
        source_url=r.source_url,
        status=r.status.value,
        status_reason=r.status_reason,
        created_by=r.created_by,
        created_at=r.created_at,
    )


def _node(n: Node) -> LineageNode:
    return LineageNode(**{**n.__dict__, "inputs": [_node(c) for c in n.inputs]})


def _company(session: Session, company_id: uuid.UUID) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/api/companies/{company_id}/evidence", response_model=list[EvidenceOut])
def list_evidence(
    company_id: uuid.UUID,
    session: SessionDep,
    evidence_class: str | None = None,
    metric: str | None = None,
    status: str | None = "ACTIVE",
    limit: int = 200,
) -> list[EvidenceOut]:
    _company(session, company_id)
    query = select(EvidenceRecord).where(EvidenceRecord.company_id == company_id)
    if evidence_class:
        if evidence_class.upper() not in {"FACT", "CALCULATION", "ASSUMPTION", "INTERPRETATION"}:
            raise HTTPException(status_code=422, detail="unknown evidence_class")
        query = query.where(EvidenceRecord.evidence_class == evidence_class.upper())
    if metric:
        query = query.where(EvidenceRecord.metric == metric)
    if status and status.upper() != "ALL":
        if status.upper() not in {"ACTIVE", "SUPERSEDED", "WITHDRAWN"}:
            raise HTTPException(status_code=422, detail="unknown status")
        query = query.where(EvidenceRecord.status == status.upper())
    query = query.order_by(
        EvidenceRecord.metric, EvidenceRecord.period_end.desc(), EvidenceRecord.id
    )
    return [_out(r) for r in session.scalars(query.limit(min(max(limit, 1), 1000)))]


@router.get("/api/evidence/{evidence_id}", response_model=EvidenceDetail)
def evidence_detail(evidence_id: int, session: SessionDep) -> EvidenceDetail:
    record = session.get(EvidenceRecord, evidence_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    tree = lineage_tree(session, evidence_id)
    return EvidenceDetail(
        evidence=_out(record),
        details=record.details,
        lineage=_node(tree),
        used_by=dependents(session, evidence_id),
    )


@router.get("/api/companies/{company_id}/resolved", response_model=list[ResolvedOut])
def resolved_values(
    company_id: uuid.UUID,
    session: SessionDep,
    metric: str | None = None,
    basis: str | None = None,
    period_type: str | None = None,
    limit: int = 500,
) -> list[ResolvedOut]:
    """The single value per metric/period/basis that a report may use, and why."""
    _company(session, company_id)
    query = select(EvidenceResolution).where(EvidenceResolution.company_id == company_id)
    if metric:
        query = query.where(EvidenceResolution.metric == metric)
    if basis:
        query = query.where(EvidenceResolution.basis == basis.upper())
    if period_type:
        query = query.where(EvidenceResolution.period_type == period_type.upper())
    rows = session.scalars(
        query.order_by(EvidenceResolution.metric, EvidenceResolution.period_end.desc()).limit(
            min(max(limit, 1), 2000)
        )
    )
    out = []
    for r in rows:
        selected = (
            session.get(EvidenceRecord, r.selected_evidence_id) if r.selected_evidence_id else None
        )
        out.append(
            ResolvedOut(
                metric=r.metric,
                dimension=r.dimension,
                period_end=r.period_end,
                period_type=r.period_type,
                basis=r.basis,
                status=r.status.value,
                quality=r.quality.value,
                quality_reasons=list(r.quality_reasons),
                selected=_out(selected) if selected else None,
                alternatives=list(r.alternatives),
                max_difference_pct=str(r.max_difference_pct)
                if r.max_difference_pct is not None
                else None,
                blocked=r.blocked,
                blocked_by=r.blocked_by,
                note=r.note,
            )
        )
    return out


@router.get("/api/companies/{company_id}/validation", response_model=ValidationOut)
def latest_validation(
    company_id: uuid.UUID,
    session: SessionDep,
    severity: str | None = None,
    rule: str | None = None,
    limit: int = 500,
) -> ValidationOut:
    _company(session, company_id)
    run = session.scalar(
        select(ValidationRun)
        .where(ValidationRun.company_id == company_id)
        .order_by(ValidationRun.id.desc())
        .limit(1)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Validation has not been run for this company")
    query = select(ValidationFinding).where(ValidationFinding.run_id == run.id)
    if severity:
        if severity.upper() not in {"ERROR", "WARNING", "INFO"}:
            raise HTTPException(status_code=422, detail="severity must be ERROR, WARNING or INFO")
        query = query.where(ValidationFinding.severity == severity.upper())
    if rule:
        query = query.where(ValidationFinding.rule_code == rule)
    findings = session.scalars(
        query.order_by(
            ValidationFinding.severity, ValidationFinding.rule_code, ValidationFinding.id
        ).limit(min(max(limit, 1), 2000))
    )
    return ValidationOut(
        run_id=run.id,
        rules_version=run.rules_version,
        started_at=run.started_at,
        as_of=run.as_of,
        evidence_checked=run.evidence_checked,
        errors=run.errors,
        warnings=run.warnings,
        infos=run.infos,
        summary=run.summary,
        findings=[
            FindingOut(
                rule=f.rule_code,
                severity=f.severity.value,
                message=f.message,
                metric=f.metric,
                period_end=f.period_end,
                evidence_ids=list(f.evidence_ids or []),
                details=f.details,
            )
            for f in findings
        ],
    )


@router.get("/api/companies/{company_id}/evidence/summary", response_model=EvidenceSummary)
def evidence_summary(company_id: uuid.UUID, session: SessionDep) -> EvidenceSummary:
    _company(session, company_id)

    def counts(column, table, where) -> dict[str, int]:
        rows = session.execute(select(column, func.count()).where(where).group_by(column)).all()
        return {getattr(k, "value", k): n for k, n in rows}

    run = session.scalar(
        select(ValidationRun)
        .where(ValidationRun.company_id == company_id)
        .order_by(ValidationRun.id.desc())
        .limit(1)
    )
    return EvidenceSummary(
        by_class=counts(
            EvidenceRecord.evidence_class, EvidenceRecord, EvidenceRecord.company_id == company_id
        ),
        by_status=counts(
            EvidenceRecord.status, EvidenceRecord, EvidenceRecord.company_id == company_id
        ),
        resolutions=counts(
            EvidenceResolution.status,
            EvidenceResolution,
            EvidenceResolution.company_id == company_id,
        ),
        quality=counts(
            EvidenceResolution.quality,
            EvidenceResolution,
            EvidenceResolution.company_id == company_id,
        ),
        blocked=session.scalar(
            select(func.count()).where(
                EvidenceResolution.company_id == company_id, EvidenceResolution.blocked.is_(True)
            )
        )
        or 0,
        last_run_id=run.id if run else None,
        last_run_errors=run.errors if run else None,
        last_run_warnings=run.warnings if run else None,
    )
