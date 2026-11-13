"""Find, verify and register a listed company's official website - without guessing.

Candidates come from three permitted places, cheapest first:

1. ``document``  web addresses printed in the company's own stored filings (annual report cover and
                 back pages), ranked by how often and how prominently they appear;
2. ``name``      addresses built from the legal name (``example.com`` / ``.in`` / ``.co.in``);
3. ``llm``       a Gemini/Groq suggestion. The model only PROPOSES; nothing it says is trusted until
                 the page itself passes the same check as every other candidate.

A candidate is fetched politely (robots.txt obeyed, rate-limited, public host only) and judged on the
page: the company name must be in the page title / site name / copyright line (or the ISIN must be
printed) AND in the visible text. A name in the body alone is AMBIGUOUS and is flagged for a person.
A domain already registered to a different issuer is AMBIGUOUS too. Aggregators, social networks and
exchanges are never accepted as "the company's website".

Registration goes through ``set_website`` (additive: a stored website is never changed) and every
decision is kept in ``company_website_registry`` so it can be audited, retried or corrected by hand.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.acquisition.evidence_fetch import WebsiteRejected, _public_host, set_website
from app.acquisition.http import PoliteClient
from app.core.text import normalize_text
from app.docintel.entity import company_names
from app.models import (
    Company,
    CompanyWebsiteRegistry,
    Document,
    DocumentExtraction,
    DocumentPage,
    Security,
)
from app.providers.company_ir import extract_ir_links, validate_html

log = logging.getLogger("equityos.website_discovery")

VERIFIED, AMBIGUOUS, NO_CANDIDATE, FAILED, MANUAL = (
    "VERIFIED",
    "AMBIGUOUS",
    "NO_CANDIDATE",
    "FAILED",
    "MANUAL",
)
SOURCE_DOCUMENT, SOURCE_NAME, SOURCE_LLM, SOURCE_MANUAL, SOURCE_EXISTING = (
    "document",
    "name",
    "llm",
    "manual",
    "existing",
)
MAX_CANDIDATES = 8
RETRY_BASE_DAYS = 7
RETRY_MAX_DAYS = 90

# Never "the company's own website": social networks, exchanges, regulators, data/news aggregators,
# code hosts, document hosts and URL shorteners.
NOT_A_COMPANY_SITE = frozenset(
    "facebook.com twitter.com x.com linkedin.com instagram.com youtube.com wikipedia.org wikidata.org "
    "nseindia.com bseindia.com sebi.gov.in rbi.org.in mca.gov.in moneycontrol.com screener.in "
    "tickertape.in trendlyne.com economictimes.com livemint.com business-standard.com "
    "google.com bing.com yahoo.com duckduckgo.com github.com scribd.com slideshare.net "
    "zaubacorp.com tofler.in indiamart.com justdial.com crunchbase.com bloomberg.com reuters.com "
    "upstox.com zerodha.com groww.in cdsl.com nsdl.co.in bit.ly t.co goo.gl".split()
)
_GENERIC_WORDS = frozenset(
    "limited ltd the of and india indian private pvt company co corporation corp industries "
    "official website home page welcome investor investors relations relation".split()
)
_TLDS = (".com", ".in", ".co.in")
_ADDRESS = re.compile(
    r"\b(?:https?://)?(?:www\.)([a-z0-9][a-z0-9-]{1,60}(?:\.[a-z0-9-]{2,24}){1,3})\b", re.I
)
_TITLE_SPLIT = re.compile(r"\s[|\-–—:·»]\s|\s[|\-–—:·»]|[|\-–—:·»]\s")


@dataclass
class Verdict:
    outcome: str  # VERIFIED | AMBIGUOUS | REJECTED
    url: str
    final_url: str | None = None
    signals: dict = field(default_factory=dict)
    reason: str = ""
    html: str = ""


@dataclass
class DiscoveryResult:
    company: str
    status: str
    website: str | None = None
    investor_relations_url: str | None = None
    source: str | None = None
    tried: list[dict] = field(default_factory=list)
    note: str = ""


# ---- small helpers -------------------------------------------------------------------------------


def registrable(url_or_host: str) -> str:
    host = (urlsplit(url_or_host).hostname or url_or_host).lower().removeprefix("www.")
    parts = host.split(".")
    if (
        len(parts) >= 3
        and parts[-2] in {"co", "com", "org", "net", "gov", "ac"}
        and len(parts[-1]) == 2
    ):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _tokens(name: str) -> list[str]:
    return [t for t in normalize_text(name).split() if t not in _GENERIC_WORDS]


_CORP_WORDS = frozenset("limited ltd private pvt the of and".split())


def name_guesses(names: set[str]) -> list[str]:
    """Home-page addresses a company of this name would plausibly use. Only guesses: the page decides."""
    out: list[str] = []
    for name in sorted(names, key=len):
        core = _tokens(name)  # without industries / india / company ...
        full = [t for t in normalize_text(name).split() if t not in _CORP_WORDS]
        if not core:
            continue
        stems = {"".join(core), "".join(full)}
        if len(core) == 1 or len(full) >= 2:
            stems.add(core[0])
        if len(full) >= 3:
            stems.add("".join(t[0] for t in full))
        if len(normalize_text(name).split()) >= 3:  # "reliance industries limited" -> ril
            stems.add("".join(t[0] for t in normalize_text(name).split()))
        for stem in sorted(stems, key=len, reverse=True):
            if len(stem) < 3:
                continue
            for tld in _TLDS:
                url = f"https://www.{stem}{tld}"
                if url not in out:
                    out.append(url)
    return out[: MAX_CANDIDATES * 2]


def document_candidates(session: Session, company_id) -> list[str]:
    """Web addresses printed in the company's own stored filings, most frequent first."""
    pages = session.scalars(
        select(DocumentPage.text)
        .join(DocumentExtraction, DocumentExtraction.id == DocumentPage.extraction_id)
        .join(Document, Document.id == DocumentExtraction.document_id)
        .where(Document.company_id == company_id, DocumentPage.page_number <= 6)
        .limit(24)
    ).all()
    names = {registrable(n.replace(" ", "")) for n in company_names(session, company_id)}
    tokens = {t for n in company_names(session, company_id) for t in _tokens(n) if len(t) >= 3}
    counts: dict[str, int] = {}
    for text in pages:
        for m in _ADDRESS.finditer(text or ""):
            dom = registrable(m.group(1).lower())
            if dom in NOT_A_COMPANY_SITE:
                continue
            counts[dom] = counts.get(dom, 0) + 1
    ranked = sorted(
        counts,
        key=lambda d: (-(any(t in d for t in tokens) or d in names), -counts[d]),
    )
    return [f"https://www.{d}" for d in ranked[:3] if any(t in d for t in tokens) or counts[d] >= 3]


def llm_candidates(router, company: Company, symbols: list[str], isin: str | None) -> list[str]:
    """Ask the model for the official site. It proposes; verification disposes."""
    from app.llm.base import LlmError, LlmRequest

    request = LlmRequest(
        system=(
            "You identify the official corporate website of listed Indian companies. Answer with JSON "
            '{"websites": ["https://..."]}. Give only an address you are confident is the company\'s '
            "own domain; give an empty list if unsure. Never give a news, data, exchange or social site."
        ),
        user=f"Company: {company.legal_name}\nNSE/BSE symbols: {', '.join(symbols) or 'n/a'}\nISIN: {isin or 'n/a'}",
        max_output_tokens=300,
    )
    try:
        data = router.complete_json(request).data
    except LlmError as exc:
        log.info("LLM website suggestion unavailable for %s: %s", company.common_name, exc)
        return []
    sites = data.get("websites") if isinstance(data, dict) else None
    if not isinstance(sites, list):
        return []
    return [s for s in sites if isinstance(s, str) and s.startswith("http")][:3]


# ---- verification --------------------------------------------------------------------------------


def _head_segments(title: str) -> list[str]:
    return [normalize_text(s) for s in _TITLE_SPLIT.split(title or "") if normalize_text(s)]


def distinctive_names(names: set[str]) -> set[str]:
    """Names specific enough to identify a company on a web page.

    A name whose meaningful part is ONE word ("titan", "sun", "dabur") is shared with many
    unrelated businesses, so on its own it proves nothing; only its full form ("titan company
    limited") is kept. A name with two or more meaningful words ("asian paints") is kept as it is.
    """
    keep = set()
    for name in names:
        words = name.split()
        core = [t for t in words if t not in _GENERIC_WORDS]
        if len(core) >= 2 or (len(core) == 1 and len(words) > 1 and len(core[0]) >= 3):
            keep.add(name)
    return {n for n in keep if len(n) >= 4}


def _name_in_head(names: set[str], segments: list[str]) -> str | None:
    """A segment of the title / site name that IS the company name (allowing a few filler words)."""
    for name in names:
        core = [t for t in name.split() if t not in _GENERIC_WORDS]
        single = len(core) < 2
        for seg in segments:
            if single:  # one meaningful word: the full legal form must be written out
                if f" {name} " in f" {seg} ":
                    return name
                continue
            toks = [t for t in seg.split() if t not in _GENERIC_WORDS]
            if toks == core or (toks[: len(core)] == core and len(toks) <= len(core) + 1):
                return name
    return None


def judge_page(html: str, names: set[str], isin: str | None, domain: str = "") -> tuple[str, dict]:
    """(outcome, signals) for one home page. Pure: no network."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ") if soup.title else ""
    site_name = ""
    for meta in soup.find_all("meta"):
        if (meta.get("property") or meta.get("name") or "").lower() in (
            "og:site_name",
            "application-name",
        ):
            site_name = meta.get("content") or ""
    text = normalize_text(soup.get_text(" "))
    padded = f" {text} "
    usable = distinctive_names(names)
    signals: dict = {
        "title": title.strip()[:120],
        "name_in_title": _name_in_head(usable, _head_segments(title) + _head_segments(site_name)),
        "name_in_body": next(
            (n for n in sorted(usable, key=len, reverse=True) if f" {n} " in padded), None
        ),
        "copyright_name": next(
            (
                n
                for n in usable
                if re.search(
                    rf"(?:©|copyright|\(c\))[^.]{{0,40}}{re.escape(n)}",
                    html.lower().replace("&amp;", "and"),
                )
            ),
            None,
        ),
        "isin_printed": bool(isin and isin.lower() in html.lower()),
    }
    stem = domain.split(".")[0] if domain else ""
    stems = set()
    for n in usable:
        core = [t for t in n.split() if t not in _GENERIC_WORDS]
        if core:
            stems |= {"".join(core), core[0] if len(core) == 1 else "".join(core)}
    # the domain is the company's name (dabur.com, asianpaints.com) AND its full legal name is in the text
    signals["domain_matches_name"] = bool(stem) and stem in stems
    strong = (
        signals["domain_matches_name"]
        or signals["name_in_title"]
        or signals["copyright_name"]
        or signals["isin_printed"]
    )
    if strong and (signals["name_in_body"] or signals["isin_printed"]):
        return VERIFIED, signals
    if signals["name_in_body"]:
        return AMBIGUOUS, signals
    return "REJECTED", signals


def check_candidate(
    session: Session,
    company: Company,
    url: str,
    client: PoliteClient,
    names: set[str],
    isin: str | None,
) -> Verdict:
    try:
        _public_host(url)
    except WebsiteRejected as exc:
        return Verdict("REJECTED", url, reason=str(exc))
    if registrable(url) in NOT_A_COMPANY_SITE:
        return Verdict(
            "REJECTED",
            url,
            reason="an aggregator, exchange or social site is not the company's own site",
        )
    home = client.fetch(url, source="COMPANY_IR", validate=validate_html)
    if not home.ok:
        return Verdict("REJECTED", url, reason=f"not retrieved ({home.status.value})")
    final = home.final_url or url
    if registrable(final) in NOT_A_COMPANY_SITE:
        return Verdict(
            "REJECTED", url, final, reason="redirects to a site that is not a company site"
        )
    outcome, signals = judge_page(home.text, names, isin, registrable(final))
    signals["redirected_to"] = final if registrable(final) != registrable(url) else None
    if outcome == VERIFIED:
        owner = session.scalar(
            select(CompanyWebsiteRegistry)
            .join(Company, Company.id == CompanyWebsiteRegistry.company_id)
            .where(
                CompanyWebsiteRegistry.status.in_((VERIFIED, MANUAL)),
                CompanyWebsiteRegistry.company_id != company.id,
                CompanyWebsiteRegistry.website.ilike(f"%{registrable(final)}%"),
            )
        )
        if owner is not None:
            return Verdict(
                AMBIGUOUS,
                url,
                final,
                signals,
                f"the domain {registrable(final)} is already registered to another company",
            )
    reason = {
        VERIFIED: "company name found in the page title, site name or copyright line, and in the page text",
        AMBIGUOUS: "the name appears in the page text only; not enough to call this the official site",
        "REJECTED": "the page does not mention this company",
    }[outcome]
    return Verdict(outcome, url, final, signals, reason, home.text)


def find_ir_url(html: str, home_url: str, client: PoliteClient) -> str | None:
    """The investor-relations page linked from the home page, if it loads."""
    links = [
        link
        for link in extract_ir_links(html, home_url)
        if link.kind == "investor_relations" and not link.is_document
    ]
    for link in links[:3]:
        res = client.fetch(link.url, source="COMPANY_IR", validate=validate_html)
        if res.ok:
            return (res.final_url or link.url).rstrip("/")
    return None


# ---- the workflow --------------------------------------------------------------------------------


def _backoff(attempts: int) -> datetime:
    days = min(RETRY_BASE_DAYS * 2 ** max(attempts - 1, 0), RETRY_MAX_DAYS)
    return datetime.now(UTC) + timedelta(days=days)


def registry_row(session: Session, company: Company) -> CompanyWebsiteRegistry:
    row = session.get(CompanyWebsiteRegistry, company.id)
    if row is None:
        row = CompanyWebsiteRegistry(company_id=company.id, status=NO_CANDIDATE, attempts=0)
        session.add(row)
    return row


def record_existing(session: Session, company: Company) -> bool:
    """A website already stored on the company is kept as is and registered as such (no refetch)."""
    if not company.website:
        return False
    row = registry_row(session, company)
    if row.status in (VERIFIED, MANUAL):
        return False
    row.status, row.website, row.discovery_source = MANUAL, company.website, SOURCE_EXISTING
    row.investor_relations_url = company.investor_relations_url
    row.verification = {"note": "stored before the registry existed; not re-verified, not changed"}
    row.verified_at = datetime.now(UTC)
    session.commit()
    return True


def discover(
    session: Session,
    company: Company,
    client: PoliteClient,
    router=None,
    *,
    use_llm: bool = True,
    force: bool = False,
    now: datetime | None = None,
) -> DiscoveryResult:
    """One company: discover, verify, register. Idempotent; a verified website is never replaced."""
    now = now or datetime.now(UTC)
    row = registry_row(session, company)
    if company.website and row.status not in (VERIFIED, MANUAL):
        record_existing(session, company)
        row = registry_row(session, company)
    if row.status in (VERIFIED, MANUAL) and company.website:
        # Already registered: only fill in a missing investor-relations URL.
        res = DiscoveryResult(
            company.common_name,
            row.status,
            company.website,
            company.investor_relations_url,
            row.discovery_source,
            note="already registered; unchanged",
        )
        if not company.investor_relations_url:
            home = client.fetch(company.website, source="COMPANY_IR", validate=validate_html)
            if home.ok:
                ir = find_ir_url(home.text, home.final_url or company.website, client)
                if ir:
                    company.investor_relations_url = row.investor_relations_url = ir
                    res.investor_relations_url, res.note = ir, "investor-relations page added"
                    session.commit()
        return res
    if not force and row.next_retry_at and row.next_retry_at > now:
        return DiscoveryResult(
            company.common_name,
            row.status,
            note=f"retry not due until {row.next_retry_at:%d/%m/%Y}",
        )

    names = company_names(session, company.id)
    securities = session.scalars(select(Security).where(Security.company_id == company.id)).all()
    isin = next((s.isin for s in securities if s.isin), None)
    symbols = sorted({s.symbol for s in securities if s.symbol})
    result = DiscoveryResult(company.common_name, NO_CANDIDATE)

    def try_candidates(urls: list[str], source: str) -> Verdict | None:
        seen_domains: set[str] = set()
        best_ambiguous: Verdict | None = None
        for url in urls[:MAX_CANDIDATES]:
            dom = registrable(url)
            if dom in seen_domains:
                continue
            seen_domains.add(dom)
            v = check_candidate(session, company, url, client, names, isin)
            result.tried.append(
                {
                    "url": url,
                    "source": source,
                    "outcome": v.outcome,
                    "reason": v.reason,
                    "signals": v.signals,
                }
            )
            if v.outcome == VERIFIED:
                result.source = source
                return v
            if v.outcome == AMBIGUOUS and best_ambiguous is None:
                best_ambiguous = v
                result.source = source
        return best_ambiguous

    verdict = try_candidates(document_candidates(session, company.id), SOURCE_DOCUMENT)
    if not (verdict and verdict.outcome == VERIFIED):
        v2 = try_candidates(name_guesses(names), SOURCE_NAME)
        verdict = v2 if v2 and (v2.outcome == VERIFIED or verdict is None) else verdict
    if not (verdict and verdict.outcome == VERIFIED) and use_llm and router is not None:
        v3 = try_candidates(llm_candidates(router, company, symbols, isin), SOURCE_LLM)
        verdict = v3 if v3 and (v3.outcome == VERIFIED or verdict is None) else verdict

    row.attempts = (row.attempts or 0) + 1
    row.last_attempt_at = now
    row.verification = {"tried": result.tried[-12:]}
    if verdict and verdict.outcome == VERIFIED:
        site = (verdict.final_url or verdict.url).rstrip("/")
        parts = urlsplit(site)
        site = f"{parts.scheme}://{parts.netloc}"  # register the home page, not a deep link
        try:
            stored = set_website(session, company, site, client, confirm_issuer=True)
        except WebsiteRejected as exc:
            row.status, result.status, result.note = FAILED, FAILED, f"registration refused: {exc}"
            row.next_retry_at = _backoff(row.attempts)
            session.commit()
            return result
        ir = find_ir_url(verdict.html, site, client)
        if ir:
            company.investor_relations_url = ir
        row.status, row.website, row.investor_relations_url = VERIFIED, stored, ir
        row.discovery_source, row.verified_at, row.next_retry_at = result.source, now, None
        row.verification = {
            "tried": result.tried[-12:],
            "signals": verdict.signals,
            "reason": verdict.reason,
        }
        result.status, result.website, result.investor_relations_url = VERIFIED, stored, ir
    elif verdict and verdict.outcome == AMBIGUOUS:
        row.status, row.next_retry_at = (
            AMBIGUOUS,
            None,
        )  # a person decides; not retried automatically
        row.website = (verdict.final_url or verdict.url).rstrip("/")
        row.discovery_source = result.source
        result.status, result.note = AMBIGUOUS, f"needs a person: {verdict.reason}"
    else:
        row.status = NO_CANDIDATE if not result.tried else FAILED
        row.next_retry_at = _backoff(row.attempts)
        result.status = row.status
        blocked = [t for t in result.tried if "BLOCKED_BY_ROBOTS" in t["reason"]]
        gone = [t for t in result.tried if "NETWORK_ERROR" in t["reason"]]
        parts = []
        if blocked:
            parts.append(
                f"{len(blocked)} of {len(result.tried)} refuse automated access (robots.txt)"
            )
        if gone:
            parts.append(f"{len(gone)} of {len(result.tried)} unreachable")
        result.note = (
            "no candidate address found"
            if not result.tried
            else "; ".join([*parts, "none passed verification"])
        )
    session.commit()
    return result


def registry_summary(session: Session) -> dict:
    from sqlalchemy import func

    total = session.scalar(select(func.count()).select_from(Company)) or 0
    by = dict(
        session.execute(
            select(CompanyWebsiteRegistry.status, func.count()).group_by(
                CompanyWebsiteRegistry.status
            )
        ).all()
    )
    with_site = (
        session.scalar(
            select(func.count()).select_from(Company).where(Company.website.is_not(None))
        )
        or 0
    )
    with_ir = (
        session.scalar(
            select(func.count())
            .select_from(Company)
            .where(Company.investor_relations_url.is_not(None))
        )
        or 0
    )
    return {"companies": total, "with_website": with_site, "with_ir_url": with_ir, "by_status": by}


def ensure_website(
    session: Session, company: Company, client: PoliteClient
) -> DiscoveryResult | None:
    """Pipeline hook: a company with no website gets one discovered (once per back-off window)
    before its public documents are fetched. A company that has one is not touched or fetched."""
    if company.website:
        return None
    router = None
    try:
        from app.llm.providers import default_router

        router = default_router()
        if not router.providers:
            router = None
    except Exception:  # noqa: BLE001 - discovery works without an LLM
        router = None
    return discover(session, company, client, router, use_llm=router is not None)
