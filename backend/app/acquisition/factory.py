"""Builds the shared PoliteClient and records every fetch in the acquisition log."""

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.acquisition.http import ClientConfig, FetchResult, PoliteClient
from app.core.config import get_settings, resolve_data_dir
from app.core.database import get_engine
from app.models import AcquisitionLog

log = logging.getLogger("equityos.acquisition")

# Hosts that punish bursts (BSE answers HTTP 200 with an error page) get a wider spacing.
HOST_INTERVALS = {
    "www.bseindia.com": 8.0,
    "archives.nseindia.com": 2.0,
    "query.wikidata.org": 1.5,
}
# BSE's WAF answers HTTP 406 for a while after bursts; measured: it clears within ~30 seconds.
HOST_THROTTLE_CODES = {"www.bseindia.com": frozenset({406})}
HOST_BACKOFF_S = {"www.bseindia.com": 30.0}


def db_recorder(source: str, result: FetchResult) -> None:
    """Persist one audit row per fetch. Logging must never break an acquisition."""
    log.info("fetch %s %s -> %s", source, result.url, result.status.value)
    try:
        with Session(get_engine()) as session, session.begin():
            session.add(
                AcquisitionLog(
                    source_code=source,
                    url=result.url,
                    status=result.status,
                    http_status=result.http_status,
                    from_cache=result.from_cache,
                    attempts=result.attempts,
                    bytes=len(result.content) if result.content is not None else None,
                    error=result.error,
                    duration_ms=result.duration_ms,
                    started_at=datetime.now(UTC),
                )
            )
    except Exception:  # noqa: BLE001
        log.exception("could not write acquisition log")


def build_client(*, record: bool = True, shared_pacing: bool = False) -> PoliteClient:
    """`shared_pacing` makes host spacing hold across processes (stages run in children)."""
    settings = get_settings()
    config = ClientConfig(
        user_agent=settings.http_user_agent,
        cache_dir=resolve_data_dir(settings) / "cache",
        host_intervals=HOST_INTERVALS,
        host_throttle_codes=HOST_THROTTLE_CODES,
        host_backoff_s=HOST_BACKOFF_S,
    )
    pacer = None
    if shared_pacing:
        from app.acquisition.pacing import SharedPacer

        pacer = SharedPacer(resolve_data_dir(settings) / "pacing").reserve
    return PoliteClient(config, recorder=db_recorder if record else None, pacer=pacer)
