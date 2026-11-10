"""Corporate actions verified against documents EquityOS already holds.

A price series that halves overnight is only a SUSPICION (`PRICE_GAP_ONLY`, see adjust.py). This
module turns it into a recorded corporate action only when BOTH of these hold:

1. an official document of the company, already stored and read by the extractor, states the bonus
   or split in words: the ratio and the record date, on a named page, quoted;
2. the stored official price series shows exactly that factor on the trading day just before the
   record date (the ex-date under T+1 settlement), within the tolerance used for price gaps.

The ex-date is therefore the first day the official prices show the factor; the document supplies
the ratio and the record date. Both are stored with the action (document, page, quote), so the
adjustment that follows is traceable to its sources. Raw prices are never touched. Nothing is
recorded when either condition is missing: a company whose price gap has no stored document remains
"suspected, awaiting evidence".
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calc.adjust import (
    PRICE_GAP_TOLERANCE,
    detect_implied,
    parse_action_factor,
    price_rows,
)
from app.models import CorporateAction, Document, DocumentExtraction, DocumentPage, Security

PROVIDER = "DOCUMENT_VERIFIED"
WINDOW_BEFORE = 6  # days before the record date in which the ex-date may fall
WINDOW_AFTER = 2
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"],
        start=1,
    )
}  # fmt: skip
_DATE = (
    r"(?:(?P<m1>[A-Za-z]{3,9})\s+(?P<d1>\d{1,2})\s*,?\s+(?P<y1>\d{4})"
    r"|(?P<d2>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m2>[A-Za-z]{3,9})\s*,?\s+(?P<y2>\d{4}))"
)
_RATIO = re.compile(
    r"(?i)\bbonus\b[^.]{0,160}?\b(?:in\s+the\s+(?:proportion|ratio)\s+of|ratio\s+of)\s*"
    r"(?P<new>\d{1,2})\s*:\s*(?P<held>\d{1,2})"
)
_RECORD = re.compile(rf"(?i)record\s+date[^.]{{0,40}}?(?:was|is|being|i\.e\.,?|:)?\s*{_DATE}")


@dataclass
class DocumentEvidence:
    document_id: uuid.UUID
    title: str | None
    page: int
    ratio_text: str
    record_date: date
    quote: str


@dataclass
class Verification:
    status: str  # VERIFIED | ALREADY_RECORDED | NO_DOCUMENT | NO_PRICE_EVIDENCE
    reason: str
    action: CorporateAction | None = None
    evidence: DocumentEvidence | None = None


def _parse_date(m: re.Match) -> date | None:
    try:
        if m.group("m1"):
            month, day, year = m.group("m1"), m.group("d1"), m.group("y1")
        else:
            month, day, year = m.group("m2"), m.group("d2"), m.group("y2")
        return date(int(year), _MONTHS[month.lower()], int(day))
    except (KeyError, ValueError):
        return None


def find_bonus_in_documents(session: Session, company_id: uuid.UUID) -> list[DocumentEvidence]:
    """Bonus statements (ratio AND record date) in the company's stored documents."""
    rows = session.execute(
        select(DocumentPage.page_number, DocumentPage.text, Document)
        .join(DocumentExtraction, DocumentExtraction.id == DocumentPage.extraction_id)
        .join(Document, Document.id == DocumentExtraction.document_id)
        .where(
            Document.company_id == company_id,
            Document.association_status != "MISMATCH",
            DocumentExtraction.superseded_at.is_(None),
            DocumentPage.text.op("~*")(r"bonus"),
        )
        .order_by(Document.retrieved_at, DocumentPage.page_number)
    ).all()
    found: dict[tuple, DocumentEvidence] = {}
    for page, text, doc in rows:
        flat = " ".join(text.split())
        for m in _RATIO.finditer(flat):
            window = flat[m.start() : m.end() + 420]
            rec = _RECORD.search(window)
            record = _parse_date(rec) if rec else None
            if record is None:
                continue
            ratio = f"{m.group('new')}:{m.group('held')}"
            key = (ratio, record)
            if key not in found:
                found[key] = DocumentEvidence(
                    doc.id,
                    doc.title,
                    page,
                    ratio,
                    record,
                    flat[max(0, m.start() - 80) : m.end() + 300],
                )
    return list(found.values())


def _price_gap(session: Session, company_id: uuid.UUID, record_date: date, factor: Decimal):
    """The event where the official prices show `factor` near the record date."""
    lo, hi = record_date - timedelta(days=WINDOW_BEFORE), record_date + timedelta(days=WINDOW_AFTER)
    for sec in session.scalars(select(Security).where(Security.company_id == company_id)):
        for e in detect_implied(price_rows(session, sec.id)):
            if lo <= e.ex_date <= hi and e.confidence in ("PRICE_GAP_ONLY", "SINGLE_EXCHANGE"):
                if abs(e.factor / factor - 1) <= PRICE_GAP_TOLERANCE:
                    return sec, e
    return None


def verify_bonus(
    session: Session,
    company_id: uuid.UUID,
    evidence: DocumentEvidence,
    *,
    now: datetime | None = None,
) -> Verification:
    now = now or datetime.now(UTC)
    parsed = parse_action_factor("Bonus", evidence.ratio_text)
    if parsed is None:
        return Verification(
            "NO_DOCUMENT", f"ratio {evidence.ratio_text} is not unambiguous", None, evidence
        )
    factor = parsed[0]
    gap = _price_gap(session, company_id, evidence.record_date, factor)
    if gap is None:
        return Verification(
            "NO_PRICE_EVIDENCE",
            f"the document states a {evidence.ratio_text} bonus "
            f"(record date {evidence.record_date}) "
            f"but the official prices show no factor of {factor} near that date; nothing recorded",
            None,
            evidence,
        )
    security, event = gap
    existing = session.scalar(
        select(CorporateAction).where(
            CorporateAction.company_id == company_id,
            CorporateAction.action_type == "Bonus",
            CorporateAction.effective_date == event.ex_date,
        )
    )
    if existing is not None:
        return Verification(
            "ALREADY_RECORDED",
            f"a bonus is already recorded for {event.ex_date} ({existing.provider_code})",
            existing,
            evidence,
        )
    doc = session.get(Document, evidence.document_id)
    action = CorporateAction(
        company_id=company_id,
        provider_code=PROVIDER,
        action_type="Bonus",
        effective_date=event.ex_date,
        ratio_text=evidence.ratio_text,
        details={
            "verification": {
                "document_id": str(evidence.document_id),
                "document_title": evidence.title,
                "page": evidence.page,
                "quote": evidence.quote,
                "record_date": evidence.record_date.isoformat(),
                "ex_date_basis": (
                    "first trading day on which the stored official prices show the factor; the "
                    "document states the record date"
                ),
                "price_evidence": {
                    "symbol": security.symbol,
                    "prior_close": str(event.prior_close),
                    "factor": str(event.factor),
                },
            }
        },
        source_url=doc.url if doc else "",
        retrieved_at=now,
    )
    session.add(action)
    session.flush()
    return Verification(
        "VERIFIED",
        f"{evidence.ratio_text} bonus, record date {evidence.record_date}, ex-date {event.ex_date} "
        f"(document page {evidence.page}; official prices factor {event.factor:.4f})",
        action,
        evidence,
    )


def verify_company(session: Session, company_id: uuid.UUID) -> list[Verification]:
    """Verify each bonus the stored documents state; record what both sources back."""
    results = []
    for evidence in find_bonus_in_documents(session, company_id):
        results.append(verify_bonus(session, company_id, evidence))
    if not results:
        results.append(
            Verification("NO_DOCUMENT", "no stored document of the company states a bonus issue")
        )
    return results
