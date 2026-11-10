"""Research API: authentication, ownership, limits, uploads, reports and recovery (Phase 11)."""

import pytest
from fastapi.testclient import TestClient

from app.api import research as research_api
from app.core.config import get_settings
from app.core.database import get_session
from app.main import create_app
from app.research import clock, orchestrator, security
from tests.research_helpers import TODAY, complete_company
from tests.test_research_vault import (  # noqa: F401
    csv_bytes,
    reliance,
    results_pdf,
    scanned_pdf,
)
from tests.test_sector import seeded  # noqa: F401

ALICE = {"X-API-Key": "alice-secret"}
BOB = {"X-API-Key": "bob-secret"}
ADMIN = {"X-API-Key": "root-secret"}
IT = ("it_services", "it_services_and_consulting")
BANK = ("banking", "private_sector_bank")


@pytest.fixture
def client(seeded, monkeypatch, tmp_path):  # noqa: F811
    settings = get_settings()
    monkeypatch.setattr(
        settings, "research_api_keys", "alice:alice-secret,bob:bob-secret,root:root-secret:admin"
    )
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(research_api, "INLINE", True)
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    security.limiter.reset()
    (tmp_path / "documents").mkdir()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: seeded
    return TestClient(app)


def start(client, company, headers=ALICE, **kw):
    r = client.post(
        "/api/research/sessions", json={"company_id": str(company.id), **kw}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---- authentication and authorisation -------------------------------------------------------------


def test_without_configured_keys_every_endpoint_refuses(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "research_api_keys", "")
    r = client.get("/api/research/sessions", headers=ALICE)
    assert r.status_code == 503 and "RESEARCH_API_KEYS" in r.json()["detail"]


def test_a_missing_or_wrong_key_is_unauthorised_and_never_echoed(client):
    assert client.get("/api/research/sessions").status_code == 401
    r = client.get("/api/research/sessions", headers={"X-API-Key": "guess-me"})
    assert r.status_code == 401 and "guess-me" not in r.text
    assert client.get("/api/research/sessions", headers=ALICE).status_code == 200


def test_every_research_endpoint_requires_a_key(client):
    import uuid

    sid = uuid.uuid4()
    for method, path in [
        ("get", f"/api/research/sessions/{sid}"),
        ("get", f"/api/research/sessions/{sid}/readiness"),
        ("get", f"/api/research/sessions/{sid}/missing"),
        ("post", f"/api/research/sessions/{sid}/resume"),
        ("post", f"/api/research/sessions/{sid}/uploads"),
        ("post", f"/api/research/sessions/{sid}/reports"),
        ("get", f"/api/research/reports/{sid}"),
        ("get", f"/api/research/uploads/{sid}"),
        ("get", f"/api/research/companies/{sid}/vault"),
        ("get", "/api/research/template"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path


def test_one_client_cannot_see_or_change_anothers_session(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Private IT", *IT)
    sid = start(client, c)["id"]
    for path in ("", "/readiness", "/missing", "/reports", "/uploads"):
        assert client.get(f"/api/research/sessions/{sid}{path}", headers=BOB).status_code == 404
    assert client.post(f"/api/research/sessions/{sid}/resume", headers=BOB).status_code == 404
    assert client.post(f"/api/research/sessions/{sid}/continue", headers=BOB).status_code == 404
    assert client.get(f"/api/research/sessions/{sid}", headers=ADMIN).status_code == 200
    assert client.get("/api/research/sessions", headers=BOB).json() == []
    assert len(client.get("/api/research/sessions", headers=ADMIN).json()) == 1


# ---- starting research ----------------------------------------------------------------------------


def test_sufficient_evidence_runs_automatically_end_to_end(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Auto IT", *IT)
    body = start(client, c)
    assert body["status"] == "COMPLETED" and body["workflow"] == "AUTOMATIC"
    assert [s["step"] for s in body["steps"]] == list(orchestrator.STEPS)
    sid = body["id"]
    ready = client.get(f"/api/research/sessions/{sid}/readiness", headers=ALICE).json()
    assert ready["eligibility"] == "AUTOMATIC" and ready["counts"]["critical_missing"] == 0
    reports = client.get(f"/api/research/sessions/{sid}/reports", headers=ALICE).json()
    assert len(reports) == 1 and reports[0]["mode"] == "COMPLETE" and reports[0]["complete"]
    full = client.get(f"/api/research/reports/{reports[0]['id']}", headers=ALICE).json()
    assert full["content"]["meta"]["complete"] is True
    assert client.get(f"/api/research/reports/{reports[0]['id']}", headers=BOB).status_code == 404


def test_missing_critical_data_returns_an_actionable_assessment(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Needs docs", *BANK, skip=("nim", "gross_npa"))
    body = start(client, c)
    assert body["status"] == "AWAITING_DOCUMENTS"
    miss = client.get(f"/api/research/sessions/{body['id']}/missing", headers=ALICE).json()
    assert miss["eligibility"] == "ASSISTED" and miss["options"]["continue_with_available"] is True
    nim = next(m for m in miss["missing"]["CRITICAL"] if m["id"] == "nim")
    for field in (
        "why_it_matters", "affects", "document", "document_types", "preferred_period",
        "basis_required", "official_source", "importance", "upload_helps", "action",
    ):  # fmt: skip
        assert nim[field] not in (None, ""), field
    assert nim["status"] == "MISSING" and "found" not in nim  # missing is not zero
    assert any(
        r["id"] == "net_profit" for r in miss["resolved"]
    )  # held evidence is not re-requested
    # no report was silently produced
    assert client.get(f"/api/research/sessions/{body['id']}/reports", headers=ALICE).json() == []


def test_the_user_can_continue_with_available_evidence(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Partial", *BANK, skip=("nim",))
    sid = start(client, c)["id"]
    r = client.post(f"/api/research/sessions/{sid}/continue", headers=ALICE)
    assert r.status_code == 200
    reports = client.get(f"/api/research/sessions/{sid}/reports", headers=ALICE).json()
    assert [x["mode"] for x in reports] == ["AVAILABLE_EVIDENCE"]
    assert reports[0]["missing_critical"] == 1 and reports[0]["complete"] is False


def test_a_complete_report_is_refused_with_reasons_and_a_partial_one_is_allowed(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Refuse", *BANK, skip=("nim",))
    sid = start(client, c)["id"]
    r = client.post(
        f"/api/research/sessions/{sid}/reports", json={"mode": "COMPLETE"}, headers=ALICE
    )
    assert r.status_code == 409 and r.json()["detail"]["reasons"]
    ok = client.post(
        f"/api/research/sessions/{sid}/reports", json={"mode": "AVAILABLE_EVIDENCE"}, headers=ALICE
    )
    assert ok.status_code == 201 and ok.json()["content"]["limitations"]["missing_critical"]
    bad = client.post(f"/api/research/sessions/{sid}/reports", json={"mode": "FULL"}, headers=ALICE)
    assert bad.status_code == 422


def test_a_company_with_no_evidence_gets_no_report(client, seeded):  # noqa: F811
    from tests.test_sector import mk_company

    c = mk_company(seeded, "Nothing", *IT)
    sid = start(client, c)["id"]
    r = client.post(
        f"/api/research/sessions/{sid}/reports", json={"mode": "AVAILABLE_EVIDENCE"}, headers=ALICE
    )
    assert r.status_code == 409


def test_company_resolution_never_guesses(client, seeded):  # noqa: F811
    r = client.post("/api/research/sessions", json={"query": "zzzzqqqq nothing"}, headers=ALICE)
    assert r.status_code == 404
    assert client.post("/api/research/sessions", json={}, headers=ALICE).status_code == 422
    import uuid

    r = client.post("/api/research/sessions", json={"company_id": str(uuid.uuid4())}, headers=ALICE)
    assert r.status_code == 404


def test_session_history_lists_saved_incomplete_research(client, seeded):  # noqa: F811
    done = complete_company(seeded, "Done IT", *IT)
    open_ = complete_company(seeded, "Open Bank", *BANK, skip=("nim",))
    start(client, done)
    start(client, open_)
    everything = client.get("/api/research/sessions", headers=ALICE).json()
    assert {s["company"] for s in everything} == {"Done IT", "Open Bank"}
    saved = client.get("/api/research/sessions?only_open=true", headers=ALICE).json()
    assert [s["company"] for s in saved] == ["Open Bank"]
    assert saved[0]["status"] == "AWAITING_DOCUMENTS" and saved[0]["readiness_score"] is not None
    again = start(client, open_)
    assert again["id"] == saved[0]["id"]  # starting again resumes the saved session


# ---- failure and retry ----------------------------------------------------------------------------


def test_a_failed_run_reports_the_stage_and_resume_retries_only_that(client, seeded, monkeypatch):  # noqa: F811
    c = complete_company(seeded, "Flaky", *IT)
    real = orchestrator.RUNNERS["SCORING"]
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("upstream unavailable")
        return real(ctx)

    monkeypatch.setitem(orchestrator.RUNNERS, "SCORING", flaky)
    body = start(client, c)
    assert body["status"] == "FAILED" and "Score the company" in body["last_error"]
    failed = next(s for s in body["steps"] if s["step"] == "SCORING")
    assert failed["status"] == "FAILED" and "ConnectionError" in failed["error"]
    assert "upstream unavailable" not in str(body)  # internal messages are not exposed
    again = client.post(f"/api/research/sessions/{body['id']}/resume", headers=ALICE).json()
    assert again["status"] == "COMPLETED"
    steps = {s["step"]: s for s in again["steps"]}
    assert steps["SCORING"]["attempts"] == 2 and steps["EVIDENCE"]["attempts"] == 1
    assert steps["EVIDENCE"]["resumed_without_rerun"] is True


# ---- uploads --------------------------------------------------------------------------------------


def post_files(client, sid, files, headers=ALICE, **data):
    return client.post(
        f"/api/research/sessions/{sid}/uploads", files=files, data=data, headers=headers
    )


def test_uploading_a_filing_resolves_the_gap_and_resumes_the_research(client, reliance):  # noqa: F811
    sid = start(client, reliance)["id"]
    r = post_files(
        client,
        sid,
        [("files", ("q2.pdf", results_pdf(), "application/pdf"))],
        licence="PUBLIC_FILING",
    )
    assert r.status_code == 202, r.text
    up = r.json()["uploads"][0]
    assert up["status"] == "ACCEPTED" and {"revenue", "net_profit"} <= set(
        up["resolved_requirements"]
    )
    assert up["filename"] == "q2.pdf" and up["sha256"] and up["visibility"] == "SHARED"
    status = client.get(f"/api/research/uploads/{up['id']}", headers=ALICE).json()
    assert status["validation_status"] in ("PASSED", "PARTIAL") and status["steps"]
    miss = client.get(f"/api/research/sessions/{sid}/missing", headers=ALICE).json()
    assert "net_profit" in {x["id"] for x in miss["resolved"]}
    assert not any(m["id"] == "net_profit" for m in miss["missing"]["CRITICAL"])
    history = client.get(f"/api/research/sessions/{sid}/uploads", headers=ALICE).json()
    assert [h["id"] for h in history] == [up["id"]]


def test_several_files_can_be_uploaded_together_with_individual_outcomes(client, reliance):  # noqa: F811
    sid = start(client, reliance)["id"]
    files = [
        ("files", ("q2.pdf", results_pdf(), "application/pdf")),
        ("files", ("fin.csv", csv_bytes(), "text/csv")),
        ("files", ("fake.pdf", b"MZ\x90\x00" + b"\x00" * 100, "application/pdf")),
        ("files", ("other.pdf", results_pdf(issuer="Beta Chemicals Limited"), "application/pdf")),
    ]
    r = post_files(client, sid, files, licence="PUBLIC_FILING")
    by_name = {u["filename"]: u for u in r.json()["uploads"]}
    assert by_name["q2.pdf"]["status"] == "ACCEPTED"
    assert by_name["fin.csv"]["status"] == "NEEDS_CLARIFICATION"
    assert by_name["fake.pdf"]["status"] == "REJECTED"
    assert (
        "content" in by_name["fake.pdf"]["reject_reason"]
        or "unsupported" in by_name["fake.pdf"]["reject_reason"]
    )
    assert (
        by_name["other.pdf"]["status"] == "REJECTED"
        and "issued by" in by_name["other.pdf"]["reject_reason"]
    )


def test_the_same_file_twice_is_reported_as_a_duplicate(client, reliance):  # noqa: F811
    sid = start(client, reliance)["id"]
    f = [("files", ("q2.pdf", results_pdf(), "application/pdf"))]
    post_files(client, sid, f, licence="PUBLIC_FILING")
    second = post_files(client, sid, f, licence="PUBLIC_FILING").json()["uploads"][0]
    assert second["status"] == "DUPLICATE" and "already uploaded" in second["reject_reason"]


def test_clarifying_an_ambiguous_upload_over_the_api(client, reliance):  # noqa: F811
    sid = start(client, reliance)["id"]
    up = post_files(
        client, sid, [("files", ("fin.csv", csv_bytes(), "text/csv"))], licence="PUBLIC_FILING"
    ).json()["uploads"][0]
    assert up["status"] == "NEEDS_CLARIFICATION" and up["ambiguities"][0]["options"]
    r = client.post(
        f"/api/research/uploads/{up['id']}/clarify",
        json={"source_reference": "Q2 FY27 results on BSE, 30 Oct 2026"},
        headers=ALICE,
    )
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED"
    again = client.post(
        f"/api/research/uploads/{up['id']}/clarify", json={"confirm_issuer": True}, headers=ALICE
    )
    assert again.status_code == 409  # nothing left to clarify
    assert (
        client.post(
            f"/api/research/uploads/{up['id']}/clarify", json={"confirm_issuer": True}, headers=BOB
        ).status_code
        == 404
    )


def test_uploads_are_size_limited_and_counted(client, reliance, monkeypatch):  # noqa: F811
    sid = start(client, reliance)["id"]
    monkeypatch.setattr(get_settings(), "research_max_upload_mb", 1)
    big = b"%PDF-1.7\n" + b"0" * (1024 * 1024 + 5)
    r = post_files(client, sid, [("files", ("big.pdf", big, "application/pdf"))])
    assert r.status_code == 413
    many = [("files", (f"f{i}.csv", csv_bytes(), "text/csv")) for i in range(11)]
    assert post_files(client, sid, many).status_code == 422


def test_upload_rate_limit_returns_429_with_retry_after(client, reliance, monkeypatch):  # noqa: F811
    sid = start(client, reliance)["id"]
    monkeypatch.setattr(get_settings(), "research_upload_rate_per_hour", 2)
    f = [("files", ("a.csv", b"x,y\n1,2\n", "text/csv"))]
    assert post_files(client, sid, f).status_code == 202
    assert post_files(client, sid, f).status_code == 202
    r = post_files(client, sid, f)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert (
        post_files(client, sid, f, headers=BOB).status_code == 404
    )  # bob's own bucket, no session


def test_write_endpoints_are_rate_limited(client, seeded, monkeypatch):  # noqa: F811
    c = complete_company(seeded, "Busy", *IT)
    monkeypatch.setattr(get_settings(), "research_rate_limit_per_minute", 3)
    codes = [
        client.post(
            "/api/research/sessions", json={"company_id": str(c.id)}, headers=ALICE
        ).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [201, 201, 201] and codes[3:] == [429, 429]


def test_a_scanned_pdf_is_reported_with_the_reason(client, reliance):  # noqa: F811
    sid = start(client, reliance)["id"]
    up = post_files(
        client,
        sid,
        [("files", ("scan.pdf", scanned_pdf(), "application/pdf"))],
        licence="PUBLIC_FILING",
    ).json()["uploads"][0]
    assert up["status"] in ("REJECTED", "NEEDS_CLARIFICATION") and not up["resolved_requirements"]


# ---- privacy --------------------------------------------------------------------------------------


def test_the_vault_shares_validated_filings_but_not_private_material(client, reliance):  # noqa: F811
    sid = start(client, reliance)["id"]
    post_files(
        client,
        sid,
        [("files", ("q2.pdf", results_pdf(), "application/pdf"))],
        licence="PUBLIC_FILING",
    )
    post_files(
        client,
        sid,
        [
            (
                "files",
                (
                    "mine.pdf",
                    results_pdf(revenue="2,222.00", total="2,322.00", expenses="1,822.00"),
                    "application/pdf",
                ),
            )
        ],
        licence="USER_LICENSED",
    )
    mine = client.get(f"/api/research/companies/{reliance.id}/vault", headers=ALICE).json()
    theirs = client.get(f"/api/research/companies/{reliance.id}/vault", headers=BOB).json()
    assert len(mine) == 2 and len(theirs) == 1
    seen = theirs[0]
    assert seen["status"] == "ACCEPTED" and seen["filename"] is None
    assert seen["uploaded_by"] == "another user" and seen["steps"] == []
    # another user's upload status is not readable either
    private_id = next(e["id"] for e in mine if e["visibility"] == "PRIVATE")
    assert client.get(f"/api/research/uploads/{private_id}", headers=BOB).status_code == 404


def test_the_next_researcher_benefits_from_a_validated_upload(client, reliance):  # noqa: F811
    first = start(client, reliance)["id"]
    post_files(
        client,
        first,
        [("files", ("q2.pdf", results_pdf(), "application/pdf"))],
        licence="PUBLIC_FILING",
    )
    second = start(client, reliance, headers=BOB)
    assert second["id"] != first
    miss = client.get(f"/api/research/sessions/{second['id']}/missing", headers=BOB).json()
    assert "net_profit" in {x["id"] for x in miss["resolved"]}  # bob uploads nothing


def test_the_template_and_requirements_endpoints_are_informative(client, seeded):  # noqa: F811
    t = client.get("/api/research/template", headers=ALICE).json()
    assert "revenue_from_operations" in t["line_items"]["INCOME"] and t["example"]
    c = complete_company(seeded, "Req Bank", *BANK)
    req = client.get(f"/api/research/requirements/{c.id}", headers=ALICE).json()
    assert req["profile"] == "BANK" and any(r["id"] == "nim" for r in req["requirements"])


def test_an_upload_can_be_reprocessed_by_its_owner_only(client, reliance):  # noqa: F811
    sid = start(client, reliance)["id"]
    up = post_files(
        client,
        sid,
        [("files", ("q2.pdf", results_pdf(), "application/pdf"))],
        licence="PUBLIC_FILING",
    ).json()["uploads"][0]
    again = client.post(f"/api/research/uploads/{up['id']}/reprocess", headers=ALICE)
    assert again.status_code == 200 and again.json()["status"] == "ACCEPTED"
    assert (
        client.post(f"/api/research/uploads/{up['id']}/reprocess", headers=BOB).status_code == 404
    )
    assert client.post(f"/api/research/uploads/{up['id']}/reprocess").status_code == 401


def test_the_overview_lists_every_company_with_what_to_supply_next(client, seeded):  # noqa: F811
    complete_company(seeded, "Ready IT", *IT)
    complete_company(seeded, "Gappy Bank", *BANK, skip=("nim", "gross_npa"))
    r = client.get("/api/research/overview", headers=ALICE)
    assert r.status_code == 200
    body = r.json()
    names = [c["name"] for c in body["companies"]]
    assert names[0] == "Ready IT" and "Gappy Bank" in names  # ready companies first
    assert body["automatic"] >= 1 and body["assisted"] >= 1
    bank = next(c for c in body["companies"] if c["name"] == "Gappy Bank")
    assert {m["id"] for m in bank["critical_missing"]} == {"nim", "gross_npa"}
    assert bank["profile"] == "Bank" and bank["documents_needed"]
    assert client.get("/api/research/overview").status_code == 401
