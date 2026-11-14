"""Pipeline monitoring and control.

Reads (runs, stage states, events) need an API key. Anything that starts, stops or changes work
needs an ADMIN key. The API never does the heavy work: it creates or updates run records, and the
worker (`python -m app.pipeline.cli serve`) executes them, so a browser request cannot hold a run.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.models import Company, PipelineEvent, PipelineRun, PipelineStageState, Security
from app.pipeline import backlog, runner, scheduler
from app.pipeline import state as st
from app.pipeline.state import STAGES
from app.research.security import Principal, current_principal

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])
SessionDep = Annotated[Session, Depends(get_session)]
Caller = Annotated[Principal, Depends(current_principal)]
MAX_RUN_COMPANIES = 100


def admin(principal: Caller) -> Principal:
    if not principal.is_admin:
        raise HTTPException(
            status_code=403, detail="starting or changing pipeline work needs an admin key"
        )
    return principal


AdminCaller = Annotated[Principal, Depends(admin)]


class RunIn(BaseModel):
    stages: list[str] | None = None
    symbols: list[str] | None = Field(default=None, max_length=MAX_RUN_COMPANIES)
    batch_size: int = Field(default=10, ge=1, le=MAX_RUN_COMPANIES)
    workers: int = Field(default=1, ge=1, le=scheduler.MAX_WORKERS)
    force: bool = False
    select: str | None = Field(
        default=None, pattern="^(failed|missing-evidence|unread-documents|auto)$"
    )


class ScheduleIn(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    batch_size: int | None = None
    concurrency: int | None = None
    stages: list[str] | None = None
    symbols: list[str] | None = None  # the selected companies the schedule works on
    clear_symbols: bool = False
    allow_universe: bool = False  # schedule batches from the whole universe (not default)


def _run(run: PipelineRun) -> dict:
    return {
        "id": run.id,
        "trigger": run.trigger,
        "status": run.status,
        "control": run.control,
        "stages": run.stages,
        "symbols": run.symbols,
        "batch_size": run.batch_size,
        "options": run.options,
        "companies": len(run.companies or []),
        "summary": run.summary,
        "created_by": run.created_by,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "heartbeat_at": run.heartbeat_at,
    }


def _stage(s: PipelineStageState) -> dict:
    return {
        "status": s.status,
        "message": s.message,
        "error": s.error,
        "attempts": s.attempts,
        "consecutive_failures": s.consecutive_failures,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "next_retry_at": s.next_retry_at,
        "detail": s.detail,
        "last_run_id": s.last_run_id,
    }


def _company_row(session: Session, company: Company, states: dict) -> dict:
    sec = session.scalar(
        select(Security)
        .where(Security.company_id == company.id)
        .order_by(Security.security_code.is_(None).desc())
        .limit(1)
    )
    return {
        "company_id": str(company.id),
        "name": company.common_name,
        "symbol": sec.symbol if sec else None,
        "stages": {stage: _stage(s) for stage, s in states.items()},
    }


@router.get("/overview")
def overview(session: SessionDep, _: Caller) -> dict:
    counts = session.execute(
        select(PipelineStageState.stage, PipelineStageState.status, func.count()).group_by(
            PipelineStageState.stage, PipelineStageState.status
        )
    ).all()
    table: dict[str, dict[str, int]] = {stage: {} for stage in STAGES}
    for stage, status, n in counts:
        table[stage][status] = n
    active = runner.active_run(session)
    latest = session.scalar(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1))
    row = scheduler.get_schedule(session)
    return {
        "stages": list(STAGES),
        "stage_status_counts": table,
        "backlog": backlog.summary(session),
        "active_run": _run(active) if active else None,
        "latest_run": _run(latest) if latest else None,
        "schedule": _schedule(row),
    }


def _schedule(row) -> dict:
    return {
        "enabled": row.enabled,
        "interval_minutes": row.interval_minutes,
        "batch_size": row.batch_size,
        "concurrency": row.concurrency,
        "stages": row.stages,
        "symbols": row.symbols,
        "last_triggered_at": row.last_triggered_at,
        "worker_seen_at": row.worker_seen_at,
        "worker_note": row.worker_note,
    }


@router.get("/runs")
def list_runs(session: SessionDep, _: Caller, limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    rows = session.scalars(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit))
    return [_run(r) for r in rows]


@router.post("/runs", status_code=201)
def create_run(body: RunIn, session: SessionDep, who: AdminCaller) -> dict:
    try:
        stages = runner.validate_stages(body.stages) if body.stages else list(STAGES)
        trigger = "RETRY" if body.select == "failed" else "STAGE" if body.stages else "MANUAL"
        if body.symbols and len(body.symbols) == 1:
            trigger = "SINGLE"
        run = runner.create_run(
            session,
            trigger=trigger,
            stages=stages,
            symbols=body.symbols,
            batch_size=body.batch_size,
            options={
                "workers": body.workers,
                "force": body.force,
                **({"select": body.select} if body.select else {}),
                **({"respect_backoff": True} if body.select == "failed" else {}),
            },
            created_by=who.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _run(run)


@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    session: SessionDep,
    _: Caller,
    events: int = Query(60, ge=0, le=500),
) -> dict:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    ids = [uuid.UUID(i) for i in (run.companies or [])]
    companies = {c.id: c for c in session.scalars(select(Company).where(Company.id.in_(ids)))}
    states = st.states_for(session, ids)
    log = list(
        session.scalars(
            select(PipelineEvent)
            .where(PipelineEvent.run_id == run_id)
            .order_by(PipelineEvent.id.desc())
            .limit(events)
        )
    )
    return {
        **_run(run),
        "company_states": [
            _company_row(session, companies[i], states[i]) for i in ids if i in companies
        ],
        "events": [
            {
                "id": e.id,
                "at": e.at,
                "company_id": str(e.company_id) if e.company_id else None,
                "stage": e.stage,
                "status": e.status,
                "message": e.message,
            }
            for e in reversed(log)
        ],
    }


@router.post("/runs/{run_id}/{action}")
def control(run_id: int, action: str, session: SessionDep, _: AdminCaller) -> dict:
    if action not in ("pause", "resume", "cancel"):
        raise HTTPException(status_code=404, detail="unknown action")
    try:
        return _run(runner.request_control(session, run_id, action))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/companies/{company_id}")
def company_stages(company_id: uuid.UUID, session: SessionDep, _: Caller) -> dict:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="no such company")
    states = st.states_for(session, [company_id])[company_id]
    events = session.scalars(
        select(PipelineEvent)
        .where(PipelineEvent.company_id == company_id)
        .order_by(PipelineEvent.id.desc())
        .limit(40)
    )
    row = _company_row(session, company, states)
    row["events"] = [
        {"at": e.at, "run_id": e.run_id, "stage": e.stage, "status": e.status, "message": e.message}
        for e in events
    ]
    return row


@router.get("/failures")
def failures(session: SessionDep, _: Caller, limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    """Stages currently FAILED, with the error and when they will be retried."""
    rows = session.execute(
        select(PipelineStageState, Company)
        .join(Company, Company.id == PipelineStageState.company_id)
        .where(PipelineStageState.status == "FAILED")
        .order_by(PipelineStageState.finished_at.desc())
        .limit(limit)
    ).all()
    return [
        {"company_id": str(c.id), "name": c.common_name, "stage": s.stage, **_stage(s)}
        for s, c in rows
    ]


@router.get("/metrics")
def pipeline_metrics(
    session: SessionDep, _: Caller, hours: int = Query(168, ge=1, le=24 * 90)
) -> dict:
    """Stage durations, outcome counts and timeouts over the last `hours`, plus recent runs."""
    from app.pipeline import metrics

    return {
        "hours": hours,
        "stages": metrics.stage_metrics(session, hours=hours),
        "runs": metrics.run_metrics(session),
    }


@router.get("/health")
def pipeline_health(session: SessionDep, _: Caller) -> dict:
    """Is the pipeline working: worker alive, runs moving, stages not stuck or failing."""
    from app.pipeline import metrics

    return metrics.health(session)


@router.get("/coverage")
def annual_report_coverage(
    session: SessionDep, _: Caller, symbols: str | None = Query(None, max_length=600)
) -> dict:
    """Annual reports held per target fiscal year, with the reason for each gap (read-only)."""
    from app.acquisition import doc_coverage
    from app.providers.cli import find_company
    from app.seeds.sample_universe import SAMPLE

    wanted = [s.strip() for s in symbols.split(",")] if symbols else [c.symbol for c in SAMPLE]
    rows = []
    for symbol in wanted[:60]:
        try:
            company = find_company(session, symbol)
        except (SystemExit, LookupError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=f"unknown company {symbol!r}") from exc
        rows.append(doc_coverage.company_coverage(session, company))
    return {
        "targets": rows[0]["targets"] if rows else [],
        "company_years_held": sum(r["have"] for r in rows),
        "company_years_total": sum(len(r["targets"]) for r in rows),
        "companies": rows,
    }


@router.get("/integrity")
def integrity_audit(session: SessionDep, _: Caller, company_id: uuid.UUID | None = None) -> dict:
    """Read-only evidence-integrity audit (nothing is written or repaired)."""
    from app.core.config import get_settings, resolve_data_dir
    from app.evidence import integrity

    report = integrity.audit(
        session, company_id=company_id, data_dir=resolve_data_dir(get_settings())
    )
    return integrity.names(session, report).as_dict()


@router.get("/schedule")
def get_schedule(session: SessionDep, _: Caller) -> dict:
    return _schedule(scheduler.get_schedule(session))


@router.put("/schedule")
def put_schedule(body: ScheduleIn, session: SessionDep, _: AdminCaller) -> dict:
    try:
        row = scheduler.update_schedule(session, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _schedule(row)
