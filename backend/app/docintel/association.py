"""Who is a document really from? Verify, record and (with approval) correct company links.

A company's website hosts documents of other entities: a bank's site publishes its insurance and
asset-management subsidiaries' results. If those are linked to the bank, the bank's coverage is
inflated and (worse) the subsidiary's numbers could be read as the bank's.

The issuer is read from the document's own first page (its header), never from the URL:

* the header names THIS company                         -> CONFIRMED
* the header names ANOTHER company we know (the master) -> MISMATCH, relation SEPARATELY_LISTED
* the header names an unlisted "<Name> Limited" that shares the company's brand word
                                                         -> MISMATCH, relation POSSIBLE_GROUP_ENTITY
* the header names some unrelated "<Name> Limited"      -> MISMATCH, relation OTHER_ENTITY
* no entity can be read (scan, image, odd cover)        -> UNVERIFIED (not guessed)

Parent / subsidiary / associate status is NOT in our data. It is therefore never asserted: where
the document itself states a relationship ("... is a subsidiary of ..."), those sentences are
quoted as evidence, and the relation stays a name-based classification.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.text import normalize_text
from app.models import (
    Company,
    Document,
    DocumentExtraction,
    DocumentLinkEvent,
    ExtractedFact,
    FinancialFact,
    GroupEntity,
)

HEADER_LINES = 25
RELATION_PAGES = 6
MAX_NGRAM = 10
MIN_NAME_WORDS = 2
_SUFFIX = re.compile(r"\s+(limited|ltd|private limited|pvt ltd|plc)$")
_ENTITY = re.compile(r"\b((?:[a-z0-9&]+ ){1,7}(?:limited|ltd))\b(?! review)")
_RELATION_WORDS = re.compile(
    r"\b(subsidiary|subsidiaries|joint venture|associate|promoter|holding company|parent|"
    r"group company|wholly[- ]owned)\b",
    re.IGNORECASE,
)

# Letters to the exchanges name BSE / NSE as the ADDRESSEE; that is never the issuer.
_ADDRESSEE_WORDS = ("stock exchange", "depository", "depositories", "securities and exchange board")
_ADDRESSEES = {"bse limited", "bse ltd", "nse limited", "national stock exchange of india limited"}
MIN_TEXT_CHARS = 200

SAME_ENTITY = "SAME_ENTITY"
SEPARATELY_LISTED = "SEPARATELY_LISTED"
POSSIBLE_GROUP_ENTITY = "POSSIBLE_GROUP_ENTITY"
OTHER_ENTITY = "OTHER_ENTITY"
UNKNOWN = "UNKNOWN"


@dataclass
class IssuerIndex:
    """Normalised company names of the whole universe -> company ids."""

    names: dict[str, set[uuid.UUID]] = field(default_factory=dict)
    display: dict[uuid.UUID, str] = field(default_factory=dict)

    def add(self, company_id: uuid.UUID, name: str | None, display: str) -> None:
        norm = normalize_text(name or "")
        for key in {norm, _SUFFIX.sub("", norm).strip()}:
            if key and len(key.split()) >= MIN_NAME_WORDS:
                self.names.setdefault(key, set()).add(company_id)
        self.display.setdefault(company_id, display)


def build_issuer_index(session: Session) -> IssuerIndex:
    index = IssuerIndex()
    for company_id, legal, common in session.execute(
        select(Company.id, Company.legal_name, Company.common_name)
    ):
        index.add(company_id, legal, legal)
        index.add(company_id, common, legal)
    return index


@dataclass
class IssuerCheck:
    status: str  # CONFIRMED | MISMATCH | UNVERIFIED
    relation: str
    issuer_name: str | None
    note: str
    other_company_id: uuid.UUID | None = None
    evidence: dict = field(default_factory=dict)

    @property
    def mismatch(self) -> bool:
        return self.status == "MISMATCH"


def _tokens(text: str) -> list[str]:
    return normalize_text(text).split()


def find_known_entities(text: str, index: IssuerIndex) -> list[tuple[str, uuid.UUID]]:
    """Universe companies named in the text (whole-phrase), longest phrase first at each spot."""
    tokens = _tokens(text)
    found: list[tuple[str, uuid.UUID]] = []
    i = 0
    while i < len(tokens):
        hit = None
        for n in range(min(MAX_NGRAM, len(tokens) - i), MIN_NAME_WORDS - 1, -1):
            phrase = " ".join(tokens[i : i + n])
            ids = index.names.get(phrase)
            if ids:
                hit = (phrase, ids, n)
                break
        if hit:
            phrase, ids, n = hit
            found.extend((phrase, cid) for cid in sorted(ids, key=str))
            i += n
        else:
            i += 1
    return found


_MONTHS = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}


def clean_entity(phrase: str) -> str | None:
    """Drop everything up to the last date / registration-number token ("... 30 2026 bse limited"
    -> "bse limited"); None if fewer than two words remain."""
    words = phrase.split()
    cut = max(
        (
            i
            for i, w in enumerate(words)
            if w in _MONTHS or any(ch.isdigit() for ch in w) and len(w) > 3 or w.isdigit()
        ),
        default=-1,
    )
    words = words[cut + 1 :]
    return " ".join(words) if len(words) >= MIN_NAME_WORDS else None


def is_addressee(phrase: str) -> bool:
    return phrase in _ADDRESSEES or any(word in phrase for word in _ADDRESSEE_WORDS)


def _brand_words(names: set[str]) -> set[str]:
    return {n.split()[0] for n in names if n and len(n.split()[0]) >= 3}


def relation_sentences(pages_text: str, company_names: set[str]) -> list[str]:
    """Short quotes where the document itself states a relationship involving this company.

    A quote is a window around a mention of the company's name that also contains a relationship
    word (subsidiary, joint venture, promoter, ...). Windows keep the quote readable and tied to
    the name instead of a whole long sentence.
    """
    flat = " ".join(pages_text.split())
    out: list[str] = []
    lowered = flat.lower()
    for name in sorted(company_names, key=len, reverse=True):
        if len(name) < 6:
            continue
        start = 0
        word = name.split()[0]
        while len(out) < 3:
            at = lowered.find(word, start)
            if at < 0:
                break
            start = at + len(word)
            window = flat[max(0, at - 120) : at + len(name) + 160]
            norm_window = f" {normalize_text(window)} "
            if f" {name} " in norm_window and _RELATION_WORDS.search(window):
                quote = window.strip()
                if quote not in out:
                    out.append(quote)
        if out:
            break
    return out


def _first_text_page(pages: list[str]) -> str:
    """First page with real text (a cover that is only an image has none)."""
    return next((p for p in pages if len(p.strip()) >= MIN_TEXT_CHARS), pages[0] if pages else "")


@dataclass(frozen=True)
class GroupInfo:
    """What a company's OWN filing says about an entity (copied words, with the page)."""

    relationship: str
    document_id: uuid.UUID
    page_number: int


RELATION_BY_WORD = (
    ("fellow subsidiary", "FELLOW_SUBSIDIARY"),
    ("ultimate holding", "HOLDING_COMPANY"),
    ("holding company", "HOLDING_COMPANY"),
    ("subsidiary", "SUBSIDIARY"),
    ("associate", "ASSOCIATE"),
    ("joint", "JOINT_VENTURE"),
    ("promoter", "PROMOTER"),
    ("related entit", "OTHER_RELATED_ENTITY"),
)


def relation_code(relationship: str) -> str:
    low = relationship.lower()
    return next((code for word, code in RELATION_BY_WORD if word in low), "OTHER_RELATED_ENTITY")


def load_group_map(session: Session, company_id: uuid.UUID) -> dict[str, GroupInfo]:
    """Entities the company's current filings list in their related-party notes."""
    rows = session.execute(
        select(GroupEntity)
        .join(DocumentExtraction, DocumentExtraction.id == GroupEntity.extraction_id)
        .where(GroupEntity.company_id == company_id, DocumentExtraction.superseded_at.is_(None))
        .order_by(GroupEntity.id)
    ).scalars()
    out: dict[str, GroupInfo] = {}
    for row in rows:
        out.setdefault(
            row.normalized_name, GroupInfo(row.relationship, row.document_id, row.page_number)
        )
        stripped = _SUFFIX.sub("", row.normalized_name).strip()
        if len(stripped.split()) >= MIN_NAME_WORDS:
            out.setdefault(stripped, GroupInfo(row.relationship, row.document_id, row.page_number))
    return out


def _with_group(check: IssuerCheck, group: dict[str, GroupInfo] | None) -> IssuerCheck:
    """Upgrade a mismatch with the relationship the company's own filing states, if any."""
    if not group or not check.mismatch or not check.issuer_name:
        return check
    norm = normalize_text(check.issuer_name)
    info = group.get(norm) or group.get(_SUFFIX.sub("", norm).strip())
    if info is None:
        return check
    check.evidence["stated_relationship"] = {
        "relationship": info.relationship,
        "document_id": str(info.document_id),
        "page": info.page_number,
    }
    check.relation = relation_code(info.relationship)
    check.note += (
        f"; the company's own filing lists it as '{info.relationship}' "
        f"(document {info.document_id}, page {info.page_number})"
    )
    return check


def check_issuer(
    company_id: uuid.UUID,
    first_pages: list[str],
    index: IssuerIndex,
    company_name_set: set[str],
    group: dict[str, GroupInfo] | None = None,
) -> IssuerCheck:
    """Classify a document from the first pages (see module docstring), then add the
    relationship the company's own filing states for the issuer, if it lists one."""
    return _with_group(_classify(company_id, first_pages, index, company_name_set), group)


def _classify(
    company_id: uuid.UUID,
    first_pages: list[str],
    index: IssuerIndex,
    company_name_set: set[str],
) -> IssuerCheck:
    """Classify a document from the text of its first pages (see module docstring)."""
    if not first_pages or not "".join(first_pages).strip():
        return IssuerCheck("UNVERIFIED", UNKNOWN, None, "no readable text in the first pages")
    header = "\n".join(_first_text_page(first_pages).splitlines()[:HEADER_LINES])
    known = find_known_entities(header, index)
    if any(cid == company_id for _, cid in known):
        return IssuerCheck("CONFIRMED", SAME_ENTITY, None, "the first page names this company")
    others = [(ph, cid) for ph, cid in known if cid != company_id and not is_addressee(ph)]
    relations = relation_sentences("\n".join(first_pages[:RELATION_PAGES]), company_name_set)
    if others:
        phrase, cid = others[0]
        return IssuerCheck(
            "MISMATCH",
            SEPARATELY_LISTED,
            index.display.get(cid, phrase),
            f"the first page names {index.display.get(cid, phrase)}, a different company",
            other_company_id=cid,
            evidence={"relation_statements": relations},
        )
    for match in _ENTITY.finditer(normalize_text(header)):
        entity = clean_entity(match.group(1))
        if entity is None or is_addressee(entity):
            continue
        brand = _brand_words(company_name_set)
        relation = POSSIBLE_GROUP_ENTITY if entity.split()[0] in brand else OTHER_ENTITY
        return IssuerCheck(
            "MISMATCH",
            relation,
            entity.title(),
            f"the first page is issued by '{entity}', which is not this company",
            evidence={"relation_statements": relations},
        )
    # No other issuer found: accept the company's own name anywhere in the first pages.
    padded = f" {normalize_text(chr(10).join(first_pages))} "
    if any(f" {name} " in padded for name in company_name_set):
        return IssuerCheck("CONFIRMED", SAME_ENTITY, None, "the first pages name this company")
    return IssuerCheck(
        "UNVERIFIED", UNKNOWN, None, "no issuing entity could be read from the first pages"
    )


def names_for(session: Session, company_id: uuid.UUID) -> set[str]:
    from app.docintel.entity import company_names  # local import: avoids a cycle at import time

    return company_names(session, company_id)


def check_document_text(
    session: Session,
    company_id: uuid.UUID,
    first_pages: list[str],
    index: IssuerIndex | None = None,
) -> IssuerCheck:
    return check_issuer(
        company_id,
        first_pages,
        index or build_issuer_index(session),
        names_for(session, company_id),
        load_group_map(session, company_id),
    )


# ---------------------------------------------------------------------------- recording


def record_check(document: Document, result: IssuerCheck, *, forced: bool = False) -> None:
    document.issuer_name = result.issuer_name
    document.association_status = "FORCED" if forced and result.mismatch else result.status
    document.association_note = result.note[:2000]
    document.association_checked_at = datetime.now(UTC)


def add_event(
    session: Session,
    document: Document,
    *,
    before: uuid.UUID | None,
    after: uuid.UUID | None,
    action: str,
    reason: str,
    actor: str,
    evidence: dict | None = None,
) -> DocumentLinkEvent:
    event = DocumentLinkEvent(
        document_id=document.id,
        company_before=before,
        company_after=after,
        action=action,
        reason=reason[:2000],
        actor=actor,
        evidence=evidence,
    )
    session.add(event)
    return event


@dataclass
class UnlinkReport:
    document_id: uuid.UUID
    company_before: uuid.UUID | None
    facts_withdrawn: int
    staged_facts_detached: int


def unlink_document(
    session: Session,
    document: Document,
    *,
    reason: str,
    actor: str,
    result: IssuerCheck | None = None,
) -> UnlinkReport:
    """Detach a document from its company. The file and the document row stay.

    Everything is recorded: an audit event holds the previous company and the exact validated
    values withdrawn from `financial_facts`. Staged values in `extracted_facts` keep their rows
    (their company is cleared), so nothing the extractor read is lost.
    """
    before = document.company_id
    withdrawn = list(
        session.scalars(select(FinancialFact).where(FinancialFact.document_id == document.id))
    )
    snapshot = [
        {
            "statement": f.statement.value,
            "line_item": f.line_item,
            "period_end": f.period_end.isoformat(),
            "period_type": f.period_type.value,
            "basis": f.basis.value,
            "value": str(f.original_value),
            "unit": f.original_unit,
            "page": f.source_page,
        }
        for f in withdrawn
    ]
    evidence = {"withdrawn_facts": snapshot}
    if result is not None:
        evidence.update(issuer=result.issuer_name, relation=result.relation, **result.evidence)
    if withdrawn:
        session.execute(delete(FinancialFact).where(FinancialFact.document_id == document.id))
    detached = (
        session.execute(
            update(ExtractedFact)
            .where(ExtractedFact.document_id == document.id)
            .values(company_id=None)
        ).rowcount
        or 0
    )
    document.company_id = None
    if result is not None:
        record_check(document, result)
    add_event(
        session,
        document,
        before=before,
        after=None,
        action="UNLINKED",
        reason=reason,
        actor=actor,
        evidence=evidence,
    )
    session.flush()
    return UnlinkReport(document.id, before, len(withdrawn), detached)


def company_label(session: Session, company_id: uuid.UUID | None) -> str:
    if company_id is None:
        return "-"
    company = session.get(Company, company_id)
    return company.common_name if company else str(company_id)
