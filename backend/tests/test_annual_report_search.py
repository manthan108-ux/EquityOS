# ruff: noqa: F811
"""Deeper annual-report discovery and the per-year coverage record (no network)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.acquisition import annual_reports as ar
from app.acquisition import doc_coverage as cov
from app.acquisition import evidence_fetch as ef
from app.acquisition.http import FetchResult
from app.models import Document, EvidenceFetchRun, FetchStatus
from tests.test_calc_db import co  # noqa: F401
from tests.test_evidence_fetch import SITE, FakeClient, html, pdf, run
from tests.test_sector import seeded  # noqa: F401

pytest.importorskip("pymupdf")
TODAY = date(2026, 11, 14)  # the latest fiscal year is FY2026


def deep_site(extra: dict | None = None) -> dict:
    """The newest report on the landing page; older years only behind an 'Archive' link."""
    pages = {
        SITE: html(("/investors", "Investor Relations"), ("/blog/our-annual-report-story", "Blog")),
        f"{SITE}/investors": html(("/annual-reports", "Annual Reports")),
        f"{SITE}/annual-reports": html(
            ("/files/AR_2025-26.pdf", "Download"), ("/annual-reports/archive", "Archive")
        ),
        f"{SITE}/annual-reports/archive": html(
            ("/files/AR_2024-25.pdf", "2024-25"),
            ("/files/AR_2023-24.pdf", "2023-24"),
            ("/files/AR_2022-23.pdf", "2022-23"),
            ("/files/AR_2021-22.pdf", "2021-22"),
        ),
        **{
            f"{SITE}/files/AR_{y}-{str(y + 1)[2:]}.pdf": pdf(f"{y}-{y + 1}")
            for y in range(2021, 2026)
        },
    }
    pages.update(extra or {})
    return pages


def refuse(status: FetchStatus, code: int, why: str):
    return lambda url: FetchResult(url, status, code, error=why)


# ---- finding the archive -------------------------------------------------------------------------


def test_the_archive_behind_a_link_is_found_and_four_years_are_stored(session, co, tmp_path):
    co.website = SITE
    report, client = run(session, co, deep_site(), tmp=tmp_path)
    held = sorted(
        d.financial_period
        for d in session.scalars(select(Document).where(Document.doc_type == "annual_report"))
    )
    assert held == ["FY2023", "FY2024", "FY2025", "FY2026"]
    assert report.deep["found"] >= 5 and report.deep["pages"] >= 3
    assert not any("/blog/" in u for u in client.requests), "blog pages are noise, not followed"
    assert not any("AR_2021-22" in u for u in client.requests if u.endswith(".pdf")), "only four"


def test_the_sitemap_lists_reports_the_pages_do_not(session, co, tmp_path):
    co.website = SITE
    sitemap_index = (
        b"<sitemapindex><sitemap><loc>https://www.testco.com/sitemap-investors.xml</loc></sitemap>"
        b"<sitemap><loc>https://www.testco.com/sitemap-products.xml</loc></sitemap></sitemapindex>"
    )
    investors = "".join(
        f"<url><loc>{SITE}/files/Annual-Report-{y}-{str(y + 1)[2:]}.pdf</loc></url>"
        for y in range(2022, 2026)
    ).encode()
    pages = {
        SITE: html(("/investors", "Investor Relations")),
        f"{SITE}/investors": html(("/results", "Results")),
        f"{SITE}/sitemap.xml": sitemap_index,
        f"{SITE}/sitemap-investors.xml": b"<urlset>" + investors + b"</urlset>",
        **{
            f"{SITE}/files/Annual-Report-{y}-{str(y + 1)[2:]}.pdf": pdf(str(y))
            for y in range(2022, 2026)
        },
    }
    report, _ = run(session, co, pages, tmp=tmp_path)
    held = sorted(
        d.financial_period
        for d in session.scalars(select(Document).where(Document.doc_type == "annual_report"))
    )
    assert held == ["FY2023", "FY2024", "FY2025", "FY2026"]
    assert report.deep["sitemap_urls"] == 4


def test_esg_reports_and_other_domains_are_never_taken(session, co, tmp_path):
    co.website = SITE
    extra = {
        f"{SITE}/annual-reports/archive": html(
            ("/files/AR_2024-25.pdf", "2024-25"),
            ("/files/Sustainability-Report-2023-24.pdf", "2023-24 sustainability report"),
            ("https://cdn.elsewhere.com/AR_2023-24.pdf", "2023-24"),
        )
    }
    _, client = run(session, co, deep_site(extra), tmp=tmp_path)
    assert not any("Sustainability" in u or "elsewhere" in u for u in client.requests)


def test_the_search_stops_at_its_page_budget():
    pages = {SITE: html(("/investors", "Investor Relations"))}
    for i in range(60):
        pages[f"{SITE}/investors" if i == 0 else f"{SITE}/annual-reports/p{i}"] = html(
            *[
                (f"/annual-reports/p{i * 3 + k + 1}", f"Annual reports archive {i}-{k}")
                for k in range(3)
            ]
        )
    client = FakeClient(pages)
    result = ar.discover(client, [SITE], max_pages=10, use_sitemap=False)
    assert result.pages_fetched == 10 and len(client.requests) == 10


# ---- refusals are reasons, not obstacles to get around ------------------------------------------------


def test_a_site_that_refuses_us_is_recorded_and_not_worked_around(session, co, tmp_path):
    co.website = SITE
    denied = FetchResult(
        f"{SITE}/annual-reports", FetchStatus.ACCESS_DENIED, 403, error="HTTP 403: access denied"
    )
    pages = deep_site({f"{SITE}/annual-reports": denied})
    report, client = run(session, co, pages, tmp=tmp_path)
    assert any("ACCESS_DENIED" in b for b in report.deep["blocked"])
    assert client.requests.count(f"{SITE}/annual-reports") == 1, "asked once, never retried around"
    coverage = ef.latest_run(session, co.id).detail["coverage"]
    reasons = {y["reason_code"] for y in coverage["years"] if y["status"] == "MISSING"}
    assert reasons == {"SITE_REFUSES"}


def test_robots_disallowed_files_give_their_own_reason(session, co, tmp_path):
    co.website = SITE
    blocked = FetchResult(
        f"{SITE}/annual-reports",
        FetchStatus.BLOCKED_BY_ROBOTS,
        None,
        error="Disallowed by robots.txt",
    )
    pages = {
        SITE: html(("/investors", "Investor Relations")),
        f"{SITE}/investors": html(("/annual-reports", "Annual Reports")),
        f"{SITE}/annual-reports": blocked,
    }
    run(session, co, pages, tmp=tmp_path)
    coverage = ef.latest_run(session, co.id).detail["coverage"]
    assert {y["reason_code"] for y in coverage["years"]} == {"ROBOTS_DISALLOW"}


def test_a_site_with_nothing_in_its_static_pages_says_so(session, co, tmp_path):
    co.website = SITE
    run(session, co, {SITE: html(("/about", "About us"))}, tmp=tmp_path)
    coverage = ef.latest_run(session, co.id).detail["coverage"]
    assert {y["reason_code"] for y in coverage["years"]} == {"NOT_IN_STATIC_PAGES"}
    assert "script" in coverage["years"][0]["reason"]


# ---- the coverage record ------------------------------------------------------------------------------


def test_targets_are_the_latest_four_fiscal_years():
    assert cov.target_years(date(2026, 11, 14)) == ["FY2026", "FY2025", "FY2024", "FY2023"]
    assert cov.target_years(date(2026, 5, 1)) == ["FY2025", "FY2024", "FY2023", "FY2022"]


def test_coverage_lists_held_years_with_their_provenance_and_missing_ones_with_a_reason(
    session, co, tmp_path
):
    co.website = SITE
    pages = deep_site()
    # only two of the four target years are reachable: the archive lists just one older report
    pages[f"{SITE}/annual-reports/archive"] = html(("/files/AR_2024-25.pdf", "2024-25"))
    run(session, co, pages, tmp=tmp_path)
    c = cov.company_coverage(session, co, TODAY)
    by = {y["fy"]: y for y in c["years"]}
    assert (c["have"], c["missing"]) == (2, 2)
    held = by["FY2026"]["documents"][0]
    assert held["source_url"].endswith("AR_2025-26.pdf") and held["sha256"]
    assert held["retrieved_at"] and held["financial_period"] == "FY2026"
    assert held["extraction"] == "NOT_READ"
    miss = by["FY2024"]
    assert miss["status"] == "MISSING" and miss["reason_code"] == "NOT_LINKED"
    assert "uploaded by the user" in miss["user_supplied"]
    assert "manual" not in miss and "inbox" not in str(miss), "nobody is told to fetch it by hand"


def test_a_company_nobody_has_looked_at_is_not_called_missing_for_a_site_reason(session, co):
    c = cov.company_coverage(session, co, TODAY)
    assert c["have"] == 0 and {y["reason_code"] for y in c["years"]} == {"NOT_LOOKED_FOR"}


def test_another_issuers_document_is_not_counted(session, co, tmp_path):
    co.website = SITE
    run(session, co, deep_site(), tmp=tmp_path)
    doc = session.scalar(select(Document).where(Document.financial_period == "FY2026"))
    doc.association_status = "MISMATCH"
    session.flush()
    c = cov.company_coverage(session, co, TODAY)
    assert {y["fy"]: y["status"] for y in c["years"]}["FY2026"] == "MISSING"


def test_older_and_period_unknown_documents_are_listed_not_lost(session, co, tmp_path):
    co.website = SITE
    run(session, co, deep_site(), tmp=tmp_path)
    docs = list(session.scalars(select(Document).where(Document.doc_type == "annual_report")))
    docs[0].financial_period = "FY2019"
    docs[1].financial_period = None
    docs[1].title, docs[1].url = "Annual report", "https://www.testco.com/files/x.pdf"
    session.flush()
    c = cov.company_coverage(session, co, TODAY)
    assert "FY2019" in c["older_held"] and len(c["period_unknown"]) == 1


# ---- the cool-down ------------------------------------------------------------------------------------


def test_a_run_from_before_the_deeper_search_does_not_block_a_new_one(session, co, tmp_path):
    co.website = SITE
    session.add(
        EvidenceFetchRun(
            company_id=co.id,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            status="NOTHING_FOUND",
            website=SITE,
            candidates_found=0,
            downloaded=0,
            detail={"status": "NOTHING_FOUND", "notes": []},  # no search_version: an old run
        )
    )
    session.flush()
    report, _ = run(session, co, deep_site(), tmp=tmp_path)
    assert report.status != ef.RECENT and report.downloaded >= 4
    again, _ = run(session, co, deep_site(), tmp=tmp_path)
    assert again.status == ef.RECENT, "a run with the deeper search is respected as recent"


# ---- the investor-relations page is not a blog post about investing --------------------------------


def test_the_investor_page_is_chosen_over_an_article_that_mentions_investors():
    from app.acquisition.website_discovery import find_ir_url

    home = html(
        ("/blogs/investments/8-common-habits-of-successful-investors", "Habits of investors"),
        ("/news/investor-day-announced", "Investor day"),
        ("/about-us/investor-relations/overview/details/extra", "Investor details"),
        ("/investors", "Investor Relations"),
    ).decode()
    client = FakeClient(
        {
            f"{SITE}/investors": html(("/annual-reports", "Annual Reports")),
            f"{SITE}/blogs/investments/8-common-habits-of-successful-investors": html(),
            f"{SITE}/news/investor-day-announced": html(),
        }
    )
    assert find_ir_url(home, SITE, client) == f"{SITE}/investors"
    assert not any("/blogs/" in u or "/news/" in u for u in client.requests)


def test_no_investor_page_is_better_than_an_article():
    from app.acquisition.website_discovery import find_ir_url

    home = html(("/blog/investors-guide", "Investors guide")).decode()
    assert find_ir_url(home, SITE, FakeClient({})) is None


# ---- what the pipeline stage reports -------------------------------------------------------------------


def test_the_documents_stage_reports_years_held_and_why_the_rest_are_missing(session, co, tmp_path):
    from app.models import PipelineStageState
    from app.pipeline import runner

    co.website = SITE
    only_latest = deep_site({f"{SITE}/annual-reports/archive": html()})
    res = runner.run_company(
        session,
        co,
        stages=("documents",),
        client=FakeClient(only_latest),
        data_dir=tmp_path,
    )
    assert "annual reports: 1 of 4 years" in res.steps["documents"]
    state = session.get(PipelineStageState, (co.id, "documents"))
    summary = state.detail["annual_reports"]
    assert summary["have"] == 1 and summary["of"] == 4
    assert set(summary["missing"].values()) == {"NOT_LINKED"}
    assert set(summary["missing"]) == {"FY2025", "FY2024", "FY2023"}


# ---- large files and large sitemaps ---------------------------------------------------------------------


def test_a_big_sitemap_never_crowds_out_the_report_entries():
    product_pages = "".join(f"<url><loc>{SITE}/products/item-{i}</loc></url>" for i in range(7000))
    reports = (
        f"<url><loc>{SITE}/files/Annual-Report-2025-26.pdf</loc></url>"
        f"<url><loc>{SITE}/investors/annual-report</loc></url>"
    )
    client = FakeClient(
        {f"{SITE}/sitemap.xml": f"<urlset>{product_pages}{reports}</urlset>".encode()}
    )
    result = ar.DeepResult()
    urls = ar.sitemap_urls(client, SITE, result)
    assert len(urls) == 2 and result.sitemap_urls == 7002
    assert any(u.endswith("Annual-Report-2025-26.pdf") for u in urls)


def test_document_downloads_get_a_longer_timeout_than_pages(monkeypatch):
    import httpx

    from app.acquisition.http import ClientConfig, PoliteClient
    from app.providers import documents as docs

    seen: list = []

    class Spy(httpx.Client):
        def stream(self, method, url, **kw):
            seen.append(kw.get("timeout"))
            return super().stream(method, url, **kw)

    client = PoliteClient(
        ClientConfig(user_agent="t", respect_robots=False),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"%PDF-1.4 x")),
    )
    client._http = Spy(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x")))
    client.fetch("https://example.invalid/a", source="t")
    client.fetch("https://example.invalid/b.pdf", source="t", timeout_s=docs.DOWNLOAD_TIMEOUT_S)
    assert seen == [None, 180.0]


def test_a_linked_report_the_site_refuses_to_deliver_gets_its_own_reason(session, co, tmp_path):
    co.website = SITE
    refused = FetchResult(
        f"{SITE}/files/AR_2025-26.pdf",
        FetchStatus.ACCESS_DENIED,
        403,
        error="HTTP 403: access denied",
    )
    run(session, co, deep_site({f"{SITE}/files/AR_2025-26.pdf": refused}), tmp=tmp_path)
    c = cov.company_coverage(session, co, TODAY)
    latest = {y["fy"]: y for y in c["years"]}["FY2026"]
    assert latest["status"] == "MISSING" and latest["reason_code"] == "DOWNLOAD_REFUSED"
    assert "403" in latest["reason"] and "not worked around" in latest["reason"]
