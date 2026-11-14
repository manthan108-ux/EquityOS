"""Stage-duration metrics, health alerts, schedule scope, the worker heartbeat and a resilient worker."""

# ruff: noqa: F811
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    Company,
    PipelineEvent,
    PipelineRun,
    PipelineSchedule,
    PipelineStageState,
)
from app.models.pipeline import COMPLETED, FAILED, QUEUED, RUNNING, SKIPPED, SUCCEEDED
from app.pipeline import metrics, runner, scheduler
from app.pipeline import state as st
from app.pipeline.state import Outcome
from app.seeds.loader import seed_reference_data, seed_test_universe

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def company(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalars(select(Company).order_by(Company.common_name)).first()


@pytest.fixture
def sym(session, company):
    """A real symbol in the seeded universe."""
    from app.models import Security

    return session.scalar(select(Security.symbol).where(Security.company_id == company.id))


def attempt(session, company, stage, status, seconds, *, at=NOW, timed_out=False, run_id=None):
    """Record one stage attempt the way the pipeline does, with a chosen duration."""
    started = at - timedelta(seconds=seconds)
    st.start(session, company.id, stage, run_id, started)
    detail = {"timed_out": True} if timed_out else None
    st.finish(session, company.id, stage, Outcome(status, "x", detail=detail), run_id, now=at)


# ---- durations are recorded -------------------------------------------------------------------


def test_every_attempt_records_its_duration_and_timeout_flag(session, company):
    attempt(session, company, "evidence", SUCCEEDED, 12.5)
    attempt(session, company, "documents", FAILED, 30, timed_out=True)
    events = {
        e.stage: e
        for e in session.scalars(
            select(PipelineEvent).where(PipelineEvent.company_id == company.id)
        )
    }
    assert events["evidence"].duration_ms == 12500 and events["evidence"].timed_out is False
    assert events["documents"].duration_ms == 30000 and events["documents"].timed_out is True


# ---- metrics ----------------------------------------------------------------------------------


def test_stage_metrics_summarise_outcomes_and_durations(session, company):
    for seconds in (1, 2, 3, 4, 10):
        attempt(session, company, "evidence", SUCCEEDED, seconds)
    attempt(session, company, "evidence", FAILED, 5)
    attempt(session, company, "evidence", SKIPPED, 0.1)
    rows = {m["stage"]: m for m in metrics.stage_metrics(session, now=NOW)}
    m = rows["evidence"]
    assert (m["attempts"], m["succeeded"], m["failed"], m["skipped"]) == (7, 5, 1, 1)
    assert m["failure_rate"] == pytest.approx(1 / 6, abs=1e-3)  # skipped attempts did no work
    assert m["max_seconds"] == 10 and m["p50_seconds"] == pytest.approx(3.0, abs=0.6)
    assert m["budget_seconds"] == 600 and m["p95_share_of_budget"] is not None
    assert rows["website"]["attempts"] == 0 and rows["website"]["p95_seconds"] is None


def test_metrics_only_look_at_the_window(session, company):
    attempt(session, company, "evidence", SUCCEEDED, 5, at=NOW - timedelta(days=30))
    attempt(session, company, "evidence", SUCCEEDED, 5, at=NOW - timedelta(hours=1))
    rows = {m["stage"]: m for m in metrics.stage_metrics(session, hours=24, now=NOW)}
    assert rows["evidence"]["attempts"] == 1


def test_run_metrics_report_duration_and_failures(session):
    run = runner.create_run(session, stages=("evidence",), batch_size=1)
    run.started_at, run.finished_at = NOW, NOW + timedelta(seconds=90)
    run.status, run.companies = COMPLETED, ["a", "b"]
    run.summary = {"companies_processed": 2, "stages": {"evidence": {FAILED: 1, SUCCEEDED: 1}}}
    session.commit()
    row = metrics.run_metrics(session)[0]
    assert row["duration_seconds"] == 90 and row["failed_stages"] == 1 and row["processed"] == 2


# ---- health -----------------------------------------------------------------------------------


def codes(report):
    return {a["code"] for a in report["alerts"]}


def test_an_idle_healthy_pipeline_has_no_alerts(session):
    report = metrics.health(session, now=NOW)
    assert report["status"] == metrics.OK and report["alerts"] == []
    assert report["worker"]["alive"] is False


def test_a_silent_running_run_and_a_missing_worker_are_critical(session):
    run = runner.create_run(session, stages=("evidence",), batch_size=1)
    run.status, run.started_at = RUNNING, NOW - timedelta(hours=2)
    run.heartbeat_at = NOW - timedelta(minutes=40)
    session.commit()
    report = metrics.health(session, now=NOW)
    assert {"RUN_STALLED", "WORKER_DOWN"} <= codes(report)
    assert report["status"] == metrics.CRITICAL


def test_a_beating_worker_keeps_a_long_run_healthy(session):
    run = runner.create_run(session, stages=("evidence",), batch_size=1)
    run.status, run.started_at, run.heartbeat_at = RUNNING, NOW - timedelta(hours=2), NOW
    scheduler.get_schedule(session).worker_seen_at = NOW - timedelta(seconds=20)
    session.commit()
    report = metrics.health(session, now=NOW)
    assert report["status"] == metrics.OK and report["worker"]["alive"] is True


def test_a_stage_running_far_past_its_budget_is_stuck(session, company):
    st.start(session, company.id, "documents", None, NOW - timedelta(hours=2))
    report = metrics.health(session, now=NOW)
    stuck = [a for a in report["alerts"] if a["code"] == "STAGE_STUCK"]
    assert stuck and stuck[0]["stage"] == "documents" and stuck[0]["company"] == company.common_name
    st.finish(session, company.id, "documents", Outcome(SUCCEEDED, "ok"), None, now=NOW)
    assert "STAGE_STUCK" not in codes(metrics.health(session, now=NOW))


def test_a_stage_within_its_budget_is_not_stuck(session, company):
    st.start(session, company.id, "documents", None, NOW - timedelta(minutes=5))
    assert "STAGE_STUCK" not in codes(metrics.health(session, now=NOW))


def test_repeated_failures_and_timeouts_and_failure_rate_warn(session, company):
    for i in range(3):
        attempt(session, company, "extract", FAILED, 60, at=NOW - timedelta(minutes=30 - i))
    attempt(session, company, "documents", FAILED, 900, timed_out=True)
    report = metrics.health(session, now=NOW)
    assert {"REPEATED_FAILURES", "STAGE_TIMEOUTS"} <= codes(report)
    assert report["status"] == metrics.DEGRADED
    for _ in range(12):
        attempt(session, company, "calculate", SUCCEEDED, 1)
    for _ in range(6):
        attempt(session, company, "calculate", FAILED, 1)
    assert "HIGH_FAILURE_RATE" in codes(metrics.health(session, now=NOW))


def test_an_enabled_schedule_without_a_worker_is_flagged(session, sym):
    scheduler.update_schedule(session, enabled=True, symbols=[sym])
    report = metrics.health(session, now=NOW)
    assert "SCHEDULE_WITHOUT_WORKER" in codes(report)


def test_an_overdue_schedule_is_flagged_while_the_worker_lives(session, sym):
    scheduler.update_schedule(session, enabled=True, interval_minutes=60, symbols=[sym])
    row = scheduler.get_schedule(session)
    row.worker_seen_at, row.last_triggered_at = NOW, NOW - timedelta(hours=5)
    session.commit()
    assert "SCHEDULE_OVERDUE" in codes(metrics.health(session, now=NOW))


def test_a_queued_run_with_no_worker_is_flagged(session):
    run = runner.create_run(session, stages=("evidence",), batch_size=1)
    run.created_at = NOW - timedelta(hours=1)
    session.commit()
    assert run.status == QUEUED
    assert "QUEUE_NOT_SERVED" in codes(metrics.health(session, now=NOW))


# ---- schedule scope ---------------------------------------------------------------------------


def test_a_schedule_cannot_be_enabled_without_selected_companies(session):
    with pytest.raises(ValueError, match="choose the companies"):
        scheduler.update_schedule(session, enabled=True)
    assert scheduler.get_schedule(session).enabled is False


def test_the_whole_universe_has_to_be_asked_for(session):
    row = scheduler.update_schedule(session, enabled=True, allow_universe=True)
    assert row.enabled and row.symbols is None


def test_a_scheduled_run_covers_exactly_the_selected_companies(session, company, sym):
    symbol = sym
    scheduler.update_schedule(session, enabled=True, symbols=[symbol, symbol.lower()])
    row = scheduler.get_schedule(session)
    assert row.symbols == [symbol.upper()]  # de-duplicated, upper-cased
    run = scheduler.tick(session)
    assert run.symbols == [symbol.upper()] and run.trigger == "SCHEDULED"
    assert "select" not in run.options and run.options["respect_backoff"] is True
    assert run.batch_size == 1


def test_unknown_symbols_are_refused(session, company):
    with pytest.raises(ValueError, match="unknown company"):
        scheduler.update_schedule(session, symbols=["NOT-A-COMPANY-XYZ"])
    with pytest.raises(ValueError, match="no company symbols"):
        scheduler.update_schedule(session, symbols=[" "])


def test_selection_can_be_cleared_but_not_while_enabled(session, company, sym):
    scheduler.update_schedule(session, enabled=True, symbols=[sym])
    with pytest.raises(ValueError, match="choose the companies"):
        scheduler.update_schedule(session, clear_symbols=True)
    scheduler.update_schedule(session, enabled=False, clear_symbols=True)
    assert scheduler.get_schedule(session).symbols is None


# ---- worker heartbeat and resilience ----------------------------------------------------------


def test_the_worker_records_that_it_is_alive(session, company):
    class Keep:
        def __call__(self):
            return session

    real_close = session.close
    session.close = lambda: None
    try:
        scheduler.serve(Keep(), lambda: {}, once=True, beat_seconds=3600)
    finally:
        session.close = real_close
    row = session.get(PipelineSchedule, 1)
    assert row.worker_seen_at is not None and ":" in (row.worker_note or "")
    assert (datetime.now(UTC) - row.worker_seen_at).total_seconds() < 60


def test_a_database_error_while_polling_does_not_kill_the_worker(session, monkeypatch):
    calls = {"n": 0}
    real_tick = scheduler.tick

    def flaky(s, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database briefly unreachable")
        return real_tick(s, now)

    monkeypatch.setattr(scheduler, "tick", flaky)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)
    real_close = session.close
    session.close = lambda: None
    stops = iter([False, False, True])
    try:
        scheduler.serve(
            lambda: session, lambda: {}, stop=lambda: next(stops), poll_seconds=0, beat_seconds=3600
        )
    finally:
        session.close = real_close
    assert calls["n"] >= 2, "the worker must poll again after the failure"


def test_a_run_keeps_its_heartbeat_while_a_company_takes_long(session, company):
    """The heartbeat is written from its own thread, however long a stage takes."""
    import threading
    import time

    run = runner.create_run(session, stages=("readiness",), batch_size=1)
    runner.claim_run(session, run.id)
    old = datetime.now(UTC) - timedelta(minutes=30)
    session.get(PipelineRun, run.id).heartbeat_at = old
    session.commit()
    stop = threading.Event()
    beater = threading.Thread(
        target=runner.keep_alive, args=(lambda: session, run.id, stop, 0.1, False), daemon=True
    )
    beater.start()
    time.sleep(0.6)  # the main thread does not touch the (shared) test session meanwhile
    stop.set()
    beater.join(5)
    session.expire_all()
    assert session.get(PipelineRun, run.id).heartbeat_at > old + timedelta(minutes=20)


# ---- a stage that stored nothing is not repeated every run ------------------------------------


class _Report:
    stored, discarded, notes = 0, 3, ["page gave nothing verifiable"]


def test_enrichment_that_stored_nothing_is_not_repeated_until_its_cooldown_ends(
    session, company, monkeypatch
):
    from app.research import enrich as enrich_mod

    calls = []
    monkeypatch.setattr(enrich_mod, "enrich_company", lambda *a, **k: calls.append(1) or _Report())
    company.website = "https://example.invalid"
    session.commit()
    kw = dict(stages=("enrich",), llm=object(), client=object())
    first = runner.run_company(session, company, **kw)
    worked_first = session.get(PipelineStageState, (company.id, "enrich")).detail["worked_at"]
    assert first.steps["enrich"].startswith("stored 0, discarded 3") and len(calls) == 1
    second = runner.run_company(session, company, **kw)
    assert "not read again" in second.steps["enrich"] and len(calls) == 1
    third = runner.run_company(session, company, **kw)  # skipping must not push the date forward
    assert len(calls) == 1
    state = session.get(PipelineStageState, (company.id, "enrich"))
    assert state.detail["worked_at"] == worked_first  # skipping did not move the date
    # after the cooldown it reads again; --force reads at once
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    state.detail = {"worked_at": old}
    session.commit()
    runner.run_company(session, company, **kw)
    assert len(calls) == 2
    runner.run_company(session, company, force=True, **kw)
    assert len(calls) == 3
    assert third.statuses["enrich"] == SUCCEEDED


def test_a_registered_website_is_not_fetched_again_every_run(session, company, monkeypatch):
    from app.acquisition import website_discovery as wd

    calls = []

    def discover(session_, company_, *a, **k):
        calls.append(1)
        return wd.DiscoveryResult(
            company_.common_name, wd.MANUAL, company_.website, None, "x", note="ok"
        )

    monkeypatch.setattr(wd, "discover", discover)
    company.website = "https://example.invalid"
    session.commit()
    runner.run_company(session, company, stages=("website",))
    again = runner.run_company(session, company, stages=("website",))
    assert len(calls) == 1 and "not checked again" in again.steps["website"]
    runner.run_company(session, company, stages=("website",), force=True)
    assert len(calls) == 2


# ---- a clean shutdown pauses the run in hand and the next worker continues it ----------------------


def test_a_shutdown_request_pauses_the_run_and_the_next_worker_finishes_it(
    session, company, monkeypatch
):
    import threading

    from app.models import Company, PipelineStageState
    from app.models.pipeline import PAUSED
    from app.pipeline import stages as pst

    others = list(session.scalars(select(Company).where(Company.id != company.id).limit(2)))
    people = [company, *others]
    run = runner.create_run(session, stages=("readiness",), batch_size=3)
    run.companies = [str(c.id) for c in people]
    session.commit()

    stop = threading.Event()
    seen: list = []

    def readiness(ctx, prev):
        seen.append(ctx.company.id)
        stop.set()  # SIGTERM arrives while the first company is being worked on
        return Outcome(SUCCEEDED, "done")

    monkeypatch.setitem(pst.STAGE_FUNCS, "readiness", readiness)
    real_close = session.close
    session.close = lambda: None
    try:
        scheduler.serve(lambda: session, lambda: {}, once=True, beat_seconds=3600, stop_event=stop)
        session.expire_all()
        paused = session.get(PipelineRun, run.id)
        # the first company finished its stage; the run is back in the queue, not lost or stuck
        assert seen == [company.id]
        assert paused.status == QUEUED, paused.status
        assert paused.status != PAUSED
        worked = {s.company_id for s in session.scalars(select(PipelineStageState))}
        assert worked == {company.id}, "companies after the stop have not been touched"

        # the next worker picks the run up and does only what is left
        monkeypatch.setitem(
            pst.STAGE_FUNCS,
            "readiness",
            lambda ctx, prev: (seen.append(ctx.company.id), Outcome(SUCCEEDED, "done"))[1],
        )
        scheduler.serve(lambda: session, lambda: {}, once=True, beat_seconds=3600)
    finally:
        session.close = real_close
    session.expire_all()
    assert session.get(PipelineRun, run.id).status == COMPLETED
    assert seen.count(company.id) == 1, "the finished company is not repeated"
    assert set(seen) == {c.id for c in people}


def test_without_a_stop_request_nothing_changes(session, company, monkeypatch):
    from app.pipeline import stages as pst

    run = runner.create_run(session, stages=("readiness",), batch_size=1)
    run.companies = [str(company.id)]
    session.commit()
    monkeypatch.setitem(pst.STAGE_FUNCS, "readiness", lambda ctx, prev: Outcome(SUCCEEDED, "ok"))
    real_close = session.close
    session.close = lambda: None
    try:
        scheduler.serve(lambda: session, lambda: {}, once=True, beat_seconds=3600)
    finally:
        session.close = real_close
    session.expire_all()
    assert session.get(PipelineRun, run.id).status == COMPLETED
