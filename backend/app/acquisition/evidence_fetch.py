"""Automatic evidence collection: from a company to its own public documents.

Given a company, this finds the investor-relations material on the company's OWN website, picks
the newest annual report(s), results and presentation, downloads them through the polite client
and stores them as documents. Extraction, validation and calculation then run through the normal
pipeline: nothing here reads a number.

Rules it keeps (they are the project's rules, not options):
* robots.txt is obeyed, requests are paced, and an access denial (401/403/418/bot challenge) is a
  final answer: it is recorded as the reason, never worked around;
* only the company's own domain is followed;
* a link labelled for another group entity, or for ESG/AGM material, is not taken;
* the period of a file is a HINT inferred from its link text, and the document is stored with
  that label only as a hint: the extraction stage reads the real period from the document;
* every outcome, including every failure and its reason, is recorded in `evidence_fetch_runs`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.acquisition.http import FetchResult, PoliteClient
from app.docintel.entity import company_names
from app.models import Company, Document, EvidenceFetchRun
from app.providers.company_ir import (
    _NOT_AN_ANNUAL_REPORT,
    _NOT_FINANCIAL_RESULTS,
    CompanyIrProvider,
    IrLink,
    _host,
    _registrable,
    extract_ir_links,
    guess_period,
    names_group_entity,
    validate_html,
)
from app.providers.documents import DocumentProvider

# Statuses of one run, from most to least useful. The dashboard shows them as the reason.
FETCHED = "FETCHED"  # at least one new document was stored
UP_TO_DATE = "UP_TO_DATE"  # everything found is already stored
NO_WEBSITE = "NO_WEBSITE"  # no website is known for this company
SITE_UNREACHABLE = "SITE_UNREACHABLE"  # the website could not be fetched (reason recorded)
NOTHING_FOUND = "NOTHING_FOUND"  # reachable, but no investor documents could be located
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"  # documents located, none could be downloaded
RECENT = "RECENT"  # a run already happened inside the cool-down; nothing was fetched

COOL_DOWN = timedelta(days=7)
KIND_TO_DOC_TYPE = {
    "annual_report": "annual_report",
    "financial_results": "quarterly_result",
    "investor_presentation": "investor_presentation",
}
# How many of each to keep: the newest only. A results PDF is small, an annual report is large.
LIMITS = {"annual_report": 2, "financial_results": 4, "investor_presentation": 1}
MAX_LANDING_PAGES = 6
_PDF = re.compile(r"\.pdf(?:$|[?/])", re.I)


@dataclass
class Candidate:
    url: str
    text: str
    kind: str
    period_hint: str | None
    found_on: str
    kind_inherited: bool = False  # the kind came from the page it was found on, not its own text


@dataclass
class Outcome:
    url: str
    kind: str
    period_hint: str | None
    status: str  # STORED | HAVE | NOT_LINKED | FAILED | SKIPPED
    detail: str = ""
    document_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "kind": self.kind,
            "period_hint": self.period_hint,
            "status": self.status,
            "detail": self.detail,
            "document_id": self.document_id,
        }


@dataclass
class FetchReport:
    status: str
    website: str | None = None
    candidates: int = 0
    outcomes: list[Outcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def downloaded(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("STORED", "NOT_LINKED"))

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "website": self.website,
            "candidates": self.candidates,
            "downloaded": self.downloaded,
            "outcomes": [o.as_dict() for o in self.outcomes],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------------- ranking


def period_rank(hint: str | None) -> tuple[int, int]:
    """Sort key, newest first when reversed. Unknown periods rank last.

    'FY2026' -> (2026, 5); 'Q2FY2027' -> (2027, 2). A full-year report outranks any quarter of
    the same year only for annual reports, where quarters never occur, so the order is safe.
    """
    if not hint:
        return (0, 0)
    q = re.fullmatch(r"Q([1-4])FY(\d{4})", hint)
    if q:
        return (int(q.group(2)), int(q.group(1)))
    fy = re.fullmatch(r"FY(\d{4})", hint)
    if fy:
        return (int(fy.group(1)), 5)
    return (0, 0)


def select_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """The newest few of each kind; duplicates by URL removed; same-period duplicates kept once."""
    chosen: list[Candidate] = []
    for kind, limit in LIMITS.items():
        pool = {c.url: c for c in candidates if c.kind == kind}.values()
        # own-text kind beats an inherited one, then newest period, then a stable URL order
        ranked = sorted(
            pool,
            key=lambda c: (c.kind_inherited, tuple(-x for x in period_rank(c.period_hint)), c.url),
        )
        seen_periods: set[str | None] = set()
        for c in ranked:
            if c.period_hint is not None and c.period_hint in seen_periods:
                continue  # a second file for the same period; the first (own-text) one is kept
            seen_periods.add(c.period_hint)
            chosen.append(c)
            if sum(1 for x in chosen if x.kind == kind) >= limit:
                break
    return chosen


# --------------------------------------------------------------------------- page reading


def landing_candidates(html: str, page_url: str, inherited_kind: str) -> list[Candidate]:
    """Document links on a landing page (e.g. an 'Annual Reports' page).

    The page itself names the kind ('Annual Reports'), so a PDF whose own text is just 'Download'
    inherits it. Links are kept to the company's own domain; ESG/AGM material is dropped.
    """
    root = _registrable(page_url)
    found: dict[str, Candidate] = {}
    for link in extract_ir_links(html, page_url):
        if link.is_document and link.kind in KIND_TO_DOC_TYPE:
            found[link.url] = Candidate(
                link.url, link.text, link.kind, link.period_hint, page_url, False
            )
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(page_url, a["href"].strip()).split("#")[0]
        if urlsplit(url).scheme not in ("http", "https") or root not in _host(url):
            continue
        if not _PDF.search(url) or url in found:
            continue
        text = " ".join(a.get_text(" ").split())[:150]
        subject = f"{text} {urlsplit(url).path.rsplit('/', 1)[-1]}"
        if inherited_kind == "annual_report" and _NOT_AN_ANNUAL_REPORT.search(subject):
            continue
        if inherited_kind == "financial_results" and _NOT_FINANCIAL_RESULTS.search(subject):
            continue
        found[url] = Candidate(
            url,
            text,
            inherited_kind,
            guess_period(f"{subject} {urlsplit(url).path}"),
            page_url,
            True,
        )
    return list(found.values())


def _candidates_from(links: list[IrLink]) -> tuple[list[Candidate], list[IrLink]]:
    """Split discovery output into direct document candidates and landing pages to open."""
    direct: list[Candidate] = []
    pages: list[IrLink] = []
    for link in links:
        if link.kind not in KIND_TO_DOC_TYPE:
            continue
        if link.is_document or _PDF.search(urlsplit(link.url).path):
            direct.append(
                Candidate(link.url, link.text, link.kind, link.period_hint, link.found_on)
            )
        else:
            pages.append(link)
    return direct, pages


# ------------------------------------------------------------------------------ the run


def _recent_run(session: Session, company_id) -> EvidenceFetchRun | None:
    run = session.scalar(
        select(EvidenceFetchRun)
        .where(EvidenceFetchRun.company_id == company_id)
        .order_by(EvidenceFetchRun.started_at.desc())
        .limit(1)
    )
    if run and run.finished_at and datetime.now(UTC) - run.finished_at < COOL_DOWN:
        return run
    return None


def _already_have(session: Session, company_id, url: str) -> Document | None:
    return session.scalar(
        select(Document).where(Document.company_id == company_id, Document.url == url).limit(1)
    )


def _site_failure(fetch: FetchResult) -> str:
    return f"{fetch.status.value}: {fetch.error or 'no detail'}"[:300]


def fetch_evidence(
    session: Session,
    company: Company,
    client: PoliteClient,
    *,
    data_dir: Path | None = None,
    force: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FetchReport:
    """Collect the newest public documents from the company's own website.

    Never raises for a network or site problem: those become the report's status and notes.
    Commits after every stored document so partial progress survives an interruption.
    """
    started = now()
    if not force:
        recent = _recent_run(session, company.id)
        if recent is not None:
            return FetchReport(
                RECENT,
                recent.website,
                notes=[
                    f"documents were last looked for on {recent.finished_at:%Y-%m-%d} "
                    f"({recent.status}); not repeated within {COOL_DOWN.days} days"
                ],
            )

    seeds = [u for u in (company.website, company.investor_relations_url) if u]
    report = FetchReport(NO_WEBSITE, company.website or company.investor_relations_url)
    if not seeds:
        report.notes.append("no website is stored for this company, so nothing can be fetched")
        return _record(session, company, report, started, now())

    provider = CompanyIrProvider(client)
    discovery = provider.discover(seeds[0])
    if not discovery.fetch.ok and not discovery.links:
        report.status = SITE_UNREACHABLE
        report.notes.append(f"home page not retrieved - {_site_failure(discovery.fetch)}")
        return _record(session, company, report, started, now())

    links = list(discovery.links)
    if company.investor_relations_url:
        extra = provider._page(company.investor_relations_url)  # noqa: SLF001 - same polite fetch
        if extra.ok:
            links += extract_ir_links(extra.text, company.investor_relations_url)
    direct, pages = _candidates_from(links)

    # Annual-report / results landing pages usually hold the actual files one click deeper.
    for page in sorted(pages, key=lambda p: p.url)[:MAX_LANDING_PAGES]:
        fetched = client.fetch(page.url, source="COMPANY_IR", validate=validate_html)
        if not fetched.ok:
            report.notes.append(f"page {page.url} not retrieved - {_site_failure(fetched)}")
            continue
        direct += landing_candidates(fetched.text, page.url, page.kind)

    report.candidates = len({c.url for c in direct})
    # Parent companies host their subsidiaries' documents: drop those BEFORE choosing the newest,
    # otherwise a subsidiary's "Annual Report 2025-26" could displace the company's own.
    names = company_names(session, company.id)
    own: list[Candidate] = []
    for cand in direct:
        other = names_group_entity(cand.text, names)
        if other:
            report.outcomes.append(
                Outcome(cand.url, cand.kind, cand.period_hint, "SKIPPED", f"labelled for {other}")
            )
        else:
            own.append(cand)
    wanted = select_candidates(own)
    if not wanted:
        report.status = NOTHING_FOUND
        report.notes.append(
            "the website was reachable but no downloadable annual report, results or "
            "presentation file could be located (it may be script-driven or published elsewhere)"
        )
        return _record(session, company, report, started, now())

    downloader = DocumentProvider(client, data_dir)
    for cand in wanted:
        doc_type = KIND_TO_DOC_TYPE[cand.kind]
        have = _already_have(session, company.id, cand.url)
        if have is not None:
            report.outcomes.append(
                Outcome(
                    cand.url, cand.kind, cand.period_hint, "HAVE", "already stored", str(have.id)
                )
            )
            continue
        try:
            stored = downloader.download(
                session,
                cand.url,
                doc_type=doc_type,
                company_id=company.id,
                title=cand.text or None,
                financial_period=cand.period_hint,
            )
        except ValueError as exc:  # an unsafe or empty file: recorded, never fatal
            session.rollback()
            report.outcomes.append(
                Outcome(cand.url, cand.kind, cand.period_hint, "FAILED", str(exc)[:200])
            )
            continue
        session.commit()
        if stored.document is None:
            report.outcomes.append(
                Outcome(cand.url, cand.kind, cand.period_hint, "FAILED", stored.error or "failed")
            )
        elif not stored.created:
            report.outcomes.append(
                Outcome(
                    cand.url,
                    cand.kind,
                    cand.period_hint,
                    "HAVE",
                    "the same file is already stored",
                    str(stored.document.id),
                )  # fmt: skip
            )
        elif stored.document.company_id is None:
            report.outcomes.append(
                Outcome(
                    cand.url,
                    cand.kind,
                    cand.period_hint,
                    "NOT_LINKED",
                    f"stored but not linked to this company: {stored.document.association_note}",
                    str(stored.document.id),
                )  # fmt: skip
            )
        else:
            report.outcomes.append(
                Outcome(
                    cand.url,
                    cand.kind,
                    cand.period_hint,
                    "STORED",
                    "period is a hint from the link text; extraction reads the real one",
                    str(stored.document.id),
                )  # fmt: skip
            )

    if report.downloaded:
        report.status = FETCHED
    elif any(o.status == "HAVE" for o in report.outcomes):
        report.status = UP_TO_DATE
    else:
        report.status = DOWNLOAD_FAILED
    return _record(session, company, report, started, now())


def _record(
    session: Session, company: Company, report: FetchReport, started: datetime, finished: datetime
) -> FetchReport:
    session.add(
        EvidenceFetchRun(
            company_id=company.id,
            started_at=started,
            finished_at=finished,
            status=report.status,
            website=report.website,
            candidates_found=report.candidates,
            downloaded=report.downloaded,
            detail=report.as_dict(),
        )
    )
    session.commit()
    return report


def latest_run(session: Session, company_id) -> EvidenceFetchRun | None:
    return session.scalar(
        select(EvidenceFetchRun)
        .where(EvidenceFetchRun.company_id == company_id)
        .order_by(EvidenceFetchRun.started_at.desc())
        .limit(1)
    )


# ------------------------------------------------------------------------- storing a website


class WebsiteRejected(ValueError):
    """The URL was not stored; the message says why."""


def _public_host(url: str) -> str:
    """The host of an http(s) URL that is a public domain name, else WebsiteRejected."""
    import ipaddress

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise WebsiteRejected("the address must start with http:// or https://")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise WebsiteRejected("a raw IP address is not accepted; use the company's domain name")
    if "." not in host or host.endswith((".local", ".internal", ".localhost")):
        raise WebsiteRejected("that is not a public domain name")
    return host


def set_website(
    session: Session,
    company: Company,
    url: str,
    client: PoliteClient,
    *,
    confirm_issuer: bool = False,
) -> str:
    """Store a company's website after checking it. Additive: an existing value is never changed.

    The page is fetched politely (robots.txt obeyed). Unless the supplier confirms the issuer, the
    page must mention the company's name, which stops a wrong site being attached by accident.
    """
    if company.website:
        raise WebsiteRejected(f"a website is already stored ({company.website}); it is not changed")
    _public_host(url)
    home = client.fetch(url, source="COMPANY_IR", validate=validate_html)
    if not home.ok:
        raise WebsiteRejected(f"the page could not be retrieved - {_site_failure(home)}")
    if not confirm_issuer:
        from app.core.text import normalize_text

        page = normalize_text(BeautifulSoup(home.text, "html.parser").get_text(" "))
        names = {n for n in company_names(session, company.id) if len(n) >= 4}
        if not any(name in page for name in names):
            raise WebsiteRejected(
                "the page never mentions this company's name; if you are sure it is the right "
                "site, confirm the issuer explicitly"
            )
    company.website = url.rstrip("/")
    session.commit()
    return company.website
