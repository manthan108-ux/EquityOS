"""Scheduled incremental runs and the worker loop.

The schedule is one editable row (`pipeline_schedule`). The worker polls: when the schedule is due
and nothing is active it queues a SCHEDULED run for the next batch of companies, then executes
queued runs one at a time. Runs created from the API or CLI are executed by the same loop, so the
API never does the heavy work itself and a restart loses nothing (an interrupted run is taken over
when its heartbeat stops).
"""

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import PipelineSchedule
from app.pipeline import runner
from app.pipeline.state import STAGES

log = logging.getLogger("equityos.pipeline")
MIN_INTERVAL_MINUTES = 5
MAX_BATCH = 100
MAX_WORKERS = 8
MAX_SELECTED = 100
WORKER_BEAT_SECONDS = 30
# the default schedule is deliberately gentle: a small batch, two threads
DEFAULT = {"enabled": False, "interval_minutes": 360, "batch_size": 10, "concurrency": 2}


def get_schedule(session: Session) -> PipelineSchedule:
    row = session.get(PipelineSchedule, 1)
    if row is None:
        row = PipelineSchedule(id=1, stages=None, **DEFAULT)  # None = every stage, always
        session.add(row)
        session.commit()
    return row


def update_schedule(
    session: Session,
    *,
    enabled: bool | None = None,
    interval_minutes: int | None = None,
    batch_size: int | None = None,
    concurrency: int | None = None,
    stages: list[str] | None = None,
    symbols: list[str] | None = None,
    clear_symbols: bool = False,
    allow_universe: bool = False,
) -> PipelineSchedule:
    """Edit the schedule. A scheduled run works on the SELECTED companies (`symbols`); running it
    over the whole universe has to be asked for (`allow_universe`) and is not the default."""
    row = get_schedule(session)
    if symbols is not None:
        row.symbols = _validated_symbols(session, symbols)
    elif clear_symbols:
        row.symbols = None
    if interval_minutes is not None:
        if interval_minutes < MIN_INTERVAL_MINUTES:
            raise ValueError(f"the interval must be at least {MIN_INTERVAL_MINUTES} minutes")
        row.interval_minutes = interval_minutes
    if batch_size is not None:
        if not 1 <= batch_size <= MAX_BATCH:
            raise ValueError(f"the batch size must be 1 to {MAX_BATCH}")
        row.batch_size = batch_size
    if concurrency is not None:
        if not 1 <= concurrency <= MAX_WORKERS:
            raise ValueError(f"concurrency must be 1 to {MAX_WORKERS}")
        row.concurrency = concurrency
    if stages is not None:
        # an empty list means "every stage": a stored list would go stale when a stage is added
        row.stages = runner.validate_stages(stages) if stages else None
    if enabled is not None:
        row.enabled = enabled
    if row.enabled and not row.symbols and not allow_universe:
        session.rollback()
        raise ValueError(
            "choose the companies the schedule works on (symbols), or allow the whole universe"
        )
    session.commit()
    return row


def _validated_symbols(session: Session, symbols: list[str]) -> list[str]:
    from app.providers.cli import find_company

    out: list[str] = []
    for raw in symbols:
        symbol = raw.strip()
        if not symbol:
            continue
        try:
            find_company(session, symbol)
        except (SystemExit, LookupError, ValueError) as exc:
            raise ValueError(f"unknown company {symbol!r}") from exc
        if symbol.upper() not in out:
            out.append(symbol.upper())
    if not out:
        raise ValueError("no company symbols given")
    if len(out) > MAX_SELECTED:
        raise ValueError(f"a schedule works on at most {MAX_SELECTED} selected companies")
    return out


def tick(session: Session, now: datetime | None = None):
    """Queue a scheduled run if one is due. Returns the new run or None."""
    now = now or datetime.now(UTC)
    row = get_schedule(session)
    if not row.enabled or runner.active_run(session) is not None:
        return None
    due = row.last_triggered_at is None or now - row.last_triggered_at >= timedelta(
        minutes=row.interval_minutes
    )
    if not due:
        return None
    selected = list(row.symbols) if row.symbols else None
    options = {"workers": row.concurrency, "respect_backoff": True}
    if not selected:
        options["select"] = "auto"
    run = runner.create_run(
        session,
        trigger="SCHEDULED",
        stages=row.stages or list(STAGES),
        symbols=selected,
        batch_size=len(selected) if selected else row.batch_size,
        options=options,
        created_by="scheduler",
    )
    row.last_triggered_at = now
    session.commit()
    return run


class WorkerBeat:
    """Records that a worker is alive, from its own thread, however long a run takes."""

    def __init__(self, session_factory: Callable[[], Session], seconds: float) -> None:
        self._factory, self._seconds = session_factory, seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="worker-beat")
        self.note = f"{socket.gethostname()}:{os.getpid()}"

    def beat(self) -> None:
        s = self._factory()
        try:
            row = get_schedule(s)
            row.worker_seen_at, row.worker_note = datetime.now(UTC), self.note
            s.commit()
        except Exception:  # noqa: BLE001 - monitoring must never stop the worker
            log.exception("could not record the worker heartbeat")
            s.rollback()
        finally:
            s.close()

    def _loop(self) -> None:
        while not self._stop.wait(self._seconds):
            self.beat()

    def start(self) -> None:
        self.beat()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def serve(
    session_factory: Callable[[], Session],
    build_context: Callable[[], dict],
    *,
    poll_seconds: int = 30,
    once: bool = False,
    stop: Callable[[], bool] = lambda: False,
    isolation: str = "thread",
    beat_seconds: float = WORKER_BEAT_SECONDS,
) -> int:
    """Worker loop: schedule, claim, execute. `build_context()` returns client/llm/upstox/closer.

    A failure in one iteration (database briefly unreachable, a context that cannot be built) is
    logged and the loop carries on after the poll interval; only `stop` or Ctrl+C ends it.
    """
    executed = 0
    beat = WorkerBeat(session_factory, beat_seconds)
    beat.start()
    try:
        while not stop():
            run_id = None
            try:
                s = session_factory()
                try:
                    tick(s)
                    claimed = runner.claim_next(s)
                    run_id = claimed.id if claimed is not None else None  # before the close
                finally:
                    s.close()
            except Exception:  # noqa: BLE001
                log.exception("worker could not poll for work")
                if once:
                    break
                time.sleep(poll_seconds)
                continue
            if run_id is not None:
                ctx: dict = {}
                try:
                    ctx = build_context()
                    runner.execute(
                        session_factory,
                        run_id,
                        client=ctx.get("client"),
                        llm=ctx.get("llm"),
                        upstox=ctx.get("upstox"),
                        data_dir=ctx.get("data_dir"),
                        isolation=isolation,
                    )
                    executed += 1
                except Exception:  # noqa: BLE001 - a broken run must not kill the worker
                    log.exception("run %s crashed", run_id)
                finally:
                    closer = ctx.get("close")
                    if closer:
                        closer()
                continue
            if once:
                break
            time.sleep(poll_seconds)
    finally:
        beat.stop()
    return executed
