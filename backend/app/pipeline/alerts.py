"""Deliver pipeline alerts to where someone will see them, once, and say when they clear.

`metrics.health` says what is wrong right now. This module turns that into notifications:

* every NEW alert is sent to each configured sink and recorded as an `ALERT` event;
* an alert that is still present is not sent again until `cooldown` has passed (a reminder);
* an alert that has gone is sent once as `RESOLVED`.

Sinks: a JSON-lines file under the data folder (`logs/pipeline_alerts.jsonl`, always on) and an
optional webhook (`PIPELINE_ALERT_WEBHOOK`, a URL you control; the body is JSON with a `text` field,
which Slack-style receivers accept). A sink that fails is recorded and never stops the others, the
worker or a run. De-duplication uses the stored events, so it survives restarts.
"""

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PipelineEvent
from app.pipeline import metrics
from app.pipeline import state as st

log = logging.getLogger("equityos.pipeline")
ALERT, RESOLVED = "ALERT", "RESOLVED"
DEFAULT_COOLDOWN = timedelta(hours=6)
WEBHOOK_ENV = "PIPELINE_ALERT_WEBHOOK"
LOOKBACK = timedelta(days=2)

Sink = Callable[[dict], None]


def alert_key(alert: dict) -> str:
    """What makes two alerts 'the same problem': code plus where it is."""
    parts = [alert["code"]]
    for field in ("stage", "company", "run_id"):
        if alert.get(field) not in (None, ""):
            parts.append(str(alert[field]))
    return ":".join(parts)


def file_sink(directory: Path) -> Sink:
    def write(payload: dict) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "pipeline_alerts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")

    return write


def webhook_sink(url: str, *, client: httpx.Client | None = None) -> Sink:
    def post(payload: dict) -> None:
        owns = client is None
        http = client or httpx.Client(timeout=10.0)
        try:
            icon = "RESOLVED" if payload["event"] == RESOLVED else payload["level"]
            response = http.post(
                url, json={"text": f"[EquityOS pipeline] {icon}: {payload['message']}", **payload}
            )
            response.raise_for_status()
        finally:
            if owns:
                http.close()

    return post


def default_sinks(data_dir: Path | None = None) -> list[Sink]:
    if data_dir is None:
        from app.core.config import get_settings, resolve_data_dir

        data_dir = resolve_data_dir(get_settings())
    sinks: list[Sink] = [file_sink(Path(data_dir) / "logs")]
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if url:
        sinks.append(webhook_sink(url))
    return sinks


def _recent_state(session: Session, now: datetime) -> dict[str, tuple[str, datetime]]:
    """Latest ALERT / RESOLVED event per alert key: {key: (kind, when)}."""
    latest: dict[str, tuple[str, datetime]] = {}
    rows = session.execute(
        select(PipelineEvent.status, PipelineEvent.message, PipelineEvent.at)
        .where(PipelineEvent.status.in_((ALERT, RESOLVED)), PipelineEvent.at >= now - LOOKBACK)
        .order_by(PipelineEvent.id)
    )
    for status, message, at in rows:
        key = (message or "").split("|", 1)[0]
        latest[key] = (status, at if at.tzinfo else at.replace(tzinfo=UTC))
    return latest


def deliver(
    session: Session,
    sinks: list[Sink],
    *,
    now: datetime | None = None,
    cooldown: timedelta = DEFAULT_COOLDOWN,
    health: dict | None = None,
) -> list[dict]:
    """Send what is new or has cleared. Returns the notifications that were sent."""
    now = now or datetime.now(UTC)
    report = health if health is not None else metrics.health(session, now=now)
    previous = _recent_state(session, now)
    current = {alert_key(a): a for a in report["alerts"]}
    sent: list[dict] = []

    def send(payload: dict) -> None:
        failures = []
        for sink in sinks:
            try:
                sink(payload)
            except Exception as exc:  # noqa: BLE001 - one broken sink must not stop the rest
                log.warning("alert sink failed: %s", exc)
                failures.append(f"{type(exc).__name__}: {exc}"[:120])
        payload["delivery_failures"] = failures
        sent.append(payload)

    for key, alert in current.items():
        kind, when = previous.get(key, (None, None))
        if kind == ALERT and when is not None and now - when < cooldown:
            continue  # still the same problem, already told
        payload = {
            "event": ALERT,
            "key": key,
            "level": alert["level"],
            "code": alert["code"],
            "message": alert["message"],
            "at": now.isoformat(),
            "reminder": kind == ALERT,
        }
        send(payload)
        st.event(session, None, None, None, ALERT, f"{key}|{alert['level']}: {alert['message']}")
    for key, (kind, _when) in previous.items():
        if kind == ALERT and key not in current:
            payload = {
                "event": RESOLVED,
                "key": key,
                "level": "INFO",
                "code": key.split(":")[0],
                "message": f"{key} has cleared",
                "at": now.isoformat(),
            }
            send(payload)
            st.event(session, None, None, None, RESOLVED, f"{key}|cleared")
    return sent
