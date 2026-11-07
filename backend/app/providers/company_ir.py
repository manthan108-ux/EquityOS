"""Discover a company's investor-relations pages from its official website.

Returns CANDIDATES only. Recording one as `companies.investor_relations_url` is a separate,
deliberate step so that discovery never overwrites verified data.
"""

import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.acquisition.http import FetchResult, PoliteClient

TTL = timedelta(days=1)

# (kind, pattern on the link text or URL), checked in order; the first match wins.
_KINDS = [
    ("annual_report", re.compile(r"annual[\s_-]*report", re.I)),
    (
        "financial_results",
        re.compile(r"(financial|quarterly|results?)[\s_-]*(results?|statements?)?", re.I),
    ),
    ("investor_relations", re.compile(r"investor|shareholder|stakeholder", re.I)),
]
_RESULT_WORDS = re.compile(r"financial|quarter|results|statement", re.I)


@dataclass(frozen=True)
class IrLink:
    url: str
    text: str
    kind: str  # investor_relations | annual_report | financial_results


@dataclass
class IrDiscovery:
    website: str
    fetch: FetchResult
    links: list[IrLink]


def validate_html(body: bytes, _ctype: str | None) -> str | None:
    return None if b"<" in body[:5000] else "not HTML"


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def extract_ir_links(html: str, base_url: str) -> list[IrLink]:
    """Find links that look like investor-relations material, on the company's own domain."""
    root = _host(base_url).split(":")[0]
    root = ".".join(root.split(".")[-2:]) if root.count(".") >= 1 else root
    found: dict[str, IrLink] = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if urlsplit(url).scheme not in ("http", "https") or root not in _host(url):
            continue  # stay on the company's own domain
        text = " ".join(a.get_text(" ").split())[:150]
        haystack = f"{text} {urlsplit(url).path}"
        for kind, pattern in _KINDS:
            if kind == "financial_results" and not _RESULT_WORDS.search(haystack):
                continue
            if pattern.search(haystack):
                found.setdefault(url, IrLink(url, text, kind))
                break
    return sorted(found.values(), key=lambda link: (link.kind, link.url))


class CompanyIrProvider:
    source_code = "COMPANY_IR"

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def discover(self, website: str) -> IrDiscovery:
        fetch = self._client.fetch(
            website, source=self.source_code, ttl=TTL, validate=validate_html
        )
        links = extract_ir_links(fetch.text, website) if fetch.ok else []
        return IrDiscovery(website, fetch, links)
