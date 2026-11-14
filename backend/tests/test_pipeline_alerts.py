"""Alert delivery: new alerts are sent once, repeat only as reminders, and clear with a notice."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models import PipelineEvent
from app.pipeline import alerts, scheduler

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def health(*items):
    return {"status": "DEGRADED", "alerts": [dict(i) for i in items]}


STUCK = {
    "level": "CRITICAL",
    "code": "STAGE_STUCK",
    "message": "TCS: documents stuck",
    "stage": "documents",
    "company": "TCS",
}
DOWN = {"level": "CRITICAL", "code": "WORKER_DOWN", "message": "no worker"}


class Collector:
    def __init__(self):
        self.sent = []

    def __call__(self, payload):
        self.sent.append(payload)


def test_a_new_alert_is_sent_to_every_sink_and_recorded(session):
    a, b = Collector(), Collector()
    sent = alerts.deliver(session, [a, b], now=NOW, health=health(DOWN))
    assert len(sent) == 1 and a.sent[0]["code"] == "WORKER_DOWN" and b.sent == a.sent
    event = session.scalar(select(PipelineEvent).where(PipelineEvent.status == "ALERT"))
    assert event.message.startswith("WORKER_DOWN|CRITICAL:")


def test_the_same_alert_is_not_sent_again_inside_the_cooldown(session):
    sink = Collector()
    alerts.deliver(session, [sink], now=NOW, health=health(DOWN))
    again = alerts.deliver(session, [sink], now=NOW + timedelta(minutes=30), health=health(DOWN))
    assert again == [] and len(sink.sent) == 1


def test_it_is_repeated_as_a_reminder_after_the_cooldown(session):
    sink = Collector()
    alerts.deliver(session, [sink], now=NOW, health=health(DOWN), cooldown=timedelta(hours=1))
    later = NOW + timedelta(hours=2)
    alerts.deliver(session, [sink], now=later, health=health(DOWN), cooldown=timedelta(hours=1))
    assert len(sink.sent) == 2 and sink.sent[1]["reminder"] is True


def test_an_alert_that_clears_is_announced_once(session):
    sink = Collector()
    alerts.deliver(session, [sink], now=NOW, health=health(DOWN))
    cleared = alerts.deliver(session, [sink], now=NOW + timedelta(minutes=10), health=health())
    assert [p["event"] for p in cleared] == ["RESOLVED"] and "cleared" in cleared[0]["message"]
    nothing = alerts.deliver(session, [sink], now=NOW + timedelta(minutes=20), health=health())
    assert nothing == [], "a cleared alert is not announced twice"


def test_a_returning_alert_is_new_again(session):
    sink = Collector()
    alerts.deliver(session, [sink], now=NOW, health=health(DOWN))
    alerts.deliver(session, [sink], now=NOW + timedelta(minutes=5), health=health())
    back = alerts.deliver(session, [sink], now=NOW + timedelta(minutes=10), health=health(DOWN))
    assert len(back) == 1 and back[0]["event"] == "ALERT" and back[0]["reminder"] is False


def test_alerts_about_different_places_are_separate_problems(session):
    sink = Collector()
    other = {**STUCK, "company": "Infosys", "message": "Infosys: documents stuck"}
    alerts.deliver(session, [sink], now=NOW, health=health(STUCK))
    sent = alerts.deliver(session, [sink], now=NOW, health=health(STUCK, other))
    assert [p["key"] for p in sent] == ["STAGE_STUCK:documents:Infosys"]


def test_a_failing_sink_is_recorded_and_does_not_stop_the_others(session):
    good = Collector()

    def broken(payload):
        raise ConnectionError("webhook down")

    sent = alerts.deliver(session, [broken, good], now=NOW, health=health(DOWN))
    assert good.sent and sent[0]["delivery_failures"] == ["ConnectionError: webhook down"]


def test_state_survives_a_restart(session):
    """De-duplication reads the stored events, so a new worker does not repeat old alerts."""
    alerts.deliver(session, [Collector()], now=NOW, health=health(DOWN))
    session.commit()
    fresh = Collector()
    alerts.deliver(session, [fresh], now=NOW + timedelta(minutes=1), health=health(DOWN))
    assert fresh.sent == []


def test_the_file_sink_appends_json_lines(tmp_path):
    sink = alerts.file_sink(tmp_path / "logs")
    sink({"event": "ALERT", "code": "X", "message": "m"})
    sink({"event": "RESOLVED", "code": "X", "message": "n"})
    lines = (tmp_path / "logs" / "pipeline_alerts.jsonl").read_text().splitlines()
    assert [json.loads(x)["event"] for x in lines] == ["ALERT", "RESOLVED"]


def test_the_webhook_receives_a_json_body_with_text(session):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200)

    sink = alerts.webhook_sink(
        "https://hooks.example.invalid/x",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    alerts.deliver(session, [sink], now=NOW, health=health(DOWN))
    url, body = seen[0]
    assert url == "https://hooks.example.invalid/x"
    assert (
        body["text"].startswith("[EquityOS pipeline] CRITICAL:") and body["code"] == "WORKER_DOWN"
    )


def test_a_webhook_error_status_counts_as_a_failed_delivery(session):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    sink = alerts.webhook_sink("https://hooks.example.invalid/x", client=client)
    sent = alerts.deliver(session, [sink], now=NOW, health=health(DOWN))
    assert sent[0]["delivery_failures"] and "500" in sent[0]["delivery_failures"][0]


def test_default_sinks_add_the_webhook_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(alerts.WEBHOOK_ENV, raising=False)
    assert len(alerts.default_sinks(tmp_path)) == 1
    monkeypatch.setenv(alerts.WEBHOOK_ENV, "https://hooks.example.invalid/x")
    assert len(alerts.default_sinks(tmp_path)) == 2


def test_the_real_health_report_feeds_delivery(session):
    """End to end with the real health check: a queued run nobody serves becomes a delivered alert."""
    from app.pipeline import runner

    run = runner.create_run(session, stages=("evidence",), batch_size=1)
    run.created_at = NOW - timedelta(hours=1)
    session.commit()
    sink = Collector()
    sent = alerts.deliver(session, [sink], now=NOW)
    assert any(p["code"] == "QUEUE_NOT_SERVED" for p in sent)


def test_the_worker_delivers_alerts_on_its_own(session):
    """serve() calls delivery from its loop (and an alerting failure never stops it)."""
    calls = []

    def sinks():
        calls.append(1)
        raise RuntimeError("sinks could not be built")

    real_close = session.close
    session.close = lambda: None
    try:
        scheduler.serve(
            lambda: session, lambda: {}, once=True, beat_seconds=3600, alert_sinks=sinks
        )
    finally:
        session.close = real_close
    assert calls, "the worker must try to deliver alerts"


@pytest.mark.parametrize(
    ("alert", "key"),
    [
        ({"code": "WORKER_DOWN"}, "WORKER_DOWN"),
        ({"code": "STAGE_STUCK", "stage": "extract", "company": "TCS"}, "STAGE_STUCK:extract:TCS"),
        ({"code": "RUN_STALLED", "run_id": 7}, "RUN_STALLED:7"),
    ],
)
def test_alert_keys_identify_the_problem_and_where(alert, key):
    assert alerts.alert_key(alert) == key
