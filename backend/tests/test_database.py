from sqlalchemy import create_engine

from app.core.database import check_database


def test_check_database_ok_with_sqlite_engine():
    # SQLite stands in for "a reachable database" so this runs without Postgres.
    ok, error = check_database(create_engine("sqlite://"))
    assert ok is True
    assert error is None


def test_check_database_reports_failure_without_raising():
    # Port 1 is never a Postgres server, so the connection must fail cleanly.
    engine = create_engine(
        "postgresql+psycopg://u:p@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    ok, error = check_database(engine)
    assert ok is False
    assert error  # exception class name, no secrets
