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
    CompanyClassification,
    CompanyWebsiteRegistry,
    Document,
    DocumentExtraction,
    DocumentPage,
    RawPayload,
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

# A page that names the company is still not its website when it is a domain-dispute ruling, a parked
# or for-sale domain, or written in a script the checks cannot read.
_UNUSABLE_PAGE = re.compile(
    r"reverse domain name|domain name (?:dispute|hijack)|\budrp\b|"
    r"(?:this|the) domain(?: name)? (?:is|may be) for sale|buy this domain|domain (?:is )?parked",
    re.I,
)
MIN_LATIN_SHARE = 0.6
# The provider's description of the business must be recognisable on the site: genuine sites share at
# least about a quarter of its distinctive words; a different company's site shares almost none.
MIN_PROFILE_WORDS = 15
MIN_PROFILE_OVERLAP = 0.20
_PROFILE_STOP = frozenset(
    "limited company based india indian engaged business provider services service solutions "
    "products product including offers segments segment also other include which their with from "
    "that this these such into across various range".split()
)


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


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{5,}", (text or "").lower()) if w not in _PROFILE_STOP}


def profile_overlap(profile: str | None, page_text: str) -> dict | None:
    """How much of the provider's business description appears on the page (None: nothing to compare)."""
    words = content_words(profile or "")
    if len(words) < MIN_PROFILE_WORDS:
        return None
    shared = words & content_words(page_text)
    return {
        "profile_words": len(words),
        "shared": len(shared),
        "ratio": round(len(shared) / len(words), 2),
    }


def profile_text(session: Session, company_id) -> str | None:
    """The provider's business description for the company: stored on the label, else the raw profile."""
    row = session.scalar(
        select(CompanyClassification.description).where(
            CompanyClassification.company_id == company_id,
            CompanyClassification.description.is_not(None),
        )
    )
    if row:
        return row
    payload = session.scalar(
        select(RawPayload)
        .where(RawPayload.company_id == company_id, RawPayload.dataset.ilike("%profile%"))
        .order_by(RawPayload.retrieved_at.desc())
        .limit(1)
    )
    data = (payload.payload or {}).get("data") if payload else None
    text = data.get("company_profile") if isinstance(data, dict) else None
    return text if isinstance(text, str) and text else None


def judge_page(
    html: str,
    names: set[str],
    isin: str | None,
    domain: str = "",
    profile: str | None = None,
) -> tuple[str, dict]:
    """(outcome, signals) for one home page. Pure: no network.

    A name match alone is not enough: a page that is a domain dispute or parked, one the checks
    cannot read, or one that does not describe the business the data provider describes is AMBIGUOUS
    (a person decides) unless the page prints the company's ISIN.
    """
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
        visible = soup.get_text(" ")
        letters = [ch for ch in visible if ch.isalpha()]
        signals["latin_share"] = (
            round(sum(ch.isascii() for ch in letters) / len(letters), 2) if letters else None
        )
        signals["unusable_page"] = bool(_UNUSABLE_PAGE.search(f"{title} {visible[:2500]}"))
        signals["profile_overlap"] = profile_overlap(profile, visible)
        overlap = signals["profile_overlap"]
        veto = None
        if signals["unusable_page"]:
            veto = "the page is a domain dispute / parked page, not the company's site"
        elif signals["latin_share"] is not None and signals["latin_share"] < MIN_LATIN_SHARE:
            veto = "the page is mostly in a script the check cannot read"
        elif overlap and overlap["ratio"] < MIN_PROFILE_OVERLAP and not signals["isin_printed"]:
            veto = (
                "the page does not describe the business the data provider describes "
                f"({overlap['shared']} of {overlap['profile_words']} distinctive words in common)"
            )
        if veto is None:
            return VERIFIED, signals
        signals["veto"] = veto
        return AMBIGUOUS, signals
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
    profile: str | None = None,
) -> Verdict:
    row = session.get(CompanyWebsiteRegistry, company.id)
    if row is not None and registrable(url) in rejected_domains(row):
        return Verdict(
            "REJECTED", url, reason="reviewed earlier and rejected as not this company's site"
        )
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
    outcome, signals = judge_page(home.text, names, isin, registrable(final), profile)
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
        AMBIGUOUS: signals.get("veto")
        or "the name appears in the page text only; not enough to call this the official site",
        "REJECTED": "the page does not mention this company",
    }[outcome]
    return Verdict(outcome, url, final, signals, reason, home.text)


_IR_PATH = re.compile(r"/(investor-relations|investors?|invest-relations|shareholders?)(/|$)", re.I)


def _ir_rank(link) -> tuple[int, int] | None:
    """How good an investor-relations page this link looks like (lower is better); None = reject.

    A blog post or news item that mentions investors is not the investor page: the path must not be
    editorial, and a short path that names investor relations beats a long one that merely mentions it.
    """
    from app.acquisition.annual_reports import is_noise

    if link.is_document or is_noise(link.url):
        return None
    path = urlsplit(link.url).path
    depth = len([p for p in path.split("/") if p])
    if depth > 4:
        return None
    return (0 if _IR_PATH.search(path) else 1, depth)


def find_ir_url(html: str, home_url: str, client: PoliteClient) -> str | None:
    """The investor-relations page linked from the home page, if it loads."""
    ranked = sorted(
        (
            (rank, link)
            for link in extract_ir_links(html, home_url)
            if link.kind == "investor_relations" and (rank := _ir_rank(link)) is not None
        ),
        key=lambda pair: (pair[0], pair[1].url),
    )
    for _, link in ranked[:3]:
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
    profile = profile_text(session, company.id)

    def try_candidates(urls: list[str], source: str) -> Verdict | None:
        seen_domains: set[str] = set()
        best_ambiguous: Verdict | None = None
        for url in urls[:MAX_CANDIDATES]:
            dom = registrable(url)
            if dom in seen_domains:
                continue
            seen_domains.add(dom)
            v = check_candidate(session, company, url, client, names, isin, profile)
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
    row.verification = _carry(row, {"tried": result.tried[-12:]})
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
        row.verification = _carry(
            row,
            {"tried": result.tried[-12:], "signals": verdict.signals, "reason": verdict.reason},
        )
        result.status, result.website, result.investor_relations_url = VERIFIED, stored, ir
    elif verdict and verdict.outcome == AMBIGUOUS:
        row.status, row.next_retry_at = (
            AMBIGUOUS,
            None,
        )  # a person decides; not retried automatically
        row.website = (verdict.final_url or verdict.url).rstrip("/")
        row.discovery_source = result.source
        row.verification = _carry(
            row,
            {"tried": result.tried[-12:], "signals": verdict.signals, "reason": verdict.reason},
        )
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


_KEEP = ("rejected", "review_history")


def _carry(row: CompanyWebsiteRegistry, new: dict) -> dict:
    """New verification evidence, keeping what a reviewer recorded (rejected domains, history)."""
    old = row.verification or {}
    return {**{k: old[k] for k in _KEEP if k in old}, **new}


def rejected_domains(row: CompanyWebsiteRegistry) -> set[str]:
    return {r.get("domain", "") for r in (row.verification or {}).get("rejected", [])}


def mark_unverified(
    session: Session,
    company: Company,
    *,
    reason: str,
    evidence: dict | None = None,
    status: str = AMBIGUOUS,
    actor: str = "review",
    now: datetime | None = None,
) -> CompanyWebsiteRegistry:
    """Withdraw a website that failed review. Nothing is deleted or guessed.

    The company stops using the address (its site is cleared so documents are not fetched from it),
    the registry row keeps it as an AMBIGUOUS candidate for a person, the earlier verification is
    preserved in `review_history`, and the domain is remembered so discovery cannot re-accept it.
    A MANUAL website (set by a person) is never touched.
    """
    now = now or datetime.now(UTC)
    row = registry_row(session, company)
    if row.status == MANUAL:
        raise ValueError("a website set by hand is not changed by an automated review")
    site = row.website or company.website
    old = dict(row.verification or {})
    history = list(old.get("review_history", []))
    history.append(
        {
            "at": now.isoformat(),
            "actor": actor,
            "previous_status": row.status,
            "previous_website": site,
            "reason": reason,
            "evidence": evidence or {},
            "previous_verification": {k: v for k, v in old.items() if k not in _KEEP},
        }
    )
    rejected = list(old.get("rejected", []))
    if site and registrable(site) not in {r.get("domain") for r in rejected}:
        rejected.append({"domain": registrable(site), "reason": reason, "at": now.isoformat()})
    row.verification = {
        **{k: v for k, v in old.items() if k == "tried"},
        "rejected": rejected,
        "review_history": history,
        "reason": reason,
    }
    row.status = status
    row.next_retry_at = None
    row.verified_at = None
    if company.website and (not site or registrable(company.website) == registrable(site)):
        company.website = None
        company.investor_relations_url = None
        row.investor_relations_url = None
    session.commit()
    return row


def review_registered(
    session: Session, company: Company, client: PoliteClient, *, now: datetime | None = None
) -> dict:
    """Re-judge an automatically VERIFIED website with the current checks.

    Returns {"outcome": CONFIRMED | WITHDRAWN | SKIPPED, "reason": ...}. A site that cannot be
    fetched is left as it is (SKIPPED): being unreachable today is not evidence against it.
    """
    now = now or datetime.now(UTC)
    row = session.get(CompanyWebsiteRegistry, company.id)
    if row is None or row.status != VERIFIED or not company.website:
        return {"outcome": "SKIPPED", "reason": "not an automatically verified website"}
    home = client.fetch(company.website, source="COMPANY_IR", validate=validate_html)
    if not home.ok:
        return {"outcome": "SKIPPED", "reason": f"not retrieved ({home.status.value})"}
    names = company_names(session, company.id)
    isin = session.scalar(
        select(Security.isin).where(Security.company_id == company.id, Security.isin.is_not(None))
    )
    outcome, signals = judge_page(
        home.text,
        names,
        isin,
        registrable(home.final_url or company.website),
        profile_text(session, company.id),
    )
    if outcome == VERIFIED:
        v = dict(row.verification or {})
        v["review"] = {"at": now.isoformat(), "outcome": "CONFIRMED", "signals": signals}
        row.verification = v
        session.commit()
        return {"outcome": "CONFIRMED", "reason": "passes the current checks"}
    reason = signals.get("veto") or "the page no longer passes verification"
    mark_unverified(session, company, reason=reason, evidence=signals, now=now)
    return {"outcome": "WITHDRAWN", "reason": reason}


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
