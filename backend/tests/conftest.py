"""Shared fixtures.

Database tests run against a real PostgreSQL server (SQLite cannot enforce our regex CHECK
constraints or UUID defaults). A throwaway database `<name>_test` is created, migrated with the
real Alembic migrations, and dropped afterwards. Each test runs inside a transaction that is rolled
back, so tests never see each other's data.

If PostgreSQL is unreachable the DB tests are skipped locally, but FAIL when the CI env var is set.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_config(db_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.attributes["db_url"] = db_url
    return cfg


@pytest.fixture(scope="session")
def test_db_url() -> Iterator[str]:
    base = make_url(get_settings().database_url)
    test_name = f"{base.database}_test"
    admin = create_engine(
        base.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 3},
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{test_name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{test_name}"'))
    except Exception as exc:  # noqa: BLE001
        admin.dispose()
        message = f"PostgreSQL not reachable for DB tests ({type(exc).__name__})"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)

    url = base.set(database=test_name).render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "head")
    yield url

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{test_name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="session")
def db_engine(test_db_url: str) -> Iterator[Engine]:
    engine = create_engine(test_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(db_engine: Engine) -> Iterator[Session]:
    connection = db_engine.connect()
    outer = connection.begin()
    # create_savepoint lets tests catch IntegrityError and keep using the session.
    db_session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield db_session
    finally:
        db_session.close()
        outer.rollback()
        connection.close()
