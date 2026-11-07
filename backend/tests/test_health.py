from fastapi.testclient import TestClient

from app.main import create_app


def test_liveness_returns_ok_without_database():
    client = TestClient(create_app())
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["service"] == "EquityOS API"


def test_readiness_ok_when_database_up(monkeypatch):
    monkeypatch.setattr("app.api.health.check_database", lambda: (True, None))
    res = TestClient(create_app()).get("/health/ready")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "database": "up", "detail": None}


def test_readiness_503_when_database_down(monkeypatch):
    monkeypatch.setattr("app.api.health.check_database", lambda: (False, "OperationalError"))
    res = TestClient(create_app()).get("/health/ready")
    assert res.status_code == 503
    body = res.json()
    assert body["database"] == "down"
    # Must not leak connection details, only the exception type.
    assert body["detail"] == "OperationalError"


def test_cors_allows_frontend_origin():
    res = TestClient(create_app()).get("/health", headers={"Origin": "http://localhost:3000"})
    assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"
