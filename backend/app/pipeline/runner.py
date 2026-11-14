"""Run the stages for a company, and runs of many companies, with checkpoints.

`run_company` is the single place a company moves through the pipeline. The `universe` command, the
API-created runs and the scheduler all come here, so there is one pipeline, not several.

Guarantees:
* each stage is attempted independently; an exception in one is recorded (message, time) and
  the next stage still runs on what exists;
* a stage finished earlier in the same run is not repeated when the run is resumed, and a stage
  whose inputs have not changed decides for itself to do nothing;
* in automated runs a failed stage is retried only after its back-off, never in a tight loop;
* the checkpoint is committed after every stage, so a killed process loses at most the stage that
  was running, which a restart repeats (stages are idempotent).
"""

import logging
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, PipelineRun, PipelineStageState
from app.models.pipeline import (
    CANCELLED,
    CANCELLING,
    COMPLETED,
    FAILED,
    PAUSED,
    QUEUED,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
)
from app.pipeline import isolation as iso
from app.pipeline import state as st
from app.pipeline.stages import GATES, STAGE_FUNCS, Ctx
from app.pipeline.state import STAGES, Outcome

log = logging.getLogger("equityos.pipeline")
ACTIVE = (QUEUED, RUNNING, PAUSED, CANCELLING)
HEARTBEAT_STALE_SECONDS = 600
HEARTBEAT_SECONDS = 60


class Gates:
    """Limits how many threads use one shared resource at once (provider-specific rate control).

    HTTP spacing per host is enforced by the shared PoliteClient; these gates additionally keep
    Upstox to one request stream, and heavy CPU work (PDF reading, validation) to a few threads.
    """

    def __init__(self, workers: int = 1, limits: dict[str, int] | None = None) -> None:
        defaults = {"upstox": 1, "web": max(1, workers), "cpu": max(1, workers // 2 or 1)}
        self._sems = {
            k: threading.BoundedSemaphore(v) for k, v in {**defaults, **(limits or {})}.items()
        }

    @contextmanager
    def hold(self, stage: str):
        sem = self._sems.get(GATES.get(stage, "cpu"))
        if sem is None:
            yield
            return
        with sem:
            yield


@dataclass
class RunResult:
    id: int
    status: str
    summary: dict


@dataclass
class CompanyResult:
    name: str
    steps: dict[str, str] = field(default_factory=dict)  # stage -> one-line message
    statuses: dict[str, str] = field(default_factory=dict)  # stage -> SUCCEEDED/SKIPPED/FAILED
    stopped: bool = False


def run_company(
    session: Session,
    company: Company,
    *,
    stages=STAGES,
    client=None,
    llm=None,
    upstox=None,
    data_dir=None,
    run_id: int | None = None,
    force: bool = False,
    respect_backoff: bool = False,
    gates: Gates | None = None,
    should_stop: Callable[[], bool] | None = None,
    isolation: str = "thread",
    timeouts: dict | None = None,
    child_runners: dict | None = None,
) -> CompanyResult:
    """`isolation="process"` runs the network and document stages in a child process that is stopped
    when the stage's budget (`timeouts`, seconds per stage; see `isolation.budget`) is used up."""
    out = CompanyResult(company.common_name)
    company_id, name = company.id, company.common_name
    for stage in STAGES:  # fixed order regardless of how the caller listed them
        if stage not in stages:
            continue
        if should_stop and should_stop():
            out.stopped = True
            break
        now = datetime.now(UTC)
        prev = st.get(session, company_id, stage)
        due, why = st.should_run(
            prev, now, run_id=run_id, force=force, respect_backoff=respect_backoff
        )
        if not due:
            out.steps[stage] = f"skipped ({why})"
            out.statuses[stage] = SKIPPED
            continue
        before = _Before(prev)  # what the stage last did, read before this attempt overwrites it
        try:
            st.start(session, company_id, stage, run_id, now, force=force)
        except st.StageBusy as busy:
            out.steps[stage] = f"skipped (another run is working on this stage: {busy})"
            out.statuses[stage] = SKIPPED
            continue
        ctx = Ctx(
            session=session,
            company=company,
            client=client,
            llm=llm,
            upstox=upstox,
            data_dir=data_dir,
            now=now,
            force=force,
        )
        seconds = iso.budget(stage, timeouts)
        try:
            with gates.hold(stage) if gates else _nullcontext():
                if isolation == "process" and stage in iso.ISOLATED_STAGES:
                    spec = iso.ChildSpec(
                        stage=stage,
                        company_id=str(company_id),
                        run_id=run_id,
                        force=force,
                        use_llm=llm is not None,
                        data_dir=str(data_dir) if data_dir else None,
                        previous={
                            "status": before.status,
                            "input_fingerprint": before.input_fingerprint,
                            "finished_at": before.finished_at,
                            "detail": before.detail,
                        },
                        runner=(child_runners or {}).get(stage),
                    )
                    outcome = iso.run_in_child(spec, seconds)
                    session.rollback()
                    session.expire_all()  # the child committed its work in its own session
                else:
                    iso.set_statement_timeout(session, seconds)
                    try:
                        outcome = STAGE_FUNCS[stage](ctx, before)
                    finally:
                        iso.set_statement_timeout(session, None)
        except Exception as exc:  # noqa: BLE001 - isolation: record, carry on with the next stage
            session.rollback()
            log.exception("stage %s failed for %s", stage, name)
            outcome = Outcome(
                FAILED, f"error: {type(exc).__name__}", error=f"{type(exc).__name__}: {exc}"
            )
        st.finish(session, company_id, stage, outcome, run_id)
        out.steps[stage] = outcome.message
        out.statuses[stage] = outcome.status
    return out


@contextmanager
def _nullcontext():
    yield


class _Before:
    """The stage's previous result; an attempt that never finished has status RUNNING."""

    def __init__(self, state: PipelineStageState | None) -> None:
        self.status = state.status if state is not None else None
        self.input_fingerprint = state.input_fingerprint if state is not None else None
        self.finished_at = state.finished_at if state is not None else None
        self.detail = state.detail if state is not None else None


# ---- runs -------------------------------------------------------------------------------------


def create_run(
    session: Session,
    *,
    trigger: str = "MANUAL",
    stages=None,
    symbols=None,
    batch_size: int = 10,
    options: dict | None = None,
    created_by: str = "system",
) -> PipelineRun:
    stages = validate_stages(stages or STAGES)
    run = PipelineRun(
        trigger=trigger,
        status=QUEUED,
        control="NONE",
        stages=stages,
        symbols=list(symbols) if symbols else None,
        batch_size=batch_size,
        options=options or {},
        created_by=created_by,
    )
    session.add(run)
    session.commit()
    st.event(session, run.id, None, None, QUEUED, f"run queued ({trigger.lower()})")
    return run


def validate_stages(names) -> list[str]:
    bad = [s for s in names if s not in STAGES]
    if bad:
        raise ValueError(f"unknown stage(s) {bad}; choose from {list(STAGES)}")
    return [s for s in STAGES if s in names]


def active_run(session: Session) -> PipelineRun | None:
    return session.scalar(
        select(PipelineRun)
        .where(PipelineRun.status.in_(ACTIVE))
        .order_by(PipelineRun.id.desc())
        .limit(1)
    )


def request_control(session: Session, run_id: int, action: str) -> PipelineRun:
    """pause | resume | cancel. The worker acts on it between stages of the company in hand."""
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise LookupError(f"no run {run_id}")
    if action == "pause" and run.status in (QUEUED, RUNNING):
        run.control = "PAUSE"
        if run.status == QUEUED:
            run.status, run.control = PAUSED, "NONE"
    elif action == "cancel" and run.status in (QUEUED, RUNNING, PAUSED):
        if run.status in (QUEUED, PAUSED):
            run.status, run.finished_at, run.control = CANCELLED, datetime.now(UTC), "NONE"
        else:
            run.control, run.status = "CANCEL", CANCELLING
    elif action == "resume" and run.status == PAUSED:
        run.status, run.control = QUEUED, "NONE"
    elif action == "resume" and run.status == RUNNING and run.control == "PAUSE":
        run.control = "NONE"
    else:
        raise ValueError(f"cannot {action} a run that is {run.status}")
    session.commit()
    st.event(session, run.id, None, None, run.status, f"operator asked to {action}")
    return run


def claim_next(session: Session) -> PipelineRun | None:
    """The oldest queued run, or a RUNNING run whose worker stopped (no heartbeat), now RUNNING."""
    now = datetime.now(UTC)
    run = session.scalar(
        select(PipelineRun)
        .where(PipelineRun.status == QUEUED)
        .order_by(PipelineRun.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if run is None:
        for r in session.scalars(select(PipelineRun).where(PipelineRun.status == RUNNING)):
            beat = r.heartbeat_at or r.started_at or r.created_at
            if (now - beat).total_seconds() > HEARTBEAT_STALE_SECONDS:
                run = r
                st.event(session, r.id, None, None, RUNNING, "worker stopped; run taken over")
                break
    if run is None:
        return None
    run.status, run.control = RUNNING, "NONE"
    run.started_at = run.started_at or now
    run.heartbeat_at = now
    session.commit()
    return run


def claim_run(session: Session, run_id: int) -> PipelineRun:
    """Mark one specific queued run as RUNNING (used when a process executes the run it created)."""
    run = session.get(PipelineRun, run_id)
    if run is None or run.status != QUEUED:
        raise ValueError(f"run {run_id} is not queued")
    run.status, run.control = RUNNING, "NONE"
    run.started_at = run.started_at or datetime.now(UTC)
    run.heartbeat_at = datetime.now(UTC)
    session.commit()
    return run


def pick_companies(session: Session, run: PipelineRun) -> list[Company]:
    """Fix the run's company list once, so a resumed run covers the same companies."""
    if run.companies:
        ids = run.companies
        found = {str(c.id): c for c in session.scalars(select(Company).where(Company.id.in_(ids)))}
        return [found[i] for i in ids if i in found]
    opts = run.options or {}
    if run.symbols:
        from app.providers.cli import find_company

        companies = [find_company(session, s) for s in run.symbols]
    elif opts.get("select") == "failed":
        companies = failed_companies(session, run.stages, run.batch_size)
    elif opts.get("select") == "auto":
        companies = auto_select(session, run.stages, run.batch_size)
    elif opts.get("select") == "unread-documents":
        from app.pipeline.backlog import unread_documents

        companies = unread_documents(session, run.batch_size)
    elif opts.get("select") == "missing-evidence":
        from app.pipeline.backlog import missing_evidence

        companies = missing_evidence(session, run.batch_size)
    else:
        from app.providers import universe

        companies = universe.pick_batch(session, run.batch_size)
    run.companies = [str(c.id) for c in companies]
    session.commit()
    return companies


def auto_select(session: Session, stages, limit: int) -> list[Company]:
    """Companies that most need work, in priority order, without repeats.

    1 failed stages whose retry is due, 2 stored documents nobody has read, 3 provider facts with no
    validated evidence, 4 the universe batch (never-tried companies first). Backlogs go first so a
    scheduled run keeps draining them instead of revisiting the same alphabetical companies.
    """
    from app.pipeline import backlog
    from app.providers import universe

    chosen: dict = {}
    for source in (
        lambda n: failed_companies(session, stages, n),
        lambda n: backlog.unread_documents(session, n) if "extract" in stages else [],
        lambda n: backlog.missing_evidence(session, n) if "evidence" in stages else [],
        lambda n: universe.pick_batch(session, n),
    ):
        if len(chosen) >= limit:
            break
        for company in source(limit):
            if len(chosen) >= limit:
                break
            chosen.setdefault(company.id, company)
    return list(chosen.values())


def failed_companies(session: Session, stages, limit: int) -> list[Company]:
    """Companies with a FAILED stage whose retry is due, oldest failure first."""
    now = datetime.now(UTC)
    q = (
        select(Company)
        .join(PipelineStageState, PipelineStageState.company_id == Company.id)
        .where(
            PipelineStageState.status == FAILED,
            PipelineStageState.stage.in_(list(stages)),
            (PipelineStageState.next_retry_at.is_(None))
            | (PipelineStageState.next_retry_at <= now),
        )
        .group_by(Company.id)
        .order_by(Company.common_name)
        .limit(limit)
    )
    return list(session.scalars(q))


def _control(session_factory, run_id: int, close: bool = True) -> str:
    s = session_factory()
    try:
        run = s.get(PipelineRun, run_id)
        if run is not None:
            s.refresh(run)
        return run.control if run else "CANCEL"
    finally:
        if close:
            s.close()


def keep_alive(
    session_factory: Callable[[], Session],
    run_id: int,
    stop: threading.Event,
    seconds: float,
    close_sessions: bool = True,
) -> None:
    """Record the run's heartbeat every `seconds` until `stop` is set.

    A company can take longer than the stale-heartbeat limit (a slow site, an OCR run); without
    this the run would look dead and another worker would take it over.
    """
    while not stop.wait(seconds):
        b = session_factory()
        try:
            r = b.get(PipelineRun, run_id)
            if r is not None:
                r.heartbeat_at = datetime.now(UTC)
                b.commit()
        except Exception:  # noqa: BLE001 - monitoring must not stop the run
            log.exception("could not record the run heartbeat")
            b.rollback()
        finally:
            if close_sessions:
                b.close()


def execute(
    session_factory: Callable[[], Session],
    run_id: int,
    *,
    client=None,
    llm=None,
    upstox=None,
    data_dir=None,
    workers: int | None = None,
    on_company: Callable[[CompanyResult], None] | None = None,
    close_sessions: bool = True,
    isolation: str = "thread",
) -> RunResult:
    """Execute a claimed run. Pause / cancel are honoured between stages; resume re-claims it."""
    from concurrent.futures import ThreadPoolExecutor

    s0 = session_factory()
    try:
        run = s0.get(PipelineRun, run_id)
        stages, force = list(run.stages), bool((run.options or {}).get("force"))
        backoff = bool((run.options or {}).get("respect_backoff"))
        workers = workers or int((run.options or {}).get("workers") or 1)
        timeouts = (run.options or {}).get("timeouts") or None
        child_runners = (run.options or {}).get("child_runners") or None
        companies = pick_companies(s0, run)
        ids = [c.id for c in companies]
        st.event(
            s0, run_id, None, None, RUNNING, f"{len(ids)} companies, stages: {', '.join(stages)}"
        )
    finally:
        if close_sessions:
            s0.close()
    gates = Gates(workers)
    stop_flag = {"value": ""}
    lock = threading.Lock()
    results: list[CompanyResult] = []

    def stopping() -> bool:
        if stop_flag["value"]:
            return True
        control = _control(session_factory, run_id, close_sessions)
        if control in ("PAUSE", "CANCEL"):
            stop_flag["value"] = control
            return True
        return False

    def work(company_id) -> CompanyResult | None:
        if stop_flag["value"]:
            return None
        s = session_factory()
        try:
            company = s.get(Company, company_id)
            res = run_company(
                s,
                company,
                stages=stages,
                client=client,
                llm=llm,
                upstox=upstox,
                data_dir=data_dir,
                run_id=run_id,
                force=force,
                respect_backoff=backoff,
                gates=gates,
                should_stop=stopping,
                isolation=isolation,
                timeouts=timeouts,
                child_runners=child_runners,
            )
        except Exception as exc:  # noqa: BLE001 - a company never takes the run down
            s.rollback()
            log.exception("company %s failed", company_id)
            st.event(s, run_id, company_id, None, FAILED, f"{type(exc).__name__}: {exc}")
            res = CompanyResult(str(company_id), {"run": f"error: {type(exc).__name__}"}, {})
        finally:
            if close_sessions:
                s.close()
        if res.stopped and not res.statuses:
            return None  # asked to stop before it began: not counted, picked up on resume
        with lock:
            results.append(res)
            beat = session_factory()
            try:
                r = beat.get(PipelineRun, run_id)
                r.heartbeat_at = datetime.now(UTC)
                beat.commit()
            finally:
                if close_sessions:
                    beat.close()
        if on_company:
            on_company(res)
        return res

    beat_stop = threading.Event()
    beater = threading.Thread(
        target=keep_alive,
        args=(session_factory, run_id, beat_stop, HEARTBEAT_SECONDS, close_sessions),
        daemon=True,
        name=f"run-{run_id}-beat",
    )
    beater.start()
    try:
        if workers <= 1:
            for cid in ids:
                if work(cid) is None:
                    break
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(work, ids))
    finally:
        beat_stop.set()

    s1 = session_factory()
    try:
        run = s1.get(PipelineRun, run_id)
        control = stop_flag["value"] or run.control
        summary = summarise(results)
        run.summary = {**(run.summary or {}), **summary}
        run.heartbeat_at = datetime.now(UTC)
        if control == "CANCEL":
            run.status, run.finished_at = CANCELLED, datetime.now(UTC)
        elif control == "PAUSE":
            run.status = PAUSED
        else:
            run.status, run.finished_at = COMPLETED, datetime.now(UTC)
        run.control = "NONE"
        s1.commit()
        result = RunResult(run.id, run.status, dict(run.summary or {}))
        st.event(s1, run_id, None, None, result.status, _summary_line(summary))
        return result
    finally:
        if close_sessions:
            s1.close()


def summarise(results: list[CompanyResult]) -> dict:
    by_stage: dict[str, dict[str, int]] = {}
    for r in results:
        for stage, status in r.statuses.items():
            by_stage.setdefault(stage, {}).setdefault(status, 0)
            by_stage[stage][status] += 1
    return {"companies_processed": len(results), "stages": by_stage}


def _summary_line(summary: dict) -> str:
    failed = sum(v.get(FAILED, 0) for v in summary["stages"].values())
    ok = sum(v.get(SUCCEEDED, 0) for v in summary["stages"].values())
    return f"{summary['companies_processed']} companies; {ok} stages succeeded, {failed} failed"
