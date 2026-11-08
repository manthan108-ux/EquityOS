"""Discover a company's investor-relations material from its official website.

Returns CANDIDATE links only. Recording one as `companies.investor_relations_url`, or downloading a
document, is a separate, deliberate step, so discovery never overwrites verified data.

Discovery looks at the home page, then follows at most MAX_IR_PAGES investor-relations pages found
there (one level deeper), because annual reports and results usually live a click away. Every
fetch goes through the polite client (robots.txt, pacing, access denials are final).
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.acquisition.http import FetchResult, PoliteClient
from app.core.text import normalize_text
from app.normalization import parse_period_label, quarter_label

TTL = timedelta(days=1)
MAX_IR_PAGES = 3
COMMON_IR_PATHS = (
    "/investor-relations",
    "/investors",
    "/personal/about-us/investor-relations",
    "/about-us/investor-relations",
    "/investor",
)
DOCUMENT_SUFFIXES = (".pdf", ".xlsx", ".xls", ".zip", ".xml")

# (kind, pattern on the link text or URL); the first matching kind wins, so order matters.
_KINDS = [
    ("annual_report", re.compile(r"annual[\s_-]*report|integrated[\s_-]*report", re.I)),
    ("earnings_transcript", re.compile(r"transcript|earnings[\s_-]*call|analyst[\s_-]*call", re.I)),
    ("investor_presentation", re.compile(r"presentation|investor[\s_-]*(deck|day|meet)", re.I)),
    (
        "financial_results",
        re.compile(
            r"(financial|quarter\w*|half[\s_-]*year\w*)[\s_-]*(results?|statements?)|results?", re.I
        ),
    ),
    ("investor_relations", re.compile(r"investor|shareholder|stakeholder", re.I)),
]
_NOT_AN_ANNUAL_REPORT = re.compile(r"esg|sustainab|brsr|csr|responsib|climate|tcfd", re.I)
_RESULT_WORDS = re.compile(r"financial|quarter|half|results|statement", re.I)
# Shareholder-meeting and procedural documents also contain the word "results".
_NOT_FINANCIAL_RESULTS = re.compile(
    r"voting|scrutini|postal[\s_-]*ballot|e-?voting|\bagm\b|notice|proxy|intimation|newspaper|advert",
    re.I,
)
# "2024-25", "2024_25", "FY2025", "FY 25": the fiscal year is the year in which it ends.
_FY_RANGE = re.compile(r"(20\d{2})\s*[-_/–]\s*(\d{2}|20\d{2})(?!\d)")
_QUARTER_ENDED = re.compile(
    r"(?:quarter|period|months|half[\s_-]*year)[\s_-]*ended[\s_-]*([A-Za-z]{3,9})[\s_.,-]*\d{1,2}[\s_.,-]*(20\d{2})",
    re.I,
)
_FQ_LABEL = re.compile(r"\bQ([1-4])[\s_-]*FY[\s_-]*(?:20)?(\d{2})\b", re.I)
_FY_LABEL = re.compile(r"\bFY\s*[-_]?\s*(20)?(\d{2})\b", re.I)


@dataclass(frozen=True)
class IrLink:
    url: str
    text: str
    kind: str  # investor_relations | annual_report | financial_results | ...
    found_on: str  # the page this link was found on
    is_document: bool = False  # points straight at a file (pdf, xlsx, ...)
    period_hint: str | None = None  # e.g. "FY2025", INFERRED from link text/URL, not verified


@dataclass
class IrDiscovery:
    website: str
    fetch: FetchResult  # the home page fetch
    links: list[IrLink] = field(default_factory=list)
    pages_fetched: list[FetchResult] = field(default_factory=list)  # every page looked at


def validate_html(body: bytes, _ctype: str | None) -> str | None:
    return None if b"<" in body[:5000] else "not HTML"


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def _registrable(url: str) -> str:
    host = _host(url).split(":")[0]
    return ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host


def guess_period(text: str) -> str | None:
    """Period hint from link text or URL: a fiscal quarter ('Q2FY2027') or year ('FY2025').

    A HINT only: it is inferred from wording, never read from the document, and the report must
    say so. Quarter phrases win over year phrases because results documents name a quarter.
    """
    fq = _FQ_LABEL.search(text)
    if fq:
        return f"Q{fq.group(1)}FY20{fq.group(2)}"
    ended = _QUARTER_ENDED.search(text)
    if ended:
        month = parse_period_label(f"{ended.group(1)} {ended.group(2)}")
        if month is not None and month.month in (3, 6, 9, 12):
            return quarter_label(month)
    range_match = _FY_RANGE.search(text)
    if range_match:
        start, end = range_match.group(1), range_match.group(2)
        year = int(end) if len(end) == 4 else int(start[:2] + end)
        if year == int(start) + 1:
            return f"FY{year}"
    label = _FY_LABEL.search(text)
    if label:
        return f"FY{(label.group(1) or '20')}{label.group(2)}"
    return None


def names_group_entity(text: str, company_names: set[str]) -> str | None:
    """The other group company a link is labelled for, or None.

    Parent companies host their subsidiaries' documents ("ICICI Prudential AMC: Investor
    Presentation" on icicibank.com). A link whose text starts with "<Name>:" where <Name> begins
    like the company's name but is not one of its names belongs to another entity. Prefixes that
    do not look like a group name (digits, or a different first word) are not judged here.
    """
    head, sep, _ = text.partition(":")
    if not sep:
        return None
    prefix = normalize_text(head)
    words = prefix.split()
    if not 1 <= len(words) <= 6 or any(ch.isdigit() for ch in prefix):
        return None
    if prefix in company_names:
        return None
    first_words = {name.split()[0] for name in company_names if name}
    return head.strip() if words[0] in first_words else None


def extract_ir_links(html: str, base_url: str) -> list[IrLink]:
    """Find links that look like investor-relations material, on the company's own domain."""
    root = _registrable(base_url)
    found: dict[str, IrLink] = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or root not in _host(url):
            continue  # stay on the company's own domain
        text = " ".join(a.get_text(" ").split())[:150]
        filename = parts.path.rsplit("/", 1)[-1]
        # Judge the link by its text and FILE NAME. A folder called /annual-reports/ also holds
        # ESG and other reports, so the directory alone must not decide the kind.
        primary = f"{text} {filename}"
        for kind, pattern in _KINDS:
            subject = primary if kind != "investor_relations" else f"{primary} {parts.path}"
            if kind == "financial_results" and (
                not _RESULT_WORDS.search(subject) or _NOT_FINANCIAL_RESULTS.search(primary)
            ):
                continue
            if kind == "annual_report" and _NOT_AN_ANNUAL_REPORT.search(primary):
                continue
            if pattern.search(subject):
                found.setdefault(
                    url,
                    IrLink(
                        url=url,
                        text=text,
                        kind=kind,
                        found_on=base_url,
                        is_document=parts.path.lower().endswith(DOCUMENT_SUFFIXES),
                        period_hint=guess_period(f"{primary} {parts.path}"),
                    ),
                )
                break
    return sorted(found.values(), key=lambda link: (link.kind, link.url))


class CompanyIrProvider:
    source_code = "COMPANY_IR"

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def _page(self, url: str) -> FetchResult:
        return self._client.fetch(url, source=self.source_code, ttl=TTL, validate=validate_html)

    def discover(self, website: str, *, follow: bool = True) -> IrDiscovery:
        home = self._page(website)
        discovery = IrDiscovery(website, home, pages_fetched=[home])
        if not home.ok:
            return discovery
        links = {link.url: link for link in extract_ir_links(home.text, website)}
        if follow:
            ir_pages = [
                link.url
                for link in links.values()
                if link.kind == "investor_relations" and not link.is_document
            ][:MAX_IR_PAGES]
            for page_url in ir_pages:
                page = self._page(page_url)
                discovery.pages_fetched.append(page)
                if page.ok:
                    for link in extract_ir_links(page.text, page_url):
                        links.setdefault(link.url, link)
        if follow and not any(link.is_document for link in links.values()):
            # The homepage did not lead to investor material: try the usual investor-relations
            # paths on the same site (each fetch still obeys robots.txt and is logged).
            for path in COMMON_IR_PATHS:
                page = self._page(urljoin(website.rstrip("/") + "/", path.lstrip("/")))
                discovery.pages_fetched.append(page)
                if not page.ok:
                    continue
                found = extract_ir_links(page.text, page.url if hasattr(page, "url") else website)
                for link in found:
                    links.setdefault(link.url, link)
                if any(link.is_document for link in found):
                    break
        discovery.links = sorted(links.values(), key=lambda link: (link.kind, link.url))
        return discovery
