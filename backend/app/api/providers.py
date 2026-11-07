import uuid
from datetime import datetime
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.acquisition.factory import build_client
from app.core.database import get_session
from app.coverage import CoverageReport, build_coverage
from app.models import AcquisitionLog, Company, FetchStatus, Source
from app.providers.base import DataProvider
from app.providers.registry import build_providers, verified_datasets

router = APIRouter(tags=["providers"])


class SourceStatus(BaseModel):
    code: str
    name: str
    authority_tier: int
    terms_note: str | None
    fetches: int
    successes: int
    failures_by_status: dict[str, int]
    last_success_at: datetime | None
    last_failure: str | None  # most recent failure reason, so problems are never hidden
    # Registry fields (None for feed-level sources that have no provider spec)
    configured: bool | None = None
    auth_requirement: str | None = None
    access_method: str | None = None
    rate_limits: str | None = None
    coverage_note: str | None = None
    redistribution_policy: str | None = None
    declared_datasets: list[str] = []
    verified_datasets: list[str] = []  # proven by a recorded successful live retrieval
    unverified_datasets: list[str] = []  # declared but NOT yet proven


@lru_cache
def _providers() -> dict[str, DataProvider]:
    # Specs and configuration only; no network happens when the registry is read.
    return {p.spec.code: p for p in build_providers(build_client(record=False))}


@router.get("/api/providers/status", response_model=list[SourceStatus])
def provider_status(session: Annotated[Session, Depends(get_session)]) -> list[SourceStatus]:
    """Per-source reliability from the fetch audit log, plus declared vs verified datasets."""
    ok_statuses = (FetchStatus.OK, FetchStatus.CACHED)
    out = []
    for source in session.scalars(select(Source).order_by(Source.authority_tier, Source.code)):
        base = select(AcquisitionLog).where(AcquisitionLog.source_code == source.code)
        counts = dict(
            session.execute(
                select(AcquisitionLog.status, func.count())
                .where(AcquisitionLog.source_code == source.code)
                .group_by(AcquisitionLog.status)
            ).all()
        )
        last_ok = session.scalar(
            base.where(AcquisitionLog.status.in_(ok_statuses))
            .order_by(AcquisitionLog.started_at.desc())
            .limit(1)
        )
        last_bad = session.scalar(
            base.where(AcquisitionLog.status.not_in(ok_statuses))
            .order_by(AcquisitionLog.started_at.desc())
            .limit(1)
        )
        provider = _providers().get(source.code)
        declared = sorted(source.declared_datasets or [])
        ok_datasets, _ = verified_datasets(session, source.code)
        out.append(
            SourceStatus(
                code=source.code,
                name=source.name,
                authority_tier=source.authority_tier,
                terms_note=source.terms_note,
                fetches=sum(counts.values()),
                successes=sum(n for s, n in counts.items() if s in ok_statuses),
                failures_by_status={s.value: n for s, n in counts.items() if s not in ok_statuses},
                last_success_at=last_ok.started_at if last_ok else None,
                last_failure=(
                    f"{last_bad.status.value}: {last_bad.error or ''}".strip(": ")
                    if last_bad
                    else None
                ),
                configured=provider.is_configured() if provider else None,
                auth_requirement=source.auth_requirement,
                access_method=source.access_method,
                rate_limits=source.rate_limits,
                coverage_note=source.coverage_note,
                redistribution_policy=source.redistribution_policy,
                declared_datasets=declared,
                verified_datasets=[d for d in declared if d in ok_datasets],
                unverified_datasets=[d for d in declared if d not in ok_datasets],
            )
        )
    return out


@router.get("/api/companies/{company_id}/coverage", response_model=CoverageReport)
def company_coverage(
    company_id: uuid.UUID, session: Annotated[Session, Depends(get_session)]
) -> CoverageReport:
    """Data coverage for a company: what exists, what is missing, from which source, how fresh."""
    if session.get(Company, company_id) is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return build_coverage(session, company_id)
