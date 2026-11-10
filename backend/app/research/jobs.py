"""Background processing of uploaded documents.

Extraction of a large annual report takes minutes, so the upload request only receives and checks
the file (fast); the heavy stages run here on a small worker pool, each job with its OWN database
session. The state lives in the database (`vault_entries.status`), not in memory, so:

* the browser polls the entry's status;
* a job lost when the server restarts is found again by `recover()` (entries still RECEIVED or
  PROCESSING after `STUCK_AFTER`) and run again - processing is idempotent;
* when an upload is accepted, the research session it belongs to is resumed automatically, which
  re-runs only the stages whose inputs changed.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.models import ResearchSession, VaultEntry
from app.research import orchestrator, vault

STUCK_AFTER = timedelta(minutes=15)
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="research")


def run_upload_job(entry_id: uuid.UUID, *, engine=None, data_dir=None) -> str:
    """Process one entry and resume its research session. Returns the final entry status."""
    with Session(engine or get_engine()) as session:
        entry = vault.process_entry(session, entry_id, data_dir=data_dir)
        session.commit()
        status = entry.status
        if status in (vault.ACCEPTED, vault.HELD_PRIVATE) and entry.session_id:
            sess = session.get(ResearchSession, entry.session_id)
            if sess is not None:
                orchestrator.run(session, sess, data_dir=data_dir)
        return status


def run_research_job(session_id: uuid.UUID, refresh: bool = False, *, engine=None) -> None:
    with Session(engine or get_engine()) as session:
        sess = session.get(ResearchSession, session_id)
        if sess is not None:
            orchestrator.run(session, sess, refresh=refresh, router_factory=default_router_factory)


def default_router_factory():
    """The real provider router (network). Built only when a source refresh is requested."""
    from app.acquisition.factory import build_client
    from app.providers.registry import build_providers
    from app.providers.router import DataRouter

    return DataRouter(build_providers(build_client()))


def submit_research(session_id: uuid.UUID, refresh: bool = False) -> None:
    _pool.submit(_safe_research, session_id, refresh)


def _safe_research(session_id: uuid.UUID, refresh: bool) -> None:
    try:
        run_research_job(session_id, refresh)
    except Exception:  # noqa: BLE001 - the session records its own failure
        pass


def submit_upload(entry_id: uuid.UUID) -> None:
    _pool.submit(_safe, entry_id)


def _safe(entry_id: uuid.UUID) -> None:
    try:
        run_upload_job(entry_id)
    except Exception:  # noqa: BLE001 - the entry already records its own failure; never kill the pool
        pass


def recover(session: Session) -> int:
    """Re-queue entries whose job was lost (server restart). Returns how many were queued."""
    cutoff = datetime.now(UTC) - STUCK_AFTER
    ids = list(
        session.scalars(
            select(VaultEntry.id).where(
                VaultEntry.status.in_((vault.RECEIVED, vault.PROCESSING)),
                VaultEntry.updated_at < cutoff,
            )
        )
    )
    for entry_id in ids:
        submit_upload(entry_id)
    return len(ids)
