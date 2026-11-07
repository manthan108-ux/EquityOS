from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.models import AcquisitionLog, FetchStatus, Source

router = APIRouter(prefix="/api/providers", tags=["providers"])


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


@router.get("/status", response_model=list[SourceStatus])
def provider_status(session: Annotated[Session, Depends(get_session)]) -> list[SourceStatus]:
    """Per-source reliability, computed from the fetch audit log (every fetch is logged)."""
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
            )
        )
    return out
