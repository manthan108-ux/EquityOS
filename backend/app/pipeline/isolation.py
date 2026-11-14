"""Run a stage under a wall-clock budget, in a child process that can be stopped.

A thread cannot be killed, so a stage stuck in a slow download, a hung PDF reader or an OCR run
would hold its worker for as long as the other side takes. Stages that reach outside the database
(`ISOLATED_STAGES`) therefore run in a separate process: the parent waits up to the stage's
budget and then stops the child and its own children (Tesseract), rolls back, and records the
stage as FAILED with `timed_out` in its detail, so it is retried with the usual back-off.
Whatever the child had already committed stays (stages are idempotent and commit per unit of work).

Stages that only use the database (`evidence`, `calculate`, `readiness`, `report`) stay in the
worker's own thread, bounded by a Postgres `statement_timeout` instead.

The child builds its own database session, HTTP client (with the shared cross-process host spacing,
`acquisition/pacing.py`) and provider clients from settings. The parent keeps the provider gates, so
the rate limits are unchanged.
"""

import logging
import multiprocessing
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.pipeline import FAILED
from app.pipeline.state import Outcome

log = logging.getLogger("equityos.pipeline")

# Seconds. Generous: they exist to stop a stage that is stuck, not to hurry one that is working.
DEFAULT_TIMEOUTS = {
    "classify": 120,
    "website": 300,
    "upstox": 300,
    "documents": 900,
    "extract": 1800,
    "enrich": 300,
    "evidence": 600,
    "calculate": 600,
    "readiness": 300,
    "report": 600,
}
ISOLATED_STAGES = frozenset({"classify", "website", "upstox", "documents", "extract", "enrich"})
GRACE_SECONDS = 5.0


def budget(stage: str, overrides: dict | None = None) -> float:
    """Seconds allowed for a stage: run option, then PIPELINE_TIMEOUT_<STAGE>, then the default."""
    if overrides and overrides.get(stage):
        return float(overrides[stage])
    env = os.environ.get(f"PIPELINE_TIMEOUT_{stage.upper()}")
    if env:
        try:
            return float(env)
        except ValueError:
            log.warning("ignoring PIPELINE_TIMEOUT_%s=%r", stage.upper(), env)
    return float(DEFAULT_TIMEOUTS[stage])


@dataclass
class ChildSpec:
    """Everything a child needs, in plain picklable values."""

    stage: str
    company_id: str
    run_id: int | None
    force: bool
    use_llm: bool
    data_dir: str | None
    previous: dict  # status / input_fingerprint / finished_at of the stage's last attempt
    runner: str | None = None  # "module:function" replacing the stage function (tests)


def _timed_out_outcome(stage: str, seconds: float) -> Outcome:
    return Outcome(
        FAILED,
        f"timed out after {seconds:.0f} s; the worker process was stopped",
        detail={"timed_out": True, "budget_seconds": seconds},
        error=f"TimeoutError: {stage} exceeded its {seconds:.0f} s budget",
    )


# ---- the child --------------------------------------------------------------------------------


def _resolve(spec: ChildSpec):
    if spec.runner:
        import importlib

        module, _, name = spec.runner.partition(":")
        return getattr(importlib.import_module(module), name)
    from app.pipeline.stages import STAGE_FUNCS

    return STAGE_FUNCS[spec.stage]


def _build_services(spec: ChildSpec) -> dict:
    """Client, provider and LLM for the stage, built from settings in this process."""
    from app.acquisition.factory import build_client
    from app.llm.providers import default_router
    from app.providers.registry import build_providers

    client = build_client(shared_pacing=True)
    upstox = None
    if spec.stage in ("classify", "upstox"):
        upstox = {p.spec.code: p for p in build_providers(client)}.get("UPSTOX_FUNDAMENTALS")
    llm = None
    if spec.use_llm and spec.stage in ("website", "enrich"):
        llm = default_router()
        if llm is not None and not llm.providers:
            llm = None
    return {"client": client, "upstox": upstox, "llm": llm}


class _Previous:
    def __init__(self, data: dict) -> None:
        self.status = data.get("status")
        self.input_fingerprint = data.get("input_fingerprint")
        self.finished_at = data.get("finished_at")
        self.detail = data.get("detail")


def child_main(spec: ChildSpec, conn) -> None:
    """Entry point of the child process: run the stage, send back an Outcome as a dict."""
    from pathlib import Path as _Path

    from app.core.database import get_engine
    from app.models import Company
    from app.pipeline.stages import Ctx

    reply: dict
    session = None
    services: dict = {}
    try:
        company = None
        if not spec.runner:  # a replacement runner (tests) needs neither database nor services
            session = Session(get_engine())
            company = session.get(Company, uuid.UUID(spec.company_id))
            services = _build_services(spec)
        ctx = Ctx(
            session=session,
            company=company,
            client=services.get("client"),
            llm=services.get("llm"),
            upstox=services.get("upstox"),
            data_dir=_Path(spec.data_dir) if spec.data_dir else None,
            now=datetime.now(UTC),
            force=spec.force,
        )
        outcome = _resolve(spec)(ctx, _Previous(spec.previous))
        reply = {
            "status": outcome.status,
            "message": outcome.message,
            "detail": outcome.detail,
            "fingerprint": outcome.fingerprint,
            "error": outcome.error,
        }
    except BaseException as exc:  # noqa: BLE001 - the parent must always get an answer
        if session is not None:
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
        reply = {
            "status": FAILED,
            "message": f"error: {type(exc).__name__}",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            if services.get("client") is not None:
                services["client"].close()
            if session is not None:
                session.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        conn.send(reply)
    finally:
        conn.close()


# ---- the parent -------------------------------------------------------------------------------


def stop_tree(pid: int) -> None:
    """Stop a process and everything it started (Tesseract, a PDF helper)."""
    try:
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603 - fixed arguments
                ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            import signal

            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        log.exception("could not stop process %s", pid)


def run_in_child(spec: ChildSpec, seconds: float, *, poll: float = 0.2) -> Outcome:
    """Run the stage in a spawned process; stop it when the budget is used."""
    ctx = multiprocessing.get_context("spawn")
    parent_end, child_end = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=child_main, args=(spec, child_end), daemon=True)
    started = time.monotonic()
    proc.start()
    child_end.close()
    reply = None
    try:
        while True:
            left = seconds - (time.monotonic() - started)
            if left <= 0:
                break
            if parent_end.poll(min(poll, left)):
                try:
                    reply = parent_end.recv()
                except EOFError:
                    reply = None
                break
            if not proc.is_alive() and not parent_end.poll(0):
                break  # died without answering
        closed = reply is None and left > 0  # the pipe closed or the child ended: not a timeout
        if closed:
            proc.join(GRACE_SECONDS)  # let a dying process finish exiting before judging it
        if reply is not None:
            proc.join(GRACE_SECONDS)
            return Outcome(
                reply["status"],
                reply.get("message", ""),
                detail=reply.get("detail"),
                fingerprint=reply.get("fingerprint"),
                error=reply.get("error"),
            )
        if proc.is_alive():
            log.warning("stage %s exceeded %.0f s; stopping pid %s", spec.stage, seconds, proc.pid)
            return _timed_out_outcome(spec.stage, seconds)
        code = proc.exitcode
        return Outcome(
            FAILED,
            f"the worker process ended without a result (exit code {code})",
            detail={"exit_code": code},
            error=f"WorkerDied: {spec.stage} exited with code {code}",
        )
    finally:
        if proc.is_alive():
            stop_tree(proc.pid)
            proc.join(GRACE_SECONDS)
        parent_end.close()


def previous_of(state) -> dict:
    if state is None:
        return {}
    return {
        "status": state.status,
        "input_fingerprint": state.input_fingerprint,
        "finished_at": state.finished_at,
        "detail": state.detail,
    }


def set_statement_timeout(session: Session, seconds: float | None) -> None:
    """Bound each database statement of an in-thread stage (Postgres only); None removes it.

    Best effort: Postgres drops a session setting when the transaction is rolled back, so a stage
    that rolls back mid-way continues unbounded. The stages that use this are short and read-mostly.
    """
    try:
        if session.get_bind().dialect.name != "postgresql":
            return
        if seconds is None:
            session.execute(text("RESET statement_timeout"))
        else:
            session.execute(text(f"SET statement_timeout = {int(seconds * 1000)}"))
    except Exception:  # noqa: BLE001 - a missing bound must not block the stage
        log.exception("could not set statement_timeout")
        session.rollback()
