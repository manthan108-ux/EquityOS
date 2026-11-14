# ruff: noqa: F811
"""Automatic collection of a company's public documents from its own website (no network)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.acquisition import evidence_fetch as ef
from app.acquisition.http import FetchResult
from app.models import Document, EvidenceFetchRun, FetchStatus, ResearchStep
from app.research import orchestrator
from tests.research_helpers import complete_company
from tests.test_calc_db import co  # noqa: F401
from tests.test_research_vault import make_pdf
from tests.test_sector import seeded  # noqa: F401

pymupdf = pytest.importorskip("pymupdf")

SITE = "https://www.testco.com"
IT = ("it_services", "it_services_and_consulting")


def pdf(tag: str, issuer: str = "Reliance Industries Limited") -> bytes:
    return make_pdf([(200, 25, issuer), (60, 60, f"Annual report {tag}")])


def html(*links: tuple[str, str]) -> bytes:
    body = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
    return f"<html><body>{body}</body></html>".encode()


class FakeClient:
    """Answers from a dict of url -> bytes | FetchResult; anything else is a 404."""

    def __init__(self, pages: dict):
        self.pages, self.requests = pages, []

    def fetch(self, url, *, source, ttl=None, validate=None, **_):
        self.requests.append(url)
        page = self.pages.get(url)
        if page is None:
            return FetchResult(url, FetchStatus.NOT_FOUND, 404, error="not found")
        if isinstance(page, FetchResult):
            return page
        return FetchResult(
            url, FetchStatus.OK, 200, content=page, retrieved_at=datetime.now(UTC), attempts=1
        )

    def close(self):  # pragma: no cover - parity with PoliteClient
        pass


def site(extra: dict | None = None) -> dict:
    pages = {
        SITE: html(("/investors", "Investor Relations")),
        f"{SITE}/investors": html(
            ("/annual-reports", "Annual Reports"), ("/results", "Financial Results")
        ),
        f"{SITE}/annual-reports": html(
            ("/files/AR_2025-26.pdf", "Download"),
            ("/files/AR_2024-25.pdf", "Download"),
            ("/files/AR_2022-23.pdf", "Download"),
            ("/files/Sustainability-Report-2025-26.pdf", "Sustainability report"),
            ("https://cdn.elsewhere.com/AR_2025-26.pdf", "Download"),
            ("/files/Reliance-Jio.pdf", "Reliance Jio Infocomm: Annual Report 2025-26"),
        ),
        f"{SITE}/results": html(("/files/Q2-FY2027-results.pdf", "Quarterly results")),
        f"{SITE}/files/AR_2025-26.pdf": pdf("2025-26"),
        f"{SITE}/files/AR_2024-25.pdf": pdf("2024-25"),
        f"{SITE}/files/AR_2022-23.pdf": pdf("2022-23"),
        f"{SITE}/files/Q2-FY2027-results.pdf": pdf("Q2 FY2027"),
    }
    pages.update(extra or {})
    return pages


def run(session, co, pages=None, **kw):
    client = FakeClient(pages if pages is not None else site())
    kw.setdefault("data_dir", kw.pop("tmp", None))
    return ef.fetch_evidence(session, co, client, **kw), client


# ---- ranking ---------------------------------------------------------------------------------


def test_period_rank_orders_newest_first_and_puts_unknown_last():
    hints = ["FY2024", "Q2FY2027", None, "FY2026", "Q4FY2026"]
    assert sorted(hints, key=ef.period_rank, reverse=True) == [
        "Q2FY2027", "FY2026", "Q4FY2026", "FY2024", None,
    ]  # fmt: skip


def test_only_the_newest_four_annual_reports_are_selected():
    cands = [
        ef.Candidate(f"u{y}", "AR", "annual_report", f"FY{y}", "p")
        for y in (2021, 2022, 2026, 2025, 2024, 2023)
    ]
    chosen = [c.period_hint for c in ef.select_candidates(cands)]
    assert chosen == ["FY2026", "FY2025", "FY2024", "FY2023"]


def test_a_second_file_for_the_same_period_is_dropped_and_own_text_wins():
    inherited = ef.Candidate("a", "Download", "annual_report", "FY2026", "p", kind_inherited=True)
    own = ef.Candidate("b", "Annual Report 2025-26", "annual_report", "FY2026", "p")
    assert [c.url for c in ef.select_candidates([inherited, own])] == ["b"]


def test_landing_page_files_inherit_the_page_kind_but_not_esg_or_other_domains():
    page = html(
        ("/f/AR_2025-26.pdf", "Download"),
        ("/f/ESG-Report-2025-26.pdf", "ESG report"),
        ("https://cdn.elsewhere.com/AR_2025-26.pdf", "Download"),
    )
    found = ef.landing_candidates(page.decode(), f"{SITE}/annual-reports", "annual_report")
    assert [c.url for c in found] == [f"{SITE}/f/AR_2025-26.pdf"]
    assert found[0].kind == "annual_report" and found[0].period_hint == "FY2026"


# ---- the run ---------------------------------------------------------------------------------


def test_a_company_without_a_website_is_reported_and_nothing_is_requested(session, co, tmp_path):
    co.website = co.investor_relations_url = None
    report, client = run(session, co, tmp=tmp_path)
    assert report.status == ef.NO_WEBSITE and client.requests == []
    row = session.scalar(select(EvidenceFetchRun).where(EvidenceFetchRun.company_id == co.id))
    assert row.status == ef.NO_WEBSITE and "no website" in row.detail["notes"][0]


def test_an_access_denial_is_final_and_is_not_worked_around(session, co, tmp_path):
    co.website = SITE
    denied = FetchResult(SITE, FetchStatus.ACCESS_DENIED, 403, error="HTTP 403: access denied")
    report, client = run(session, co, {SITE: denied}, tmp=tmp_path)
    assert report.status == ef.SITE_UNREACHABLE
    assert "ACCESS_DENIED" in report.notes[0]
    assert client.requests == [SITE]  # no second attempt, no alternative path


def test_robots_disallowed_pages_are_reported_as_the_reason(session, co, tmp_path):
    co.website = SITE
    blocked = FetchResult(
        f"{SITE}/annual-reports", FetchStatus.BLOCKED_BY_ROBOTS, error="Disallowed by robots.txt"
    )
    pages = site({f"{SITE}/annual-reports": blocked, f"{SITE}/results": html()})
    report, _ = run(session, co, pages, tmp=tmp_path)
    assert any("BLOCKED_BY_ROBOTS" in n for n in report.notes)
    assert report.status == ef.NOTHING_FOUND


def test_a_reachable_site_with_no_files_says_so(session, co, tmp_path):
    co.website = SITE
    report, _ = run(session, co, {SITE: html(("/about", "About us"))}, tmp=tmp_path)
    assert report.status == ef.NOTHING_FOUND and "no downloadable" in report.notes[-1]


def test_the_newest_documents_are_downloaded_stored_and_linked(session, co, tmp_path):
    co.website = SITE
    report, client = run(session, co, tmp=tmp_path)
    assert report.status == ef.FETCHED
    docs = session.scalars(select(Document).where(Document.company_id == co.id)).all()
    by_type = sorted((d.doc_type, d.financial_period) for d in docs)
    assert by_type == [
        ("annual_report", "FY2023"),
        ("annual_report", "FY2025"),
        ("annual_report", "FY2026"),
        ("quarterly_result", "Q2FY2027"),
    ]
    assert all(d.association_status == "CONFIRMED" for d in docs)
    assert not any("Sustainability" in u or "elsewhere" in u for u in client.requests)


def test_annual_reports_beyond_the_newest_four_are_not_downloaded(session, co, tmp_path):
    co.website = SITE
    extra = {
        f"{SITE}/annual-reports": html(
            *[(f"/files/AR_{y}-{str(y + 1)[2:]}.pdf", "Download") for y in range(2020, 2026)]
        ),
        **{f"{SITE}/files/AR_{y}-{str(y + 1)[2:]}.pdf": pdf(str(y)) for y in range(2020, 2026)},
    }
    report, client = run(session, co, site(extra), tmp=tmp_path)
    held = sorted(
        d.financial_period
        for d in session.scalars(select(Document).where(Document.doc_type == "annual_report"))
    )
    assert held == ["FY2023", "FY2024", "FY2025", "FY2026"]
    assert not any("AR_2020-21" in u or "AR_2021-22" in u for u in client.requests)


def test_a_group_entity_document_is_skipped_not_stored(session, co, tmp_path):
    co.website = SITE
    pages = site({f"{SITE}/files/Reliance-Jio.pdf": pdf("jio", "Reliance Jio Infocomm Limited")})
    report, _ = run(session, co, pages, tmp=tmp_path)
    assert not any("Reliance-Jio" in o.url and o.status == "STORED" for o in report.outcomes)


def test_a_document_issued_by_someone_else_is_stored_but_not_linked(session, co, tmp_path):
    co.website = SITE
    pages = site({f"{SITE}/files/AR_2025-26.pdf": pdf("x", "Wholly Different Industries Limited")})
    report, _ = run(session, co, pages, tmp=tmp_path)
    assert any(o.status == "NOT_LINKED" for o in report.outcomes)
    linked = session.scalars(select(Document).where(Document.company_id == co.id)).all()
    assert all(d.financial_period != "FY2026" for d in linked)


def test_a_second_run_inside_the_cool_down_does_nothing(session, co, tmp_path):
    co.website = SITE
    run(session, co, tmp=tmp_path)
    report, client = run(session, co, tmp=tmp_path)
    assert report.status == ef.RECENT and client.requests == []


def test_a_forced_rerun_finds_everything_already_stored(session, co, tmp_path):
    co.website = SITE
    run(session, co, tmp=tmp_path)
    before = session.scalar(select(func.count()).select_from(Document))
    report, client = run(session, co, tmp=tmp_path, force=True)
    assert report.status == ef.UP_TO_DATE
    assert session.scalar(select(func.count()).select_from(Document)) == before
    assert not any(u.endswith(".pdf") for u in client.requests)  # no file downloaded twice


def test_the_cool_down_expires(session, co, tmp_path):
    co.website = SITE
    run(session, co, tmp=tmp_path)
    old = session.scalar(select(EvidenceFetchRun).where(EvidenceFetchRun.company_id == co.id))
    old.finished_at = datetime.now(UTC) - ef.COOL_DOWN - timedelta(hours=1)
    session.flush()
    report, _ = run(session, co, tmp=tmp_path)
    assert report.status == ef.UP_TO_DATE


def test_every_run_is_recorded_with_its_outcomes(session, co, tmp_path):
    co.website = SITE
    report, _ = run(session, co, tmp=tmp_path)
    row = ef.latest_run(session, co.id)
    assert row.status == ef.FETCHED and row.downloaded == 4
    statuses = [o["status"] for o in row.detail["outcomes"]]
    assert statuses.count("STORED") == 4 and statuses.count("SKIPPED") == 1  # the Jio file


def test_a_file_that_is_not_a_document_is_a_recorded_failure(session, co, tmp_path):
    co.website = SITE
    pages = site({f"{SITE}/files/AR_2025-26.pdf": b"%PDF-"})  # truncated, but passes the sniff
    report, _ = run(session, co, pages, tmp=tmp_path)
    assert report.outcomes  # the run finished and recorded every candidate


# ---- inside the research workflow ----------------------------------------------------------------


def _session(session, company):
    return orchestrator.create_session(session, company, "alice", company.common_name)


def test_the_documents_step_runs_the_fetcher_and_records_what_it_did(seeded, tmp_path):
    company = complete_company(seeded, "Fetch Co", *IT)
    seen = []

    def fetcher(session, comp, *, data_dir=None):
        seen.append(comp.id)
        return ef.FetchReport(ef.NO_WEBSITE, None, notes=["no website"])

    sess = orchestrator.run(
        seeded, _session(seeded, company), data_dir=tmp_path, document_fetcher=fetcher
    )
    step = seeded.scalar(
        select(ResearchStep).where(
            ResearchStep.session_id == sess.id, ResearchStep.step == "DOCUMENTS"
        )
    )
    assert seen == [company.id]
    assert step.detail["evidence_fetch"]["status"] == ef.NO_WEBSITE


def test_a_fetcher_that_crashes_never_fails_the_research(seeded, tmp_path):
    company = complete_company(seeded, "Fetch Co", *IT)

    def boom(session, comp, *, data_dir=None):
        raise RuntimeError("network exploded")

    sess = orchestrator.run(
        seeded, _session(seeded, company), data_dir=tmp_path, document_fetcher=boom
    )
    step = seeded.scalar(
        select(ResearchStep).where(
            ResearchStep.session_id == sess.id, ResearchStep.step == "DOCUMENTS"
        )
    )
    assert step.status == "DONE"
    assert step.detail["evidence_fetch"]["status"] == "ERROR"


def test_without_a_fetcher_the_step_makes_no_network_request(seeded, tmp_path):
    company = complete_company(seeded, "Fetch Co", *IT)
    sess = orchestrator.run(seeded, _session(seeded, company), data_dir=tmp_path)
    step = seeded.scalar(
        select(ResearchStep).where(
            ResearchStep.session_id == sess.id, ResearchStep.step == "DOCUMENTS"
        )
    )
    assert step.detail["evidence_fetch"] is None


# ---- storing a website ---------------------------------------------------------------------------


def _home(text="Reliance Industries Limited investor site"):
    return {SITE: f"<html><body><h1>{text}</h1></body></html>".encode()}


def test_a_checked_website_is_stored(session, co):
    co.website = None
    assert ef.set_website(session, co, SITE, FakeClient(_home())) == SITE
    assert co.website == SITE


def test_an_existing_website_is_never_changed(session, co):
    co.website = "https://old.example.com"
    with pytest.raises(ef.WebsiteRejected, match="already stored"):
        ef.set_website(session, co, SITE, FakeClient(_home()))
    assert co.website == "https://old.example.com"


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://www.testco.com",
        "http://127.0.0.1/",
        "https://localhost/",
        "https://10.0.0.5/",
        "www.testco.com",
    ],
)
def test_non_public_or_malformed_addresses_are_refused(session, co, bad):
    co.website = None
    client = FakeClient(_home())
    with pytest.raises(ef.WebsiteRejected):
        ef.set_website(session, co, bad, client)
    assert client.requests == [] and co.website is None  # refused before any request


def test_a_page_that_never_names_the_company_is_refused_unless_confirmed(session, co):
    co.website = None
    pages = _home("A completely unrelated shop")
    with pytest.raises(ef.WebsiteRejected, match="never mentions"):
        ef.set_website(session, co, SITE, FakeClient(pages))
    assert ef.set_website(session, co, SITE, FakeClient(pages), confirm_issuer=True) == SITE


def test_an_unreachable_page_is_not_stored(session, co):
    co.website = None
    denied = FetchResult(SITE, FetchStatus.ACCESS_DENIED, 403, error="HTTP 403")
    with pytest.raises(ef.WebsiteRejected, match="ACCESS_DENIED"):
        ef.set_website(session, co, SITE, FakeClient({SITE: denied}))
    assert co.website is None
