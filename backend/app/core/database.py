"""Database engine and session plumbing.

Phase 1 only needs a connection to prove the database is reachable.
ORM models and migrations arrive in Phase 2.
"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping drops dead connections instead of handing them to a request.
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    session = sessionmaker(bind=get_engine(), expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def check_database(engine: Engine | None = None) -> tuple[bool, str | None]:
    """Run `SELECT 1`. Returns (ok, error_message). Never raises."""
    try:
        with (engine or get_engine()).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001 - we want to report any failure, not crash
        return False, type(exc).__name__
