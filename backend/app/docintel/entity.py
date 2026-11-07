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


def _mentions(text: str, names: set[str]) -> bool:
    padded = f" {normalize_text(text)} "
    return any(f" {name} " in padded for name in names)


def _names_an_entity(text: str) -> bool:
    # "<Name> Limited" names an entity; "subject to limited review" does not.
    return bool(re.search(r"\b[a-z]+ (limited|ltd)\b(?! review)", normalize_text(text)))


def check_entity(
    page_text: str, cover_text: str, names: set[str], first_page_text: str | None = None
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
    if _mentions(header, names):
        return EntityCheck(True, "page header")
    if _names_an_entity(header):
        return EntityCheck(False, "the page header names another entity, not this company")
    if first_page_text is not None:
        issuer = "\n".join(first_page_text.splitlines()[:HEADER_LINES])
        if _mentions(issuer, names):
            return EntityCheck(True, "document cover")
        if _names_an_entity(issuer):
            return EntityCheck(False, "the document is issued by another entity")
    if _mentions(cover_text, names):
        return EntityCheck(True, "document cover")
    return EntityCheck(False, "neither the page nor the document cover names this company")
