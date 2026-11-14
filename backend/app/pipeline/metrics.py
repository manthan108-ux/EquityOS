"""Pipeline monitoring: stage-duration metrics and the alerts that say something needs attention.

Everything here is read from the persisted stage events, stage states, runs and the schedule row, so
it is the same picture whichever process looks (CLI, API, the `/pipeline` page). Nothing is written.

`stage_metrics` answers "how long do stages take and how often do they fail or time out";
`health` answers "is the pipeline working right now": a worker that stopped, a run whose heartbeat
went quiet, a stage stuck past its budget, repeated failures, a schedule that is not producing runs.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Company,
    DocumentExtraction,
    PipelineEvent,
    PipelineRun,
    PipelineSchedule,
    PipelineStageState,
)
from app.models.docintel import ExtractionStatus
from app.models.pipeline import FAILED, QUEUED, RUNNING, SKIPPED, SUCCEEDED
from app.pipeline import isolation as iso
from app.pipeline.runner import HEARTBEAT_STALE_SECONDS
from app.pipeline.state import STAGES

OK, DEGRADED, CRITICAL = "OK", "DEGRADED", "CRITICAL"
WARNING_LEVEL = "WARNING"
WORKER_STALE_SECONDS = 180  # the worker records itself every 30 s
QUEUE_WAIT_SECONDS = 30 * 60
FAILURE_RATE_ALERT = 0.25
MIN_ATTEMPTS_FOR_RATE = 10
REPEATED_FAILURES = 3
STUCK_FACTOR = 2.0  # a stage running longer than this multiple of its budget is stuck


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def stage_metrics(session: Session, *, hours: int = 168, now: datetime | None = None) -> list[dict]:
    """Per stage over the last `hours`: outcomes, timeouts and how long attempts took."""
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=hours)
    seconds = PipelineEvent.duration_ms / 1000.0
    rows = session.execute(
        select(
            PipelineEvent.stage,
            func.count(),
            func.count().filter(PipelineEvent.status == SUCCEEDED),
            func.count().filter(PipelineEvent.status == SKIPPED),
            func.count().filter(PipelineEvent.status == FAILED),
            func.count().filter(PipelineEvent.timed_out.is_(True)),
            func.avg(seconds),
            func.percentile_cont(0.5).within_group(seconds),
            func.percentile_cont(0.95).within_group(seconds),
            func.max(seconds),
            func.max(PipelineEvent.at),
        )
        .where(PipelineEvent.stage.is_not(None), PipelineEvent.at >= since)
        .group_by(PipelineEvent.stage)
    ).all()
    by_stage = {r[0]: r for r in rows}
    out = []
    for stage in STAGES:
        r = by_stage.get(stage)
        attempts = r[1] if r else 0
        failed = r[4] if r else 0
        worked = attempts - (r[3] if r else 0)  # attempts that were not "nothing to do"
        budget = iso.budget(stage)
        p95 = float(r[8]) if r and r[8] is not None else None
        out.append(
            {
                "stage": stage,
                "attempts": attempts,
                "succeeded": r[2] if r else 0,
                "skipped": r[3] if r else 0,
                "failed": failed,
                "timed_out": r[5] if r else 0,
                "failure_rate": round(failed / worked, 4) if worked else None,
                "avg_seconds": round(float(r[6]), 2) if r and r[6] is not None else None,
                "p50_seconds": round(float(r[7]), 2) if r and r[7] is not None else None,
                "p95_seconds": round(p95, 2) if p95 is not None else None,
                "max_seconds": round(float(r[9]), 2) if r and r[9] is not None else None,
                "budget_seconds": budget,
                "p95_share_of_budget": round(p95 / budget, 3) if p95 is not None else None,
                "last_attempt_at": r[10] if r else None,
            }
        )
    return out


def run_metrics(session: Session, *, limit: int = 10) -> list[dict]:
    """The latest runs with how long they took and how many companies and failures they had."""
    out = []
    for run in session.scalars(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)):
        start, end = _aware(run.started_at), _aware(run.finished_at)
        seconds = (end - start).total_seconds() if start and end else None
        stages = (run.summary or {}).get("stages", {})
        out.append(
            {
                "run_id": run.id,
                "trigger": run.trigger,
                "status": run.status,
                "companies": len(run.companies or []),
                "processed": (run.summary or {}).get("companies_processed"),
                "failed_stages": sum(v.get(FAILED, 0) for v in stages.values()),
                "duration_seconds": round(seconds, 1) if seconds is not None else None,
                "started_at": run.started_at,
            }
        )
    return out


def _alert(level: str, code: str, message: str, **where) -> dict:
    return {"level": level, "code": code, "message": message, **where}


def health(session: Session, *, now: datetime | None = None, hours: int = 24) -> dict:
    """Is the pipeline working? A list of alerts, worst first, and one overall status."""
    now = now or datetime.now(UTC)
    alerts: list[dict] = []
    schedule = session.get(PipelineSchedule, 1)

    # worker liveness: its own beat, or the heartbeat of the run it is executing
    runs = list(session.scalars(select(PipelineRun).where(PipelineRun.status == RUNNING)))
    beat = _aware(schedule.worker_seen_at) if schedule else None
    worker_age = (now - beat).total_seconds() if beat else None
    for run in runs:
        seen = _aware(run.heartbeat_at) or _aware(run.started_at) or _aware(run.created_at)
        age = (now - seen).total_seconds()
        if age > HEARTBEAT_STALE_SECONDS:
            alerts.append(
                _alert(
                    CRITICAL,
                    "RUN_STALLED",
                    f"run {run.id} has been silent for {age / 60:.0f} min; its worker has "
                    "probably stopped (a worker restart takes the run over)",
                    run_id=run.id,
                )
            )
    if runs and (worker_age is None or worker_age > WORKER_STALE_SECONDS):
        alerts.append(
            _alert(
                CRITICAL,
                "WORKER_DOWN",
                "a run is marked RUNNING but no worker has reported in the last "
                f"{WORKER_STALE_SECONDS // 60} min",
            )
        )

    queued = list(session.scalars(select(PipelineRun).where(PipelineRun.status == QUEUED)))
    for run in queued:
        waited = (now - _aware(run.created_at)).total_seconds()
        worker_gone = worker_age is None or worker_age > WORKER_STALE_SECONDS
        if waited > QUEUE_WAIT_SECONDS and worker_gone:
            alerts.append(
                _alert(
                    WARNING_LEVEL,
                    "QUEUE_NOT_SERVED",
                    f"run {run.id} has waited {waited / 60:.0f} min and no worker is running",
                    run_id=run.id,
                )
            )

    if schedule and schedule.enabled:
        interval = timedelta(minutes=schedule.interval_minutes)
        last = _aware(schedule.last_triggered_at)
        if worker_age is None or worker_age > WORKER_STALE_SECONDS:
            alerts.append(
                _alert(
                    WARNING_LEVEL,
                    "SCHEDULE_WITHOUT_WORKER",
                    "the schedule is enabled but no worker is running, so nothing will start",
                )
            )
        elif last is not None and now - last > interval * 2 + timedelta(minutes=10):
            alerts.append(
                _alert(
                    WARNING_LEVEL,
                    "SCHEDULE_OVERDUE",
                    f"the last scheduled run started {(now - last).total_seconds() / 3600:.1f} h "
                    f"ago; the interval is {schedule.interval_minutes} min",
                )
            )

    # stages stuck past their budget
    for state, name in session.execute(
        select(PipelineStageState, Company.common_name)
        .join(Company, Company.id == PipelineStageState.company_id)
        .where(PipelineStageState.status == RUNNING)
    ):
        started = _aware(state.started_at)
        if started is None:
            continue
        limit = iso.budget(state.stage) * STUCK_FACTOR + 60
        if (now - started).total_seconds() > limit:
            alerts.append(
                _alert(
                    CRITICAL,
                    "STAGE_STUCK",
                    f"{name}: {state.stage} has been running for "
                    f"{(now - started).total_seconds() / 60:.0f} min "
                    f"(budget {iso.budget(state.stage) / 60:.0f} min)",
                    stage=state.stage,
                    company=name,
                )
            )

    # repeated failures, per stage
    for stage, n in session.execute(
        select(PipelineStageState.stage, func.count())
        .where(
            PipelineStageState.status == FAILED,
            PipelineStageState.consecutive_failures >= REPEATED_FAILURES,
        )
        .group_by(PipelineStageState.stage)
    ):
        alerts.append(
            _alert(
                WARNING_LEVEL,
                "REPEATED_FAILURES",
                f"{n} compan{'y' if n == 1 else 'ies'} failed {stage} {REPEATED_FAILURES} or "
                "more times in a row",
                stage=stage,
            )
        )

    # market prices: a long gap means everything price-based is out of date
    from app.pipeline import prices

    newest = prices.latest_price_date(session)
    if newest is not None and (now.date() - newest).days > prices.STALE_AFTER_DAYS:
        alerts.append(
            _alert(
                WARNING_LEVEL,
                "PRICES_STALE",
                f"the newest stored market price is {newest:%d/%m/%Y}, "
                f"{(now.date() - newest).days} days ago; valuation and indicators are out of date",
            )
        )

    # scanned documents nobody could read yet (OCR is only in the Docker image)
    waiting = session.scalar(
        select(func.count())
        .select_from(DocumentExtraction)
        .where(
            DocumentExtraction.status == ExtractionStatus.NEEDS_OCR,
            DocumentExtraction.superseded_at.is_(None),
        )
    )
    if waiting:
        alerts.append(
            _alert(
                WARNING_LEVEL,
                "OCR_BACKLOG",
                f"{waiting} scanned document(s) are waiting for OCR; run the worker where "
                "Tesseract is installed (the Docker worker) and they are read automatically",
            )
        )

    # failure and timeout rates in the window
    for m in stage_metrics(session, hours=hours, now=now):
        worked = m["attempts"] - m["skipped"]
        if (
            m["failure_rate"] is not None
            and worked >= MIN_ATTEMPTS_FOR_RATE
            and m["failure_rate"] >= FAILURE_RATE_ALERT
        ):
            alerts.append(
                _alert(
                    WARNING_LEVEL,
                    "HIGH_FAILURE_RATE",
                    f"{m['stage']}: {m['failed']} of {worked} attempts failed "
                    f"({m['failure_rate']:.0%}) in the last {hours} h",
                    stage=m["stage"],
                )
            )
        if m["timed_out"]:
            alerts.append(
                _alert(
                    WARNING_LEVEL,
                    "STAGE_TIMEOUTS",
                    f"{m['stage']}: {m['timed_out']} attempt(s) ran out of time in the last "
                    f"{hours} h (budget {m['budget_seconds']:.0f} s)",
                    stage=m["stage"],
                )
            )

    order = {CRITICAL: 0, WARNING_LEVEL: 1}
    alerts.sort(key=lambda a: order.get(a["level"], 2))
    status = CRITICAL if any(a["level"] == CRITICAL for a in alerts) else DEGRADED if alerts else OK
    return {
        "status": status,
        "checked_at": now,
        "alerts": alerts,
        "worker": {
            "last_seen_at": schedule.worker_seen_at if schedule else None,
            "note": schedule.worker_note if schedule else None,
            "alive": worker_age is not None and worker_age <= WORKER_STALE_SECONDS,
        },
    }
