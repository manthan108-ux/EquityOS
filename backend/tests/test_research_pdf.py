"""PDF rendering of research reports (ReportLab)."""

from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app.api import research as research_api
from app.core.config import get_settings
from app.core.database import get_session
from app.main import create_app
from app.research import clock, orchestrator, security
from app.research import report as rp
from app.research.pdf import clean, fmt_value, money, render
from tests.research_helpers import TODAY, complete_company, evidence
from tests.test_sector import seeded  # noqa: F401

pymupdf = pytest.importorskip("pymupdf")
IT = ("it_services", "it_services_and_consulting")
BANK = ("banking", "private_sector_bank")
ALICE = {"X-API-Key": "alice-secret"}
BOB = {"X-API-Key": "bob-secret"}


def pdf_text(data: bytes) -> tuple[str, int]:
    doc = pymupdf.open(stream=data, filetype="pdf")
    return "\n".join(p.get_text() for p in doc), doc.page_count


def make_report(session, company, mode):
    sess = orchestrator.create_session(session, company, "alice", company.common_name)
    orchestrator.run(session, sess, today=TODAY)
    return rp.generate(session, sess, mode, "alice", today=TODAY)


# ---- formatting -----------------------------------------------------------------------------------


def test_amounts_are_shown_in_crore_with_indian_grouping():
    assert money(D("1234567.891")) == "12,34,567.89"
    assert money(D("999")) == "999.00"
    assert money(D("-100000")) == "-1,00,000.00"
    assert fmt_value("123456700000", "inr") == "Rs. 12,345.67 crore"
    assert fmt_value("1018.5", "inr_per_share") == "Rs. 1,018.50"
    assert (
        fmt_value("1.390000", "percent") == "1.39%" and fmt_value("4.581238", "percent") == "4.58%"
    )
    assert fmt_value("11.17", "x") == "11.17x"
    assert fmt_value(None, "inr") == "not available"  # a missing value is never printed as zero


def test_text_the_standard_fonts_cannot_draw_is_replaced_and_markup_is_escaped():
    out = clean("Profit ₹ 5 <b>bold</b> & more हिन्दी")
    assert "Rs." in out and "&lt;b&gt;" in out and "&amp;" in out
    assert out.encode("ascii", "ignore")  # nothing that would crash the font encoder
    assert clean(None) == ""


# ---- reports --------------------------------------------------------------------------------------


def test_a_complete_report_renders_with_company_banner_sections_and_footer(seeded):  # noqa: F811
    c = complete_company(seeded, "Complete & Co <IT>", *IT)
    report = make_report(seeded, c, rp.COMPLETE)
    data = render(
        report.content, version=report.version, mode=report.mode,
        report_id=str(report.id), content_hash=report.content_hash,
    )  # fmt: skip
    assert data.startswith(b"%PDF-")
    text, pages = pdf_text(data)
    assert "COMPLETE RESEARCH REPORT" in text and "Complete & Co <IT>" in text
    assert "Financial performance" in text and "Limitations" in text
    assert "Accounting basis" in text and "consolidated" in text
    assert f"of {pages}" in text and "sha " in text  # page numbering and report identity
    assert "None" not in text  # no Python placeholders leak into the document


def test_a_partial_report_prints_every_gap_where_it_matters_and_is_not_called_complete(seeded):  # noqa: F811
    c = complete_company(seeded, "Gappy Bank", *BANK, skip=("nim", "gross_npa"))
    report = make_report(seeded, c, rp.AVAILABLE_EVIDENCE)
    orchestrator.choose_available_evidence(seeded, orchestrator_session(seeded, c))
    data = render(
        report.content, version=1, mode=report.mode, report_id=str(report.id),
        content_hash=report.content_hash,
    )  # fmt: skip
    text, _ = pdf_text(data)
    assert "AVAILABLE-EVIDENCE REPORT (not complete)" in text
    assert "COMPLETE RESEARCH REPORT" not in text
    assert "Net interest margin (NIM)" in text and "NOT AVAILABLE" in text
    assert "Critical items not available" in text
    assert "No conclusion is drawn on net interest margin" in text.replace("\n", " ")


def test_evidence_citations_survive_into_the_pdf(seeded):  # noqa: F811
    c = complete_company(seeded, "Cited IT", *IT)
    evidence(seeded, c, "GROWTH.eps_yoy", 3, kind="CALCULATION", unit="percent")
    report = make_report(seeded, c, rp.COMPLETE)
    text, _ = pdf_text(
        render(
            report.content,
            version=1,
            mode=report.mode,
            report_id=str(report.id),
            content_hash="a" * 64,
        )
    )
    assert "p.3" in text and "evidence #" in text


def test_a_long_report_paginates_and_keeps_the_footer_on_every_page(seeded):  # noqa: F811
    c = complete_company(seeded, "Long IT", *IT)
    report = make_report(seeded, c, rp.COMPLETE)
    content = dict(report.content)
    content["limitations"] = {
        **content["limitations"],
        "limits_on_conclusions": [f"No conclusion is drawn on item {i}." for i in range(150)],
    }
    data = render(
        content, version=2, mode=report.mode, report_id=str(report.id), content_hash="b" * 64
    )
    doc = pymupdf.open(stream=data, filetype="pdf")
    assert doc.page_count >= 3
    assert all(
        f"page {i + 1} of {doc.page_count}" in doc[i].get_text() for i in range(doc.page_count)
    )


def test_rendering_is_stable_for_unchanged_content(seeded):  # noqa: F811
    c = complete_company(seeded, "Stable IT", *IT)
    report = make_report(seeded, c, rp.COMPLETE)
    kw = dict(
        version=1, mode=report.mode, report_id=str(report.id), content_hash=report.content_hash
    )
    assert render(report.content, **kw) == render(report.content, **kw)


def orchestrator_session(session, company):
    from sqlalchemy import select

    from app.models import ResearchSession

    return session.scalar(select(ResearchSession).where(ResearchSession.company_id == company.id))


# ---- API ------------------------------------------------------------------------------------------


@pytest.fixture
def client(seeded, monkeypatch, tmp_path):  # noqa: F811
    settings = get_settings()
    monkeypatch.setattr(settings, "research_api_keys", "alice:alice-secret,bob:bob-secret")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(research_api, "INLINE", True)
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    security.limiter.reset()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: seeded
    return TestClient(app)


def test_the_pdf_endpoint_returns_a_pdf_to_its_owner_only(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Api IT", *IT)
    sid = client.post(
        "/api/research/sessions", json={"company_id": str(c.id)}, headers=ALICE
    ).json()["id"]
    rid = client.get(f"/api/research/sessions/{sid}/reports", headers=ALICE).json()[0]["id"]
    r = client.get(f"/api/research/reports/{rid}/pdf", headers=ALICE)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-") and "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    text, _ = pdf_text(r.content)
    assert "Api IT" in text
    assert client.get(f"/api/research/reports/{rid}/pdf", headers=BOB).status_code == 404
    assert client.get(f"/api/research/reports/{rid}/pdf").status_code == 401
    import uuid

    assert client.get(f"/api/research/reports/{uuid.uuid4()}/pdf", headers=ALICE).status_code == 404


def test_the_pdf_endpoint_is_rate_limited(client, seeded, monkeypatch):  # noqa: F811
    c = complete_company(seeded, "Limited IT", *IT)
    sid = client.post(
        "/api/research/sessions", json={"company_id": str(c.id)}, headers=ALICE
    ).json()["id"]
    rid = client.get(f"/api/research/sessions/{sid}/reports", headers=ALICE).json()[0]["id"]
    monkeypatch.setattr(get_settings(), "research_rate_limit_per_minute", 2)
    codes = [
        client.get(f"/api/research/reports/{rid}/pdf", headers=ALICE).status_code for _ in range(3)
    ]
    assert codes == [200, 200, 429]
