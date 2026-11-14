"""Stage checkpoints: what each stage of each company last did, and whether it is due again.

Every stage attempt is written to `pipeline_stage_states` (the latest state, the thing a restart
resumes from) and `pipeline_events` (the history). These writes are committed on their own, so a
crash in stage N leaves stages 1..N-1 recorded as done and stage N recorded as RUNNING (stale),
and a restart picks up there without repeating the finished stages.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PipelineEvent, PipelineStageState
from app.models.pipeline import FAILED, PENDING, RUNNING, SKIPPED, SUCCEEDED

STAGES = (
    "classify",
    "website",
    "upstox",
    "documents",
    "extract",
    "evidence",
    "calculate",
    "enrich",
    "readiness",
    "report",
)
# minutes to wait after the 1st, 2nd, 3rd ... consecutive failure of a stage; the last repeats
FAILURE_BACKOFF_MINUTES = (15, 60, 360, 1440, 10080)
MAX_MESSAGE = 1000


class StageBusy(Exception):
    """Another run started this stage for this company recently and has not finished it."""


# A RUNNING stage older than this is a crashed attempt, not work in progress.
BUSY_SECONDS = 3600


@dataclass
class Outcome:
    status: str  # SUCCEEDED | SKIPPED | FAILED
    message: str = ""
    detail: dict | None = None
    fingerprint: str | None = None
    error: str | None = None


def get(session: Session, company_id: uuid.UUID, stage: str) -> PipelineStageState | None:
    return session.get(PipelineStageState, (company_id, stage))


def backoff(consecutive_failures: int) -> timedelta:
    index = min(max(consecutive_failures, 1), len(FAILURE_BACKOFF_MINUTES)) - 1
    return timedelta(minutes=FAILURE_BACKOFF_MINUTES[index])


def should_run(
    state: PipelineStageState | None,
    now: datetime,
    *,
    run_id: int | None = None,
    force: bool = False,
    respect_backoff: bool = False,
) -> tuple[bool, str]:
    """Whether a stage is attempted now. The stage itself may still decide it has nothing to do."""
    if force or state is None:
        return True, ""
    if state.status == RUNNING:
        return True, "an earlier attempt did not finish"
    if run_id is not None and state.last_run_id == run_id and state.status in (SUCCEEDED, SKIPPED):
        return False, "already done earlier in this run"
    backing_off = state.next_retry_at and state.next_retry_at > now
    if respect_backoff and state.status == FAILED and backing_off:
        return False, f"retry not due until {state.next_retry_at:%d/%m/%Y %H:%M} UTC"
    return True, ""


def start(
    session: Session,
    company_id: uuid.UUID,
    stage: str,
    run_id: int | None,
    now: datetime,
    *,
    force: bool = False,
) -> PipelineStageState:
    """Begin an attempt. Raises StageBusy if a different run is working on this very stage.

    The row is locked while it is read, so two workers cannot both take the same stage. A RUNNING
    stage of the SAME run (a resume after a crash) or one older than BUSY_SECONDS is taken over.
    """
    state = session.scalar(
        select(PipelineStageState)
        .where(PipelineStageState.company_id == company_id, PipelineStageState.stage == stage)
        .with_for_update()
    )
    if state is not None and state.status == RUNNING and not force:
        age = (now - state.started_at).total_seconds() if state.started_at else BUSY_SECONDS
        other_run = run_id is None or state.last_run_id != run_id
        if other_run and age < BUSY_SECONDS:
            session.rollback()
            raise StageBusy(f"started {age / 60:.0f} min ago by run {state.last_run_id}")
    if state is None:
        state = PipelineStageState(
            company_id=company_id, stage=stage, status=PENDING, attempts=0, consecutive_failures=0
        )
        session.add(state)
    state.status, state.started_at, state.finished_at = RUNNING, now, None
    state.attempts = (state.attempts or 0) + 1
    state.last_run_id = run_id
    try:
        session.commit()
    except IntegrityError as exc:  # two workers created the first row at once
        session.rollback()
        raise StageBusy("started at the same moment by another worker") from exc
    return state


def finish(
    session: Session,
    company_id: uuid.UUID,
    stage: str,
    outcome: Outcome,
    run_id: int | None,
    now: datetime | None = None,
) -> PipelineStageState:
    """Record the end of an attempt (after any rollback, so it always reaches the database)."""
    now = now or datetime.now(UTC)
    state = get(session, company_id, stage)
    if state is None:  # the start record was lost with a rolled-back transaction
        state = PipelineStageState(company_id=company_id, stage=stage, attempts=1)
        session.add(state)
    state.status = outcome.status
    state.message = (outcome.message or "")[:MAX_MESSAGE]
    state.detail = outcome.detail
    state.error = (outcome.error or None) and outcome.error[:MAX_MESSAGE]
    state.finished_at = now
    state.last_run_id = run_id
    if outcome.fingerprint is not None or outcome.status == SUCCEEDED:
        state.input_fingerprint = outcome.fingerprint
    if outcome.status == FAILED:
        state.consecutive_failures = (state.consecutive_failures or 0) + 1
        state.next_retry_at = now + backoff(state.consecutive_failures)
    else:
        state.consecutive_failures = 0
        state.next_retry_at = None
    started = state.started_at
    duration_ms = int((now - started).total_seconds() * 1000) if started is not None else None
    session.add(
        PipelineEvent(
            run_id=run_id,
            company_id=company_id,
            stage=stage,
            status=outcome.status,
            message=(outcome.error or outcome.message or "")[:MAX_MESSAGE],
            at=now,
            duration_ms=max(duration_ms, 0) if duration_ms is not None else None,
            timed_out=bool((outcome.detail or {}).get("timed_out")),
        )
    )
    session.commit()
    return state


def event(
    session: Session,
    run_id: int | None,
    company_id: uuid.UUID | None,
    stage: str | None,
    status: str,
    message: str,
) -> None:
    session.add(
        PipelineEvent(
            run_id=run_id,
            company_id=company_id,
            stage=stage,
            status=status,
            message=message[:MAX_MESSAGE],
        )
    )
    session.commit()


def states_for(session: Session, company_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """{company_id: {stage: state}} for the monitoring views."""
    out: dict[uuid.UUID, dict] = {cid: {} for cid in company_ids}
    if not company_ids:
        return out
    for s in session.scalars(
        select(PipelineStageState).where(PipelineStageState.company_id.in_(company_ids))
    ):
        out[s.company_id][s.stage] = s
    return out
