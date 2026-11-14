"""Deeper discovery of a company's annual reports on its own website.

The standard collection (`evidence_fetch`) opens the home page, the investor-relations page and a
few landing pages. That finds the latest report on most sites but misses the archive: older years
sit one or two clicks deeper, behind "Archive" or per-year links, or are only listed in the
site's sitemap.
This module goes further, politely and within limits:

* a bounded breadth-first walk over investor-relations and annual-report pages (own domain only),
  following "archive" and per-year links on annual-report pages;
* the site's `sitemap.xml` (and the child sitemaps it names), read for annual-report URLs;
* every request goes through the shared polite client (robots.txt obeyed, request spacing, nothing
  bypassed). A page that refuses us (403, robots.txt) is recorded as a reason, never worked around.

It only FINDS candidate files and says why anything is missing; storing them is the caller's job.
"""

import re
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.acquisition.http import FetchResult, PoliteClient
from app.providers.company_ir import (
    _NOT_AN_ANNUAL_REPORT,
    _registrable,
    extract_ir_links,
    guess_period,
    not_a_financial_file,
    validate_html,
)

TARGET_YEARS = 4
MAX_PAGES = 24  # pages fetched per company, sitemap files included
MAX_DEPTH = 3
MAX_SITEMAP_FILES = 6
MAX_SITEMAP_URLS = 5000  # annual-report-looking entries kept
_PDF = re.compile(r"\.pdf(?:$|[?/])", re.I)
_LOC = re.compile(r"<loc>\s*([^<\s][^<]*?)\s*</loc>", re.I)
_AR_WORDS = re.compile(r"annual[\s_%20-]*report|integrated[\s_-]*(annual[\s_-]*)?report", re.I)
_ARCHIVE_TEXT = re.compile(
    r"archive|previous|past|older|earlier|prior|"
    r"20\d{2}\s*[-_/–]\s*(\d{2}|20\d{2})|(?<![\d.])(19|20)\d{2}(?![\d.])",
    re.I,
)
_IR_WORDS = re.compile(r"invest|shareholder|stakeholder|financial|report", re.I)
# Pages that look relevant by word but are not where reports live.
_NOISE = re.compile(
    r"/(blog|blogs|news|press|media|career|careers|jobs|events|article|articles|stories|"
    r"insights|login|search|contact)(/|$)",
    re.I,
)


@dataclass
class Found:
    """A candidate annual-report file."""

    url: str
    text: str
    period_hint: str | None
    found_on: str
    inherited: bool = False  # it said "Download"; the page it sits on said "Annual Reports"


@dataclass
class DeepResult:
    found: list[Found] = field(default_factory=list)
    pages_fetched: int = 0
    blocked: list[str] = field(default_factory=list)  # pages that refused us, and why
    notes: list[str] = field(default_factory=list)
    sitemap_urls: int = 0

    def years(self) -> set[str]:
        return {
            f.period_hint for f in self.found if f.period_hint and f.period_hint.startswith("FY")
        }


def refusal(fetch: FetchResult) -> str:
    return f"{fetch.status.value}: {fetch.error or 'no detail'}"[:200]


def is_noise(url: str) -> bool:
    """A page that mentions reports but is not where they live (a blog post, a news item)."""
    return bool(_NOISE.search(urlsplit(url).path))


def _own_domain(url: str, root: str) -> bool:
    return root in (urlsplit(url).hostname or "")


def _is_report_page(text: str, url: str) -> bool:
    return bool(_AR_WORDS.search(f"{text} {urlsplit(url).path}"))


def sitemap_urls(client: PoliteClient, website: str, result: DeepResult) -> list[str]:
    """URLs from the site's sitemap(s). A missing or refused sitemap is simply not used."""
    origin = f"{urlsplit(website).scheme}://{urlsplit(website).netloc}"
    pending = deque([f"{origin}/sitemap.xml"])
    seen: set[str] = set()
    urls: list[str] = []
    files = 0
    read = 0
    while pending and files < MAX_SITEMAP_FILES and result.pages_fetched < MAX_PAGES:
        url = pending.popleft()
        if url in seen:
            continue
        seen.add(url)
        fetched = client.fetch(url, source="COMPANY_IR")
        result.pages_fetched += 1
        if not fetched.ok:
            if files == 0:
                result.notes.append(f"no sitemap ({refusal(fetched)})")
            continue
        files += 1
        locs = _LOC.findall(fetched.text)
        children = [x for x in locs if x.lower().endswith(".xml") or "sitemap" in x.lower()]
        if children and len(children) == len(locs):  # a sitemap index: read the likeliest first
            children.sort(key=lambda c: (not _IR_WORDS.search(c), c))
            pending.extend(children)
            continue
        read += len(locs)
        # keep only what could be an annual report, so a very large sitemap (tens of thousands of
        # product pages) never crowds the few relevant entries out
        urls.extend(u for u in locs if _AR_WORDS.search(urlsplit(u).path))
        if len(urls) >= MAX_SITEMAP_URLS:
            break
    result.sitemap_urls = read
    return urls[:MAX_SITEMAP_URLS]


def discover(
    client: PoliteClient,
    seeds: list[str],
    *,
    max_pages: int = MAX_PAGES,
    max_depth: int = MAX_DEPTH,
    use_sitemap: bool = True,
    refused: dict[str, str] | None = None,
) -> DeepResult:
    """Find annual-report files starting from `seeds` (the website and its investor page).

    `refused` maps pages that already refused us in this run to why; they are not asked again.
    """
    from app.acquisition import evidence_fetch as ef  # lazy: evidence_fetch imports this module

    result = DeepResult()
    seeds = [s for s in dict.fromkeys(seeds) if s]
    if not seeds:
        return result
    root = _registrable(seeds[0])
    found: dict[str, Found] = {}
    queue: deque[tuple[str, int, bool]] = deque((s, 0, False) for s in seeds)
    seen: set[str] = set(seeds) | set(refused or {})
    for url, why in (refused or {}).items():
        result.blocked.append(f"{url} - {why}")
    queue = deque((s, d, r) for s, d, r in queue if s not in (refused or {}))

    def add_file(url: str, text: str, hint: str | None, page: str, inherited: bool) -> None:
        subject = f"{text} {urlsplit(url).path.rsplit('/', 1)[-1]}"
        if not_a_financial_file(subject) or _NOT_AN_ANNUAL_REPORT.search(subject):
            return
        found.setdefault(url, Found(url, text, hint, page, inherited))

    if use_sitemap:
        for url in sitemap_urls(client, seeds[0], result):
            if not _own_domain(url, root) or _NOISE.search(urlsplit(url).path):
                continue
            path = urlsplit(url).path
            if not _AR_WORDS.search(path):
                continue
            if _PDF.search(url):
                add_file(url, "", guess_period(path), "sitemap", False)
            elif url not in seen:
                seen.add(url)
                queue.append((url, 1, True))

    while queue and result.pages_fetched < max_pages:
        page_url, depth, on_report_page = queue.popleft()
        fetched = client.fetch(page_url, source="COMPANY_IR", validate=validate_html)
        result.pages_fetched += 1
        if not fetched.ok:
            result.blocked.append(f"{page_url} - {refusal(fetched)}")
            continue
        html = fetched.text
        report_page = on_report_page or _is_report_page("", page_url)
        # files on this page: those that say "annual report" themselves, plus (on a report page)
        # every PDF, which inherits the page's meaning
        for link in extract_ir_links(html, page_url):
            if link.kind == "annual_report" and link.is_document:
                add_file(link.url, link.text, link.period_hint, page_url, False)
        if report_page:
            for cand in ef.landing_candidates(html, page_url, "annual_report"):
                if cand.kind == "annual_report":
                    add_file(cand.url, cand.text, cand.period_hint, page_url, cand.kind_inherited)
        if depth >= max_depth:
            continue
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("#", "mailto:", "javascript:", "tel:")):
                continue
            url = urljoin(page_url, href).split("#")[0]
            text = " ".join(a.get_text(" ").split())[:150]
            if (
                url in seen
                or urlsplit(url).scheme not in ("http", "https")
                or not _own_domain(url, root)
                or _PDF.search(url)
                or urlsplit(url).path.lower().endswith((".zip", ".xlsx", ".xls", ".xml"))
                or _NOISE.search(urlsplit(url).path)
            ):
                continue
            about_reports = _is_report_page(text, url)
            archive_link = report_page and bool(_ARCHIVE_TEXT.search(text))
            if about_reports or archive_link or (depth == 0 and _IR_WORDS.search(f"{text} {url}")):
                seen.add(url)
                queue.append((url, depth + 1, about_reports or report_page))

    result.found = sorted(found.values(), key=lambda f: (f.period_hint or "", f.url), reverse=True)
    if not result.found:
        result.notes.append(
            f"no annual-report file found in {result.pages_fetched} page(s) "
            "(the reports may be loaded by script, or published elsewhere)"
        )
    return result
