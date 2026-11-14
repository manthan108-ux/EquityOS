"""Pipeline API: authentication, admin-only control, run creation, per-stage state, schedule."""

# ruff: noqa: F811
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_session
from app.main import create_app
from app.models import Company
from app.pipeline import runner
from app.research import security
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.test_pipeline_runner import execute, new_run
from tests.test_universe import PAGES  # noqa: F401

READER = {"X-API-Key": "alice-secret"}
ADMIN = {"X-API-Key": "root-secret"}


@pytest.fixture
def client(session, monkeypatch):
    seed_reference_data(session)
    seed_test_universe(session)
    for c in session.scalars(select(Company)):
        c.website = c.investor_relations_url = None
    session.commit()
    monkeypatch.setattr(
        get_settings(), "research_api_keys", "alice:alice-secret,root:root-secret:admin"
    )
    security.limiter.reset()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_every_pipeline_endpoint_needs_a_key(client):
    for path in ("/overview", "/runs", "/runs/1", "/failures", "/schedule"):
        assert client.get("/api/pipeline" + path).status_code == 401
    assert client.post("/api/pipeline/runs", json={}).status_code == 401


def test_a_reader_can_look_but_not_start_stop_or_change_anything(client):
    assert client.get("/api/pipeline/overview", headers=READER).status_code == 200
    for r in (
        client.post("/api/pipeline/runs", json={}, headers=READER),
        client.post("/api/pipeline/runs/1/pause", headers=READER),
        client.put("/api/pipeline/schedule", json={"enabled": True}, headers=READER),
    ):
        assert r.status_code == 403 and "admin" in r.json()["detail"]


def test_an_admin_queues_runs_which_the_api_does_not_execute(client, session):
    r = client.post(
        "/api/pipeline/runs",
        json={"stages": ["upstox", "evidence"], "batch_size": 3, "workers": 2},
        headers=ADMIN,
    )
    assert r.status_code == 201
    run = r.json()
    assert run["status"] == "QUEUED" and run["stages"] == ["upstox", "evidence"]
    assert (
        run["trigger"] == "STAGE" and run["options"]["workers"] == 2 and run["created_by"] == "root"
    )
    assert client.get("/api/pipeline/runs", headers=READER).json()[0]["id"] == run["id"]


def test_requests_are_validated(client):
    bad_stage = client.post("/api/pipeline/runs", json={"stages": ["teleport"]}, headers=ADMIN)
    assert bad_stage.status_code == 422 and "unknown stage" in bad_stage.json()["detail"]
    assert (
        client.post("/api/pipeline/runs", json={"batch_size": 0}, headers=ADMIN).status_code == 422
    )
    assert (
        client.post("/api/pipeline/runs", json={"batch_size": 5000}, headers=ADMIN).status_code
        == 422
    )
    assert (
        client.post("/api/pipeline/runs", json={"select": "everything"}, headers=ADMIN).status_code
        == 422
    )
    single = client.post("/api/pipeline/runs", json={"symbols": ["TCS"]}, headers=ADMIN).json()
    assert single["trigger"] == "SINGLE"
    retry = client.post("/api/pipeline/runs", json={"select": "failed"}, headers=ADMIN).json()
    assert retry["trigger"] == "RETRY" and retry["options"]["respect_backoff"] is True


def test_pause_resume_cancel_follow_the_run_state(client, session):
    run = client.post("/api/pipeline/runs", json={"stages": ["upstox"]}, headers=ADMIN).json()
    url = f"/api/pipeline/runs/{run['id']}"
    assert client.post(url + "/pause", headers=ADMIN).json()["status"] == "PAUSED"
    assert client.post(url + "/resume", headers=ADMIN).json()["status"] == "QUEUED"
    assert client.post(url + "/cancel", headers=ADMIN).json()["status"] == "CANCELLED"
    again = client.post(url + "/cancel", headers=ADMIN)
    assert again.status_code == 409 and "cannot cancel" in again.json()["detail"]
    assert client.post("/api/pipeline/runs/99999/pause", headers=ADMIN).status_code == 404
    assert client.post(url + "/explode", headers=ADMIN).status_code == 404


def test_the_run_detail_shows_each_companys_persisted_stage_state(client, session):
    run = new_run(session, batch_size=2)
    execute(session, run)
    d = client.get(f"/api/pipeline/runs/{run.id}", headers=READER).json()
    assert d["status"] == "COMPLETED" and len(d["company_states"]) == 2
    row = d["company_states"][0]
    assert row["symbol"] and set(row["stages"]) == {"upstox", "evidence", "readiness"}
    up = row["stages"]["upstox"]
    assert up["status"] == "SUCCEEDED" and up["attempts"] == 1 and up["finished_at"]
    assert row["stages"]["readiness"]["detail"]["eligibility"]
    assert any(e["stage"] == "upstox" for e in d["events"])
    assert client.get("/api/pipeline/runs/424242", headers=READER).status_code == 404


def test_one_companys_stages_and_events_and_the_failure_list(client, session, monkeypatch):
    from app.pipeline import stages as pst

    monkeypatch.setitem(
        pst.STAGE_FUNCS, "evidence", lambda c, p: (_ for _ in ()).throw(OSError("disk full"))
    )
    co = session.scalar(select(Company).where(Company.common_name == "TCS"))
    runner.run_company(session, co, stages=("evidence",))
    page = client.get(f"/api/pipeline/companies/{co.id}", headers=READER).json()
    assert page["stages"]["evidence"]["status"] == "FAILED"
    assert "disk full" in page["stages"]["evidence"]["error"]
    assert page["stages"]["evidence"]["next_retry_at"]
    assert page["events"][0]["stage"] == "evidence"
    failures = client.get("/api/pipeline/failures", headers=READER).json()
    assert failures[0]["name"] == "TCS" and failures[0]["stage"] == "evidence"


def test_the_overview_reports_counts_the_backlog_and_the_schedule(client, session):
    run = new_run(session, batch_size=2)
    execute(session, run)
    o = client.get("/api/pipeline/overview", headers=READER).json()
    assert o["stage_status_counts"]["upstox"] == {"SUCCEEDED": 2}
    assert o["stage_status_counts"]["extract"] == {}  # a stage nobody ran shows no invented counts
    assert set(o["backlog"]) >= {"companies_missing_evidence", "financial_pdfs_read"}
    assert o["schedule"]["enabled"] is False and o["latest_run"]["id"] == run.id


def test_the_schedule_can_be_changed_by_an_admin_within_bounds(client):
    ok = client.put(
        "/api/pipeline/schedule",
        json={
            "enabled": True,
            "interval_minutes": 120,
            "batch_size": 5,
            "concurrency": 2,
            "stages": ["upstox", "evidence"],
            "allow_universe": True,
        },
        headers=ADMIN,
    )
    assert (
        ok.status_code == 200
        and ok.json()["enabled"]
        and ok.json()["stages"] == ["upstox", "evidence"]
    )
    assert client.get("/api/pipeline/schedule", headers=READER).json()["interval_minutes"] == 120
    too_often = client.put("/api/pipeline/schedule", json={"interval_minutes": 1}, headers=ADMIN)
    assert too_often.status_code == 422 and "at least" in too_often.json()["detail"]


def test_a_schedule_cannot_be_enabled_without_selected_companies(client):
    refused = client.put("/api/pipeline/schedule", json={"enabled": True}, headers=ADMIN)
    assert refused.status_code == 422 and "choose the companies" in refused.json()["detail"]
    unknown = client.put("/api/pipeline/schedule", json={"symbols": ["NOPE-XYZ"]}, headers=ADMIN)
    assert unknown.status_code == 422 and "unknown company" in unknown.json()["detail"]


def test_monitoring_endpoints_need_a_key_and_answer_a_reader(client):
    for path in ("/metrics", "/health", "/integrity"):
        assert client.get("/api/pipeline" + path).status_code == 401
    metrics = client.get("/api/pipeline/metrics", headers=READER).json()
    assert [m["stage"] for m in metrics["stages"]][0] == "classify"
    assert {"p95_seconds", "budget_seconds", "timed_out", "failure_rate"} <= set(
        metrics["stages"][0]
    )
    health = client.get("/api/pipeline/health", headers=READER).json()
    assert health["status"] in ("OK", "DEGRADED", "CRITICAL") and "worker" in health
    audit = client.get("/api/pipeline/integrity", headers=READER).json()
    assert audit["status"] in ("PASS", "WARN", "FAIL")
    assert {c["code"] for c in audit["checks"]} >= {"DUPLICATE_ACTIVE", "VALUE_MISMATCH"}
