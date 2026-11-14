"""Checkpoints, resume, control, retry, schedule and multi-company pipelines."""

# ruff: noqa: F811
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    Company,
    Exchange,
    PipelineEvent,
    PipelineRun,
    PipelineSchedule,
    PipelineStageState,
    ProviderRefreshState,
    Security,
)
from app.models.pipeline import (
    CANCELLED,
    COMPLETED,
    FAILED,
    PAUSED,
    QUEUED,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
)
from app.pipeline import runner, scheduler
from app.pipeline import stages as pst
from app.pipeline import state as st
from app.pipeline.state import STAGES, Outcome
from app.providers import universe
from app.providers import upstox_refresh as ur
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.test_enrich import GOOD, Router
from tests.test_universe import ALL_NAMES, PAGES, Upstox
from tests.test_upstox_refresh import FakeUpstox
from tests.test_website_discovery import FakeClient


@pytest.fixture
def cos(session):
    seed_reference_data(session)
    seed_test_universe(session)
    for c in session.scalars(select(Company)):
        c.website = c.investor_relations_url = None
    session.commit()
    return {c.common_name: c for c in session.scalars(select(Company))}


def factory(session):
    return lambda: session


def stage_state(session, company, stage):
    return session.get(PipelineStageState, (company.id, stage))


def new_run(session, **kw):
    kw.setdefault("stages", ("upstox", "evidence", "readiness"))
    run = runner.create_run(session, **kw)
    runner.claim_run(session, run.id)
    return run


def execute(session, run, **kw):
    kw.setdefault("upstox", Upstox())
    kw.setdefault("client", FakeClient(dict(PAGES)))
    return runner.execute(factory(session), run.id, close_sessions=False, **kw)


# ---- checkpoints ----------------------------------------------------------------------------------


def test_each_stage_is_recorded_independently_with_message_and_time(session, cos):
    co = cos["Reliance Industries"]
    out = runner.run_company(
        session, co, stages=("upstox", "evidence", "readiness"), upstox=Upstox(), run_id=None
    )
    assert set(out.statuses) == {"upstox", "evidence", "readiness"}
    for stage in out.statuses:
        s = stage_state(session, co, stage)
        assert (
            s.status in (SUCCEEDED, SKIPPED) and s.finished_at and s.started_at and s.attempts == 1
        )
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.company_id == co.id)).all()
    assert [e.stage for e in events] == ["upstox", "evidence", "readiness"]


def test_a_failing_stage_is_recorded_and_the_later_stages_still_run(session, cos, monkeypatch):
    def boom(ctx, prev):
        raise ConnectionError("provider timed out")

    monkeypatch.setitem(pst.STAGE_FUNCS, "upstox", boom)
    co = cos["TCS"]
    out = runner.run_company(session, co, stages=("upstox", "evidence", "readiness"))
    assert out.statuses == {"upstox": FAILED, "evidence": SUCCEEDED, "readiness": SUCCEEDED}
    s = stage_state(session, co, "upstox")
    assert "ConnectionError: provider timed out" in s.error and s.consecutive_failures == 1
    assert s.next_retry_at > datetime.now(UTC)  # back-off is recorded


def test_a_resumed_run_repeats_only_what_did_not_finish(session, cos, monkeypatch):
    co = cos["Reliance Industries"]
    run = new_run(
        session, symbols=["RELIANCE"], stages=("classify", "upstox", "evidence", "readiness")
    )
    calls = {"upstox": 0, "evidence": 0}
    real_up, real_ev = pst.STAGE_FUNCS["upstox"], pst.STAGE_FUNCS["evidence"]

    def count_up(ctx, prev):
        calls["upstox"] += 1
        return real_up(ctx, prev)

    def flaky_ev(ctx, prev):
        calls["evidence"] += 1
        if calls["evidence"] == 1:
            raise RuntimeError("database connection lost")
        return real_ev(ctx, prev)

    monkeypatch.setitem(pst.STAGE_FUNCS, "upstox", count_up)
    monkeypatch.setitem(pst.STAGE_FUNCS, "evidence", flaky_ev)
    first = runner.run_company(
        session, co, stages=run.stages, upstox=Upstox(), run_id=run.id, force=False
    )
    assert first.statuses["evidence"] == FAILED and first.statuses["upstox"] == SUCCEEDED
    second = runner.run_company(session, co, stages=run.stages, upstox=Upstox(), run_id=run.id)
    assert calls == {"upstox": 1, "evidence": 2}  # upstox not repeated, evidence retried
    assert second.steps["upstox"] == "skipped (already done earlier in this run)"
    assert second.statuses["evidence"] == SUCCEEDED
    s = stage_state(session, co, "evidence")
    assert s.attempts == 2 and s.consecutive_failures == 0 and s.error is None


def test_a_process_killed_mid_stage_resumes_at_that_stage(session, cos, monkeypatch):
    co = cos["Reliance Industries"]

    def killed(ctx, prev):
        raise KeyboardInterrupt  # not an Exception: nothing records the end of the attempt

    real = pst.STAGE_FUNCS["evidence"]
    monkeypatch.setitem(pst.STAGE_FUNCS, "evidence", killed)
    run = new_run(session, stages=("upstox", "evidence"))
    with pytest.raises(KeyboardInterrupt):
        runner.run_company(
            session, co, stages=("upstox", "evidence"), upstox=Upstox(), run_id=run.id
        )
    assert stage_state(session, co, "upstox").status == SUCCEEDED
    assert stage_state(session, co, "evidence").status == RUNNING  # left as it was when killed
    monkeypatch.setitem(pst.STAGE_FUNCS, "evidence", real)
    again = runner.run_company(
        session, co, stages=("upstox", "evidence"), upstox=Upstox(), run_id=run.id
    )
    assert again.steps["upstox"].startswith("skipped (already done")
    assert again.steps["evidence"] == "validated"


def test_back_off_holds_failed_stages_in_automated_runs_but_not_in_manual_ones(
    session, cos, monkeypatch
):
    co = cos["TCS"]
    monkeypatch.setitem(
        pst.STAGE_FUNCS, "evidence", lambda c, p: (_ for _ in ()).throw(OSError("x"))
    )
    runner.run_company(session, co, stages=("evidence",))
    automated = runner.run_company(session, co, stages=("evidence",), respect_backoff=True)
    assert automated.steps["evidence"].startswith("skipped (retry not due until")
    monkeypatch.setitem(pst.STAGE_FUNCS, "evidence", lambda c, p: Outcome(SUCCEEDED, "validated"))
    manual = runner.run_company(session, co, stages=("evidence",))
    assert manual.steps["evidence"] == "validated"


def test_two_runs_cannot_work_on_the_same_stage_of_the_same_company_at_once(session, cos):
    """The live 25-company run collided with an extraction run on one company."""
    co = cos["TCS"]
    first = new_run(session, stages=("evidence",))
    second = new_run(session, stages=("evidence",))
    now = datetime.now(UTC)
    st.start(session, co.id, "evidence", first.id, now)  # run A is mid-stage
    blocked = runner.run_company(session, co, stages=("evidence",), run_id=second.id)
    assert blocked.statuses["evidence"] == SKIPPED
    assert "another run is working on this stage" in blocked.steps["evidence"]
    assert stage_state(session, co, "evidence").status == RUNNING  # A's record is untouched
    assert stage_state(session, co, "evidence").last_run_id == first.id
    # the same run resuming after a crash, or an operator forcing it, may take it over
    same = runner.run_company(session, co, stages=("evidence",), run_id=first.id)
    assert same.steps["evidence"] == "validated"
    st.start(session, co.id, "evidence", first.id, now)
    forced = runner.run_company(session, co, stages=("evidence",), run_id=second.id, force=True)
    assert forced.steps["evidence"] == "validated"


def test_a_stage_left_running_for_over_an_hour_is_a_crash_not_work_in_progress(session, cos):
    co = cos["TCS"]
    a, b = new_run(session, stages=("evidence",)), new_run(session, stages=("evidence",))
    old = datetime.now(UTC) - timedelta(seconds=st.BUSY_SECONDS + 60)
    st.start(session, co.id, "evidence", a.id, old)
    out = runner.run_company(session, co, stages=("evidence",), run_id=b.id)
    assert out.steps["evidence"] == "validated"


def test_state_helpers_compute_backoff_and_due():
    assert st.backoff(1) == timedelta(minutes=15) and st.backoff(99) == timedelta(days=7)
    assert st.should_run(None, datetime.now(UTC)) == (True, "")


# ---- runs: execution, control, resume -----------------------------------------------------------------


def test_a_run_covers_a_fixed_batch_and_reports_per_stage_counts(session, cos):
    run = new_run(session, batch_size=3)
    result = execute(session, run)
    session.expire_all()
    stored = session.get(PipelineRun, run.id)
    assert result.status == COMPLETED and len(stored.companies) == 3
    assert result.summary["companies_processed"] == 3
    assert result.summary["stages"]["upstox"] == {SUCCEEDED: 3}
    assert stored.finished_at is not None


def test_one_companys_crash_does_not_stop_the_run(session, cos, monkeypatch):
    victim = sorted(cos)[1]
    real = pst.STAGE_FUNCS["readiness"]

    def sometimes(ctx, prev):
        if ctx.company.common_name == victim:
            raise ValueError("bad data for one company")
        return real(ctx, prev)

    monkeypatch.setitem(pst.STAGE_FUNCS, "readiness", sometimes)
    run = new_run(session, batch_size=3)
    result = execute(session, run)
    assert result.summary["stages"]["readiness"] == {SUCCEEDED: 2, FAILED: 1}
    assert result.status == COMPLETED


def test_pause_stops_between_companies_and_resume_finishes_without_repeating(session, cos):
    run = new_run(session, batch_size=4)
    up = Upstox()
    seen = []

    def pause_after_first(res):
        seen.append(res.name)
        if len(seen) == 1:
            runner.request_control(session, run.id, "pause")

    first = execute(session, run, upstox=up, on_company=pause_after_first)
    assert first.status == PAUSED and len(seen) == 1
    done_first = len(up.acquired)
    runner.request_control(session, run.id, "resume")
    claimed = runner.claim_next(session)
    assert claimed.id == run.id and claimed.status == RUNNING
    second = execute(session, run, upstox=up)
    assert second.status == COMPLETED
    assert len(up.acquired) > done_first
    names = {n for (n, _d) in up.acquired}
    assert len(names) == 4
    per_company = session.scalar(
        select(func.count())
        .select_from(PipelineStageState)
        .where(PipelineStageState.stage == "upstox")
    )
    assert per_company == 4
    assert (
        max(
            stage_state(session, c, "upstox").attempts
            for c in cos.values()
            if stage_state(session, c, "upstox")
        )
        == 1
    )


def test_cancel_ends_the_run_and_leaves_checkpoints_for_a_new_run(session, cos):
    run = new_run(session, batch_size=4)
    seen = []

    def cancel_after_first(res):
        seen.append(res)
        if len(seen) == 1:
            runner.request_control(session, run.id, "cancel")

    result = execute(session, run, on_company=cancel_after_first)
    assert result.status == CANCELLED and len(seen) == 1
    done = session.scalar(
        select(func.count())
        .select_from(PipelineStageState)
        .where(PipelineStageState.stage == "upstox", PipelineStageState.status == SUCCEEDED)
    )
    assert done == 1  # the finished company keeps its checkpoint
    with pytest.raises(ValueError, match="cannot cancel"):
        runner.request_control(session, run.id, "cancel")


def test_queued_runs_can_be_paused_and_cancelled_before_they_start(session, cos):
    a = runner.create_run(session, stages=("upstox",))
    assert runner.request_control(session, a.id, "pause").status == PAUSED
    assert runner.request_control(session, a.id, "resume").status == QUEUED
    assert runner.request_control(session, a.id, "cancel").status == CANCELLED
    with pytest.raises(LookupError):
        runner.request_control(session, 987654, "pause")


def test_a_run_whose_worker_died_is_taken_over_by_heartbeat(session, cos):
    run = new_run(session, batch_size=2)
    row = session.get(PipelineRun, run.id)
    assert runner.claim_next(session) is None  # a live run is not taken over
    row.heartbeat_at = datetime.now(UTC) - timedelta(seconds=runner.HEARTBEAT_STALE_SECONDS + 5)
    session.commit()
    taken = runner.claim_next(session)
    assert taken is not None and taken.id == run.id


def test_unknown_stages_are_refused(session):
    with pytest.raises(ValueError, match="unknown stage"):
        runner.create_run(session, stages=("upstox", "teleport"))


def test_retry_failed_selects_only_companies_whose_retry_is_due(session, cos, monkeypatch):
    monkeypatch.setitem(
        pst.STAGE_FUNCS, "evidence", lambda c, p: (_ for _ in ()).throw(OSError("x"))
    )
    a, b = cos["TCS"], cos["Infosys"]
    runner.run_company(session, a, stages=("evidence",))
    runner.run_company(session, b, stages=("evidence",))
    stage_state(session, b, "evidence").next_retry_at = datetime.now(UTC) + timedelta(hours=3)
    session.commit()
    assert runner.failed_companies(session, ("evidence",), 10) == []  # both are still backing off
    stage_state(session, a, "evidence").next_retry_at = datetime.now(UTC) - timedelta(minutes=1)
    session.commit()
    assert runner.failed_companies(session, ("evidence",), 10) == [a]
    monkeypatch.setitem(pst.STAGE_FUNCS, "evidence", lambda c, p: Outcome(SUCCEEDED, "validated"))
    run = new_run(session, stages=("evidence",), options={"select": "failed"}, batch_size=10)
    result = execute(session, run)
    assert result.summary["stages"]["evidence"] == {SUCCEEDED: 1}
    assert stage_state(session, a, "evidence").status == SUCCEEDED
    assert stage_state(session, b, "evidence").status == FAILED  # still waiting for its back-off


# ---- schedule ---------------------------------------------------------------------------------------


def test_a_disabled_schedule_queues_nothing_and_an_enabled_one_queues_once_per_interval(session):
    assert scheduler.tick(session) is None
    scheduler.update_schedule(
        session,
        enabled=True,
        interval_minutes=60,
        batch_size=7,
        concurrency=3,
        allow_universe=True,  # a schedule needs selected companies unless this is asked for
    )
    run = scheduler.tick(session)
    assert run.trigger == "SCHEDULED" and run.batch_size == 7
    assert run.options == {"workers": 3, "respect_backoff": True, "select": "auto"}
    assert scheduler.tick(session) is None  # a run is already active
    session.get(PipelineRun, run.id).status = COMPLETED
    session.commit()
    assert scheduler.tick(session) is None  # not due yet
    session.get(PipelineSchedule, 1).last_triggered_at = datetime.now(UTC) - timedelta(minutes=61)
    session.commit()
    assert scheduler.tick(session) is not None


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"interval_minutes": 1}, "at least"),
        ({"batch_size": 0}, "batch size"),
        ({"concurrency": 99}, "concurrency"),
        ({"stages": ["nope"]}, "unknown stage"),
    ],
)
def test_schedule_settings_are_bounded(session, kw, message):
    with pytest.raises(ValueError, match=message):
        scheduler.update_schedule(session, **kw)


def test_the_worker_executes_a_queued_run_then_stops_when_asked(session, cos):
    runner.create_run(session, stages=("upstox", "evidence"), batch_size=2)
    context = lambda: {"upstox": Upstox(), "client": FakeClient({})}  # noqa: E731

    class Keep:  # the worker closes its session; the test session must survive
        def __call__(self):
            return session

    def keep_open():
        return session

    real_close = session.close
    session.close = lambda: None
    try:
        n = scheduler.serve(keep_open, context, once=True)
    finally:
        session.close = real_close
    assert n == 1
    assert session.scalar(select(PipelineRun).order_by(PipelineRun.id.desc())).status == COMPLETED


def test_the_worker_does_not_touch_a_run_after_closing_its_session(session, cos):
    """Regression: the worker read `run.id` after `session.close()` and crashed on a real database."""
    runner.create_run(session, stages=("evidence",), batch_size=1)
    closed = []

    class Closing:
        """A session handle whose close() really detaches objects, as in production."""

        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def close(self):
            closed.append(1)
            self.inner.expunge_all()

    n = scheduler.serve(lambda: Closing(session), lambda: {"client": FakeClient({})}, once=True)
    assert n == 1 and closed
    assert session.scalar(select(PipelineRun).order_by(PipelineRun.id.desc())).status == COMPLETED


def test_auto_selection_drains_backlogs_before_revisiting_the_universe(session, cos, monkeypatch):
    from app.pipeline import backlog
    from tests.research_helpers import FY26  # noqa: F401
    from tests.test_evidence_db import fact

    a, b, c = cos["TCS"], cos["Infosys"], cos["HDFC Bank"]
    fact(
        session, a, "INCOME", "net_profit", 5, provider="UPSTOX_FUNDAMENTALS"
    )  # facts, no evidence
    session.commit()
    monkeypatch.setitem(
        pst.STAGE_FUNCS, "readiness", lambda c_, p: (_ for _ in ()).throw(OSError("x"))
    )
    runner.run_company(session, b, stages=("readiness",))  # a failed stage whose retry is due
    stage_state(session, b, "readiness").next_retry_at = datetime.now(UTC) - timedelta(minutes=1)
    session.commit()
    picked = runner.auto_select(session, ("readiness", "evidence", "extract"), 3)
    assert [x.common_name for x in picked][:2] == [
        "Infosys",
        "TCS",
    ]  # failed first, then no evidence
    assert (
        len({x.id for x in picked}) == len(picked) == 3
    )  # no repeats; the universe fills the rest
    assert c not in picked[:2]
    assert a in backlog.missing_evidence(session)


# ---- bounded concurrency ---------------------------------------------------------------------------


def test_provider_gates_bound_how_many_threads_use_a_resource():
    gates = runner.Gates(workers=4)
    active, peak, lock = 0, 0, threading.Lock()

    def use(stage):
        nonlocal active, peak
        with gates.hold(stage):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = [threading.Thread(target=use, args=("upstox",)) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == 1  # Upstox is one request stream however many workers there are
    peak = active = 0
    threads = [threading.Thread(target=use, args=("website",)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert 1 < peak <= 4


# ---- whole pipelines --------------------------------------------------------------------------------


def test_a_five_company_pipeline_runs_every_stage_and_isolates_the_unavailable_ones(session, cos):
    class MixedUpstox(Upstox):
        def acquire(self, session, ref, dataset):
            if ref.common_name == "Infosys":
                raise RuntimeError("provider exploded for one company")
            return super().acquire(session, ref, dataset)

    run = new_run(session, stages=STAGES, batch_size=5)
    result = execute(session, run, upstox=MixedUpstox(), llm=Router(GOOD))
    assert result.status == COMPLETED and result.summary["companies_processed"] == 5
    stages = result.summary["stages"]
    assert set(stages) == set(STAGES)
    assert stages["upstox"].get(FAILED, 0) == 1 and stages["upstox"][SUCCEEDED] == 4
    assert stages["evidence"] == {SUCCEEDED: 5}  # one company's failure did not stop the others
    assert stages["readiness"] == {SUCCEEDED: 5}
    reliance = cos["Reliance Industries"]
    assert reliance.website  # discovery verified the one site the fake web can serve
    assert (
        session.get(ProviderRefreshState, ("UPSTOX_FUNDAMENTALS", reliance.id)).status
        == "REFRESHED"
    )
    snap = stage_state(session, reliance, "readiness").detail
    assert snap["eligibility"] in ("AUTOMATIC", "ASSISTED", "BLOCKED") and snap["counts"]
    rerun = new_run(session, stages=STAGES, batch_size=5)
    again = execute(session, rerun, upstox=Upstox(), llm=Router(GOOD))
    assert again.summary["stages"]["upstox"].get(SUCCEEDED, 0) == 1  # only the one that failed


def synthetic(session, n):
    nse = session.scalar(select(Exchange).where(Exchange.code == "NSE"))
    made = []
    for i in range(n):
        c = Company(
            legal_name=f"Synthetic Works {i:02d} Limited",
            common_name=f"Synthetic Works {i:02d}",
            master_data_source="test",
            issuer_key=f"S{i:06d}",
        )
        session.add(c)
        session.flush()
        session.add(
            Security(company_id=c.id, exchange_id=nse.id, symbol=f"SYN{i:02d}", isin=f"INE{i:08d}1")
        )
        made.append(c)
    session.commit()
    return made


class ManyUpstox(Upstox):
    """Names every company in its profile: the seeded ones and the synthetic ones."""

    def _get(self, isin, endpoint, query=""):
        digits = isin[3:11]
        if digits.isdigit():
            self.profile_text = (
                f"Synthetic Works {int(digits):02d} Limited is an India-based maker."
            )
        else:
            self.profile_text = ALL_NAMES
        return FakeUpstox._get(self, isin, endpoint, query)


def test_a_twenty_five_company_pipeline_completes_resumes_and_repeats_nothing(session, cos):
    synthetic(session, 25 - len(cos))  # plus the seeded companies: 25 in all
    up = ManyUpstox()
    stages = ("classify", "upstox", "extract", "evidence", "readiness", "report")
    run = new_run(session, stages=stages, batch_size=25, options={"workers": 1})
    interrupted = []

    def stop_midway(res):
        interrupted.append(res)
        if len(interrupted) == 10:
            runner.request_control(session, run.id, "pause")

    first = execute(session, run, upstox=up, on_company=stop_midway)
    assert first.status == PAUSED and first.summary["companies_processed"] == 10
    runner.request_control(session, run.id, "resume")
    runner.claim_next(session)
    second = execute(session, run, upstox=up)
    assert second.status == COMPLETED
    states = session.scalars(select(PipelineStageState)).all()
    assert len({s.company_id for s in states}) == 25
    per_stage = {}
    for s in states:
        per_stage.setdefault(s.stage, []).append(s)
    for stage in stages:
        assert len(per_stage[stage]) == 25 and all(s.attempts == 1 for s in per_stage[stage]), stage
    assert all(s.status == SUCCEEDED for s in per_stage["upstox"] + per_stage["readiness"])
    refreshed = {n for (n, d) in up.acquired}
    assert len(refreshed) == 25  # every company reached exactly once, none twice
    assert len(up.acquired) == 25 * len(ur.DATASETS)


def test_the_legacy_universe_command_uses_the_same_pipeline(session, cos):
    out = universe.run_company(
        session,
        cos["Reliance Industries"],
        steps=universe.STEPS,
        client=FakeClient(dict(PAGES)),
        llm=Router(GOOD),
        upstox=Upstox(),
    )
    assert list(out.steps) == list(STAGES)
    assert stage_state(session, cos["Reliance Industries"], "extract") is not None
