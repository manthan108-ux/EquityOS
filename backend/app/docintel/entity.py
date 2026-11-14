"""Is this statement really the company's own?

Company websites also host documents of their subsidiaries and group companies (a bank's site
publishes its asset-management subsidiary's results). Numbers from such a document must never be
attributed to the parent. A statement is confirmed as the company's when:

  1. its page header (the first lines of the page) names the company, or
  2. the page header names no company at all, and the document's first pages name the company.

If the page header names some other "... Limited" and not the company, it is a MISMATCH. All
name matching is on normalised whole words (`normalize_text`), using the legal name, the common
name and recorded aliases.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.text import normalize_text
from app.models import Company, CompanyAlias

HEADER_LINES = 15
COVER_PAGES = 3
_SUFFIX = re.compile(r"\s+(limited|ltd|private|pvt|plc|company)$")


@dataclass(frozen=True)
class EntityCheck:
    confirmed: bool
    how: str  # "page header" | "document cover" | reason it failed


def company_names(session: Session, company_id: uuid.UUID) -> set[str]:
    """Normalised names to look for: legal name, common name and aliases, with and without
    'limited'."""
    company = session.get(Company, company_id)
    if company is None:
        return set()
    raw = [company.legal_name, company.common_name]
    raw += list(
        session.scalars(select(CompanyAlias.alias).where(CompanyAlias.company_id == company_id))
    )
    names: set[str] = set()
    for name in raw:
        norm = normalize_text(name or "")
        if not norm:
            continue
        names.add(norm)
        stripped = _SUFFIX.sub("", norm).strip()
        while stripped != norm:
            norm, stripped = stripped, _SUFFIX.sub("", stripped).strip()
        if len(stripped) >= 3:
            names.add(stripped)
    return names


_SUFFIXES = frozenset({"limited", "ltd", "inc", "llc", "gmbh", "plc", "pte", "bv", "ag"})
# words that end the entity's own name before a statement title or a connective starts
_NOT_PART_OF_A_NAME = _SUFFIXES | {
    "and", "its", "the", "for", "of", "as", "at", "on", "in", "group", "subsidiaries",
    "consolidated", "standalone", "statement", "statements", "balance", "sheet", "profit", "loss",
    "financial", "results", "annual", "report", "notes", "cash", "flow", "flows", "equity",
    "changes",
}  # fmt: skip


def names_longer_entity(text: str, names: set[str]) -> bool:
    """True when the company's name appears only as the start of a LONGER entity's name.

    "TECH MAHINDRA CERIUM PRIVATE LIMITED" contains "tech mahindra" but is a different company: a
    subsidiary whose accounts are printed in the parent's annual report. A name followed directly by
    "Limited" is the company itself; a name followed by other words and then a legal suffix is
    another entity, unless those words are part of one of the company's own names.
    """
    tokens = normalize_text(text).split()
    for name in sorted(names, key=len, reverse=True):
        needle = name.split()
        for i in range(len(tokens) - len(needle) + 1):
            if tokens[i : i + len(needle)] != needle:
                continue
            j, extra = i + len(needle), []
            while j < len(tokens) and len(extra) < 5 and tokens[j] not in _NOT_PART_OF_A_NAME:
                extra.append(tokens[j])
                j += 1
            if extra and j < len(tokens) and tokens[j] in _SUFFIXES:
                full = " ".join([*needle, *extra])
                if full not in names and full.removesuffix(" private") not in names:
                    return True
    return False


def _mentions(text: str, names: set[str]) -> bool:
    padded = f" {normalize_text(text)} "
    return any(f" {name} " in padded for name in names)


# A results filing opens with a covering letter addressed to the exchanges. They are the recipients,
# not the issuer, so naming them says nothing about whose document it is.
_EXCHANGES = re.compile(
    r"\b(national stock exchange of india|nse|bse|metropolitan stock exchange of india|"
    r"calcutta stock exchange|bombay stock exchange) (limited|ltd)\b"
)


def _names_an_entity(text: str) -> bool:
    # "<Name> Limited" names an entity; "subject to limited review" does not.
    cleaned = _EXCHANGES.sub(" ", normalize_text(text))
    return bool(re.search(r"\b[a-z]+ (limited|ltd)\b(?! review)", cleaned))


PREFACE_PAGES = 30  # how far to look when the cover is an image
PREFACE_MENTIONS = 5  # the company's name must appear this many times there


def cover_text(pages, names: set[str]) -> str:
    """The text that stands for the document's cover.

    Normally the first COVER_PAGES pages. Many annual reports open with an image cover and a
    few image-only pages, so that window holds no text at all and the company could never be
    confirmed. ONLY in that case the window widens to the first PREFACE_PAGES pages, and the text
    counts as a cover only if it names the company at least PREFACE_MENTIONS times; a document that
    merely mentions the company in passing (a subsidiary's deck quoting its parent) does not pass.
    """
    cover = "\n".join(p.text for p in pages[:COVER_PAGES])
    if cover.strip() or not names:
        return cover
    preface = "\n".join(p.text for p in pages[:PREFACE_PAGES])
    padded = f" {normalize_text(preface)} "
    mentions = sum(padded.count(f" {name} ") for name in names)
    return preface if mentions >= PREFACE_MENTIONS else cover


MIN_DOMINANT_PAGES = 80  # only long reports are judged by dominance
DOMINANT_MENTIONS = 15
DOMINANCE_FACTOR = 3
_ENTITY_PHRASE = re.compile(r"([a-z0-9&]+ [a-z0-9&]+) (?:limited|ltd)\b")


def document_dominance(pages, names: set[str]) -> str | None:
    """A description if one company clearly IS the subject of a long document, else None.

    For annual reports whose cover is artwork and whose statement pages carry no company name. The
    company must be named at least DOMINANT_MENTIONS times across the whole document AND at least
    DOMINANCE_FACTOR times as often as the most-named other "<name> Limited" entity. A subsidiary's
    document that quotes its parent a few times can never pass: the subsidiary is named more.
    """
    if not names or len(pages) < MIN_DOMINANT_PAGES:
        return None
    text = f" {normalize_text(chr(10).join(p.text for p in pages))} "
    # one mention counted once: "Tech Mahindra Limited" must not also count as "Tech Mahindra"
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    own = len(re.findall(rf"(?<= )(?:{alternatives})(?= )", text))
    if own < DOMINANT_MENTIONS:
        return None
    others: dict[str, int] = {}
    for match in _ENTITY_PHRASE.finditer(text):
        phrase = match.group(1).strip()  # the two words before "limited"
        if any(name == phrase or name.endswith(f" {phrase}") for name in names):
            continue
        others[phrase] = others.get(phrase, 0) + 1
    rival = max(others.values(), default=0)
    if own >= DOMINANCE_FACTOR * rival:
        return f"named {own} times in the document, the next entity {rival} times"
    return None


def check_entity(
    page_text: str,
    cover_text: str,
    names: set[str],
    first_page_text: str | None = None,
    dominance: str | None = None,
) -> EntityCheck:
    """Decide whether a statement page belongs to the company (see module docstring).

    Order: the statement page's own header; then the document's issuer line (the first page's
    header) - if it names a different entity the whole document is someone else's, even when the
    company is mentioned later (a subsidiary's deck often names its parent in a disclaimer);
    only then any mention on the first pages.
    """
    if not names:
        return EntityCheck(False, "company names unknown")
    header = "\n".join(page_text.splitlines()[:HEADER_LINES])
    if names_longer_entity(header, names):
        return EntityCheck(
            False, "the page header names a longer entity (a subsidiary or group company)"
        )
    if _mentions(header, names):
        return EntityCheck(True, "page header")
    if _names_an_entity(header):
        return EntityCheck(False, "the page header names another entity, not this company")
    if first_page_text is not None:
        issuer = "\n".join(first_page_text.splitlines()[:HEADER_LINES])
        if names_longer_entity(issuer, names):
            return EntityCheck(False, "the document is issued by a longer-named group entity")
        if _mentions(issuer, names):
            return EntityCheck(True, "document cover")
        if _names_an_entity(issuer):
            return EntityCheck(False, "the document is issued by another entity")
    if _mentions(cover_text, names):
        return EntityCheck(True, "document cover")
    if dominance:
        return EntityCheck(True, f"document subject ({dominance})")
    return EntityCheck(False, "neither the page nor the document cover names this company")
