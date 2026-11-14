"""Per-stage wall-clock budgets, child-process isolation and cross-process request spacing."""

import os
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.acquisition.http import ClientConfig, PoliteClient
from app.acquisition.pacing import SharedPacer
from app.models import Company, PipelineEvent, PipelineStageState
from app.models.pipeline import FAILED, SUCCEEDED
from app.pipeline import isolation as iso
from app.pipeline import runner
from app.pipeline.state import Outcome
from app.seeds.loader import seed_reference_data, seed_test_universe

# These run in a spawned child, so they must be importable module-level functions.


def child_ok(ctx, prev) -> Outcome:
    return Outcome(SUCCEEDED, "done in the child", detail={"pid": os.getpid()}, fingerprint="fp")


def child_hang(ctx, prev) -> Outcome:
    time.sleep(120)
    return Outcome(SUCCEEDED, "never reached")


def child_raise(ctx, prev) -> Outcome:
    raise ValueError("bad page")


def child_die(ctx, prev) -> Outcome:
    os._exit(3)


def child_spawn_grandchild(ctx, prev) -> Outcome:
    import subprocess
    import sys

    sub = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])  # noqa: S603
    with open(os.environ["GRANDCHILD_PID_FILE"], "w") as fh:
        fh.write(str(sub.pid))
    time.sleep(120)
    return Outcome(SUCCEEDED, "never reached")


def child_sees_previous(ctx, prev) -> Outcome:
    return Outcome(SUCCEEDED, f"previous={prev.status}/{prev.input_fingerprint}")


@pytest.fixture
def company(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalars(select(Company).order_by(Company.common_name)).first()


def spec(fn: str, stage: str = "documents", **kw) -> iso.ChildSpec:
    base = dict(
        stage=stage,
        company_id="00000000-0000-0000-0000-000000000000",
        run_id=None,
        force=False,
        use_llm=False,
        data_dir=None,
        previous={},
        runner=f"tests.test_pipeline_isolation:{fn}",
    )
    return iso.ChildSpec(**{**base, **kw})


# ---- budgets ----------------------------------------------------------------------------------


def test_every_stage_has_a_default_budget():
    from app.pipeline.state import STAGES

    assert set(iso.DEFAULT_TIMEOUTS) == set(STAGES)
    assert all(iso.budget(s) > 0 for s in STAGES)


def test_budget_precedence_option_then_environment_then_default(monkeypatch):
    assert iso.budget("documents") == iso.DEFAULT_TIMEOUTS["documents"]
    monkeypatch.setenv("PIPELINE_TIMEOUT_DOCUMENTS", "77")
    assert iso.budget("documents") == 77
    assert iso.budget("documents", {"documents": 5}) == 5
    monkeypatch.setenv("PIPELINE_TIMEOUT_DOCUMENTS", "not-a-number")
    assert iso.budget("documents") == iso.DEFAULT_TIMEOUTS["documents"]


def test_network_and_document_stages_are_isolated_and_database_stages_are_not():
    assert {
        "documents",
        "extract",
        "website",
        "upstox",
        "enrich",
        "classify",
    } == iso.ISOLATED_STAGES
    assert not iso.ISOLATED_STAGES & {"evidence", "calculate", "readiness", "report"}


# ---- the child process ------------------------------------------------------------------------


def test_child_returns_the_stage_outcome_from_another_process():
    out = iso.run_in_child(spec("child_ok"), 60)
    assert (out.status, out.message, out.fingerprint) == (SUCCEEDED, "done in the child", "fp")
    assert out.detail["pid"] != os.getpid()


def test_child_receives_the_previous_attempt():
    prev = {"status": SUCCEEDED, "input_fingerprint": "abc", "finished_at": None}
    out = iso.run_in_child(spec("child_sees_previous", previous=prev), 60)
    assert out.message == f"previous={SUCCEEDED}/abc"


def test_a_hung_stage_is_stopped_at_its_budget():
    started = time.monotonic()
    out = iso.run_in_child(spec("child_hang"), 3)
    took = time.monotonic() - started
    assert out.status == FAILED
    assert out.detail == {"timed_out": True, "budget_seconds": 3}
    assert "timed out after 3 s" in out.message
    assert took < 25, "the child must be stopped promptly, not waited for"


def test_an_exception_in_the_child_is_a_failed_outcome_not_a_crash():
    out = iso.run_in_child(spec("child_raise"), 60)
    assert out.status == FAILED
    assert out.error == "ValueError: bad page"


def test_a_child_that_dies_without_an_answer_is_reported_with_its_exit_code():
    out = iso.run_in_child(spec("child_die"), 60)
    assert out.status == FAILED
    assert out.detail == {"exit_code": 3}
    assert "WorkerDied" in out.error


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import subprocess

        res = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in res.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_timeout_also_stops_what_the_child_started(tmp_path, monkeypatch):
    """OCR runs Tesseract as a subprocess: stopping only the child would leave it running."""
    pid_file = tmp_path / "pid.txt"
    monkeypatch.setenv("GRANDCHILD_PID_FILE", str(pid_file))
    out = iso.run_in_child(spec("child_spawn_grandchild"), 6)
    assert out.detail["timed_out"] is True
    assert pid_file.exists(), "the child should have started its helper before the budget ended"
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(grandchild), "the helper process must be stopped with the child"


# ---- inside the pipeline ----------------------------------------------------------------------


def test_run_company_records_a_timeout_and_still_runs_later_stages(session, company):
    res = runner.run_company(
        session,
        company,
        stages=("documents", "readiness"),
        isolation="process",
        timeouts={"documents": 3},
        child_runners={"documents": "tests.test_pipeline_isolation:child_hang"},
    )
    assert res.statuses["documents"] == FAILED
    assert "timed out" in res.steps["documents"]
    assert "readiness" in res.statuses, "a timed-out stage must not stop the next stages"
    row = session.get(PipelineStageState, (company.id, "documents"))
    assert row.status == FAILED
    assert row.detail["timed_out"] is True
    assert row.consecutive_failures == 1 and row.next_retry_at is not None
    events = session.scalars(
        select(PipelineEvent).where(
            PipelineEvent.company_id == company.id, PipelineEvent.stage == "documents"
        )
    ).all()
    assert any("TimeoutError" in e.message for e in events)


def test_a_timed_out_stage_is_retried_after_back_off_in_automated_runs(session, company):
    runner.run_company(
        session,
        company,
        stages=("documents",),
        isolation="process",
        timeouts={"documents": 3},
        child_runners={"documents": "tests.test_pipeline_isolation:child_hang"},
    )
    again = runner.run_company(
        session,
        company,
        stages=("documents",),
        respect_backoff=True,
        isolation="process",
        child_runners={"documents": "tests.test_pipeline_isolation:child_ok"},
    )
    assert "retry not due" in again.steps["documents"]


def test_a_successful_child_stage_is_checkpointed_like_any_other(session, company):
    res = runner.run_company(
        session,
        company,
        stages=("documents",),
        isolation="process",
        child_runners={"documents": "tests.test_pipeline_isolation:child_ok"},
    )
    assert res.statuses["documents"] == SUCCEEDED
    row = session.get(PipelineStageState, (company.id, "documents"))
    assert row.status == SUCCEEDED and row.input_fingerprint == "fp"
    assert row.started_at <= row.finished_at <= datetime.now(UTC)


def test_thread_mode_is_unchanged_and_needs_no_child(session, company, monkeypatch):
    """Existing callers (tests, the universe command) keep the in-process behaviour."""
    from app.pipeline import stages as pst

    monkeypatch.setitem(
        pst.STAGE_FUNCS, "documents", lambda ctx, prev: Outcome(SUCCEEDED, "inline")
    )
    res = runner.run_company(session, company, stages=("documents",))
    assert res.steps["documents"] == "inline"


def test_only_isolated_stages_leave_the_process(session, company, monkeypatch):
    from app.pipeline import stages as pst

    monkeypatch.setitem(
        pst.STAGE_FUNCS, "readiness", lambda ctx, prev: Outcome(SUCCEEDED, "inline")
    )
    res = runner.run_company(
        session,
        company,
        stages=("readiness",),
        isolation="process",
        child_runners={"readiness": "tests.test_pipeline_isolation:child_hang"},
    )
    assert res.steps["readiness"] == "inline", "database-only stages stay in the worker thread"


# ---- request spacing across processes ---------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_pacer_makes_two_processes_wait_their_turn(tmp_path):
    clock = Clock()
    a = SharedPacer(tmp_path, clock=clock)
    b = SharedPacer(tmp_path, clock=clock)  # another process sees the same files
    assert a.reserve("example.com", 2.0) == 0.0
    assert b.reserve("example.com", 2.0) == pytest.approx(2.0)
    assert a.reserve("example.com", 2.0) == pytest.approx(4.0)
    assert a.reserve("other.com", 2.0) == 0.0, "hosts are independent"
    clock.t += 10
    assert b.reserve("example.com", 2.0) == 0.0, "an idle host is free again"


def test_pacer_honours_the_wait_the_caller_already_owes(tmp_path):
    p = SharedPacer(tmp_path, clock=Clock())
    assert p.reserve("example.com", 1.0, earliest=3.0) == pytest.approx(3.0)
    assert p.reserve("example.com", 1.0) == pytest.approx(4.0)


def test_pacer_recovers_from_a_lock_left_by_a_dead_process(tmp_path):
    clock = Clock()
    p = SharedPacer(tmp_path, clock=clock)
    p.reserve("example.com", 1.0)
    _, lock = p._paths("example.com")
    lock.write_text("")
    os.utime(lock, (clock.t - 60, clock.t - 60))
    assert p.reserve("example.com", 1.0) == pytest.approx(1.0)


def test_pacer_that_cannot_write_never_blocks_acquisition(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert SharedPacer(blocker / "sub").reserve("example.com", 1.0) == 0.0


def test_client_waits_for_the_shared_slot(tmp_path):
    import httpx

    slept: list[float] = []
    pacer = SharedPacer(tmp_path, clock=Clock())
    pacer.reserve("example.org", 2.0)  # another process has just used the host
    client = PoliteClient(
        ClientConfig(user_agent="t", min_interval_s=2.0, respect_robots=False),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"ok")),
        sleep=slept.append,
        jitter=lambda: 0.0,
        pacer=pacer.reserve,
    )
    assert client.fetch("https://example.org/a", source="t").ok
    assert any(s == pytest.approx(2.0) for s in slept), slept
