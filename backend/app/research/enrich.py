"""Source-grounded enrichment of a company's history, leadership and business description.

Where the stored filings say little, the company's OWN public web pages (about us, leadership,
board, history) can fill the gap. A language model (Gemini or Groq, whichever has a key) is used
only to EXTRACT from page text that EquityOS fetched itself:

1. pages are fetched politely (robots.txt obeyed, same domain as the registered website);
2. the model is asked for verbatim quotations only;
3. every quotation is checked to occur in the page text, and a leadership entry must name the
   person and the title inside its quotation - anything else is discarded;
4. what survives is stored with its URL, publisher, page title, retrieval time and the model used.

Nothing the model knows from training is used: it never sees a question about the company, only a
page. If no model is configured or a page cannot be read, nothing is stored and the report simply
has no external section. Figures are never taken from here; this is for qualitative context.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.acquisition.http import PoliteClient
from app.core.text import normalize_text
from app.models import Company, CompanyEnrichment
from app.providers.company_ir import validate_html

log = logging.getLogger("equityos.enrich")

MAX_PAGES = 4
MAX_CHARS = 12000
MAX_QUOTE = 400
MAX_PER_KIND = 8
REFRESH_DAYS = 30
_ABOUT = re.compile(
    r"about|leadership|management|board|directors|history|our[- ]story|who[- ]we[- ]are|"
    r"our[- ]journey|heritage|milestone",
    re.I,
)
_KINDS = ("history", "leadership", "business")
_SYSTEM = (
    "You extract facts from ONE web page. Use only the page text you are given; never add anything "
    "you know from elsewhere. Return JSON: "
    '{"history": [{"quote": "..."}], "leadership": [{"name": "...", "role": "...", "quote": "..."}], '
    '"business": [{"quote": "..."}]}. Every quote must be copied EXACTLY, word for word, from the '
    "page (one sentence, at most 300 characters). history = founding, milestones, acquisitions, "
    "name changes. leadership = current directors and senior managers with their titles; the quote "
    "must contain both the name and the title. business = what the company does and its segments. "
    "Use empty lists when the page has none. Never invent a quote."
)


@dataclass
class EnrichReport:
    pages_read: int = 0
    stored: int = 0
    discarded: int = 0  # extracted but not verifiable against the page
    notes: list[str] = field(default_factory=list)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def candidate_pages(html: str, home: str) -> list[str]:
    """About/leadership/history pages linked from the home page, on the company's own domain."""
    root = ".".join((urlsplit(home).hostname or "").removeprefix("www.").split(".")[-2:])
    found: dict[str, None] = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(home, href).split("#")[0].rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or root not in (parts.hostname or ""):
            continue
        if parts.path.lower().endswith((".pdf", ".xlsx", ".zip", ".jpg", ".png")):
            continue
        if _ABOUT.search(f"{a.get_text(' ')} {parts.path}"):
            found.setdefault(url, None)
    return list(found)[:MAX_PAGES]


def page_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript", "header", "form"]):
        tag.decompose()
    title = _squash(soup.title.get_text(" ")) if soup.title else ""
    return title, _squash(soup.get_text(" "))


def verify(items: list[dict], kind: str, text: str) -> tuple[list[dict], int]:
    """Keep only entries whose quotation occurs verbatim in the page (whitespace-insensitive)."""
    haystack = normalize_text(text)
    kept, dropped = [], 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        quote = _squash(str(item.get("quote") or ""))
        if not quote or len(quote) > MAX_QUOTE or normalize_text(quote) not in haystack:
            dropped += 1
            continue
        if kind == "leadership":
            name, role = _squash(str(item.get("name") or "")), _squash(str(item.get("role") or ""))
            nq = normalize_text(quote)
            if (
                not name
                or not role
                or normalize_text(name) not in nq
                or normalize_text(role) not in nq
            ):
                dropped += 1  # the person and the title must both be in the quoted words
                continue
            kept.append({"quote": quote, "name": name[:200], "role": role[:200]})
        else:
            # history / business must be a real sentence, not a menu label, title or tagline
            if not quote.endswith((".", "!", ")")) or len(quote.split()) < 7:
                dropped += 1
                continue
            kept.append({"quote": quote})
    return kept[:MAX_PER_KIND], dropped


def enrich_company(
    session: Session,
    company: Company,
    client: PoliteClient,
    router,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> EnrichReport:
    from app.llm.base import LlmError, LlmRequest

    report = EnrichReport()
    now = now or datetime.now(UTC)
    if not company.website:
        report.notes.append("no website is registered for this company")
        return report
    if router is None or not getattr(router, "providers", None):
        report.notes.append("no language model is configured; nothing was extracted")
        return report
    latest = session.scalar(
        select(func.max(CompanyEnrichment.retrieved_at)).where(
            CompanyEnrichment.company_id == company.id
        )
    )
    if latest and not force and (now - latest).days < REFRESH_DAYS:
        report.notes.append(f"read {latest:%d/%m/%Y}; not read again for {REFRESH_DAYS} days")
        return report
    home = client.fetch(company.website, source="COMPANY_IR", validate=validate_html)
    if not home.ok:
        report.notes.append(f"the home page could not be read ({home.status.value})")
        return report
    base = home.final_url or company.website
    urls = [base.rstrip("/"), *candidate_pages(home.text, base)]
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        res = (
            home
            if url == base.rstrip("/")
            else client.fetch(url, source="COMPANY_IR", validate=validate_html)
        )
        if not res.ok:
            report.notes.append(f"{url}: not read ({res.status.value})")
            continue
        title, text = page_text(res.text)
        if len(text) < 200:
            continue
        report.pages_read += 1
        try:
            answer = router.complete_json(
                LlmRequest(
                    system=_SYSTEM,
                    user=f"PAGE URL: {url}\nPAGE TEXT:\n{text[:MAX_CHARS]}",
                    max_output_tokens=1500,
                )
            )
        except LlmError as exc:
            report.notes.append(f"{url}: extraction unavailable ({type(exc).__name__})")
            break
        data = answer.data if isinstance(answer.data, dict) else {}
        session.execute(
            delete(CompanyEnrichment).where(
                CompanyEnrichment.company_id == company.id, CompanyEnrichment.url == url
            )
        )
        for kind in _KINDS:
            kept, dropped = verify(data.get(kind) or [], kind, text)
            report.discarded += dropped
            for item in kept:
                session.add(
                    CompanyEnrichment(
                        company_id=company.id,
                        kind=kind,
                        name=item.get("name"),
                        role=item.get("role"),
                        quote=item["quote"],
                        url=url,
                        publisher=(urlsplit(url).hostname or "").removeprefix("www."),
                        page_title=title[:300] or None,
                        retrieved_at=res.retrieved_at or now,
                        extractor=f"{answer.provider}/{answer.model}"[:80],
                    )
                )
                report.stored += 1
        session.commit()
    return report


def load(session: Session, company_id) -> dict:
    """What a report may show: verified, sourced statements grouped by kind (or a stated gap)."""
    rows = list(
        session.scalars(
            select(CompanyEnrichment)
            .where(CompanyEnrichment.company_id == company_id)
            .order_by(CompanyEnrichment.kind, CompanyEnrichment.id)
        )
    )
    if not rows:
        return {
            "available": False,
            "reason": "no external page has been read for this company (the model-assisted step has not run or found nothing verifiable)",
        }
    out: dict = {"available": True, "history": [], "leadership": [], "business": []}
    for r in rows:
        out[r.kind].append(
            {
                "quote": r.quote,
                "name": r.name,
                "role": r.role,
                "url": r.url,
                "publisher": r.publisher,
                "page_title": r.page_title,
                "retrieved": r.retrieved_at.date().isoformat(),
                "extractor": r.extractor,
            }
        )
    out["method"] = (
        "Quotations extracted by a language model from the company's own public web pages and "
        "confirmed to appear verbatim in the page text. They are the company's own words, not "
        "independent research; no fact in this block comes from the model's general knowledge."
    )
    return out
