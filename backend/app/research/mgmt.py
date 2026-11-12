"""Management commentary, read from the stored management-discussion section (report phase 4).

The engine is extractive and rule-based, not generative: it scans the pages of a stored
"Management's Discussion and Analysis" section, splits them into sentences, and files the sentences
that match a cue list under a topic. A sentence is quoted (short, with its page) or skipped; nothing
is paraphrased or invented, so management's words are never turned into EquityOS statements.

Every item is a MANAGEMENT STATEMENT. A forward-looking statement stays a statement of intent: it is
labelled as such and is never presented as a verified fact. Whether the topics changed since the
previous report needs two annual reports; with one, the report says so.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentPage, DocumentSection

VERSION = "1.0"
MAX_PER_TOPIC = 3
MIN_LEN, MAX_LEN = 70, 300

# topic -> (title, cue regex, forward looking?)
TOPICS: dict[str, tuple[str, str, bool]] = {
    "priorities": (
        "Stated priorities and strategy",
        r"\b(strategy|strategic|priorit(?:y|ies)|we (?:are )?focus(?:ed|ing)?|focus(?:es)? on|"
        r"our (?:aim|vision|objective|aspiration)|we aim|we aspire)\b",
        False,
    ),
    "expansion": (
        "Expansion and growth initiatives",
        r"\b(expan(?:d|sion|ding)|new branch(?:es)?|branch network|add(?:ed|ing)? (?:\d|new)|"
        r"launch(?:ed)?|acqui(?:re|sition)|integration|merger|scal(?:e|ing) up)\b",
        False,
    ),
    "capital": (
        "Capital allocation and capital position",
        r"\b(dividend|capital (?:adequacy|raise|plan|position|infusion)|CET ?1|tier ?1|"
        r"buy-?back|raised capital|rights issue|bonus)\b",
        False,
    ),
    "outlook": (
        "Outlook and forward-looking statements",
        r"\b(outlook|expect(?:s|ed)? to|anticipate[sd]?|going forward|in the coming|"
        r"over the (?:next|medium)|poised|likely to|projected|will continue to)\b",
        True,
    ),
    "risks": (
        "Risk management",
        r"\b(risk (?:management|appetite|framework|governance)|credit risk|market risk|"
        r"operational risk|liquidity risk|cyber|stress test|fraud)\b",
        False,
    ),
    "regulatory": (
        "Regulatory developments",
        r"\b(RBI|Reserve Bank|Basel|master direction|regulatory (?:changes?|norms?|requirements?|"
        r"framework)|new (?:norms|guidelines|regulations)|policy rate|repo rate)\b",
        False,
    ),
    "challenges": (
        "Challenges and execution risks",
        r"\b(challenge[sd]?|headwind|pressure|competition|competitive|volatil(?:e|ity)|"
        r"uncertaint(?:y|ies)|slowdown|moderat(?:e|ion)|compress(?:ion|ed))\b",
        False,
    ),
    "developments": (
        "Reported developments",
        r"\b(grew|growth of|increased by|improved (?:to|by)|rose (?:to|by)|record|highest)\b.*\d",
        False,
    ),
}
_COMPILED = {k: re.compile(v[1], re.I) for k, v in TOPICS.items()}
# Governance, filing and table furniture that merely contains a cue word.
_EXCLUDE = re.compile(
    r"(Listing Regulations|Regulation \d+|Secretarial Standard|ICSI|UPSI|Insider Trading|Rating Action|"
    r"Risk Type Definition|Guidance, Challenge|Nomination and Remuneration|Annual General Meeting|"
    r"Committee of the Board|unclaimed|Schedule V|Companies Act|Section \d+|SS-\d)",
    re.I,
)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _clean(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"-\n(?=[a-z])", "", text)  # a word broken across lines
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _good(sentence: str) -> bool:
    if not (MIN_LEN <= len(sentence) <= MAX_LEN):
        return False
    letters = sum(c.isalpha() for c in sentence)
    digits = sum(c.isdigit() for c in sentence)
    if letters < 0.6 * len(sentence) or digits > 0.2 * len(sentence):
        return False  # tables, numbers, page furniture
    if _EXCLUDE.search(sentence):
        return False
    if sentence.count(" ") < 9 or sentence.isupper():
        return False
    return sentence[0].isupper() and sentence.endswith((".", "!", "?"))


_FURNITURE = re.compile(
    r"^(?:(?:Management\u2019?'?s Discussion and Analysis|Standalone Financial Statements|Consolidated Financial Statements|Integrated Annual Report[^.]{0,24}?|Board\u2019?'?s Report|Corporate Overview|Statutory Reports|Financial Statements|Business Performance Review|Governance|Management Discussion (?:&|and) Analysis|Annual Report\s*20\d\d-\d\d)\s*\d*\s*\|?\s*)+",
    re.I,
)


def sentences(text: str) -> list[str]:
    out = []
    for part in _SENT_SPLIT.split(_clean(text)):
        part = _FURNITURE.sub("", part.strip()).strip()
        if part and part[0].islower():
            continue  # a fragment left after page furniture
        if _good(part):
            out.append(part)
    return out


def _score(sentence: str, rx: re.Pattern) -> int:
    hits = len(rx.findall(sentence))
    first_person = 1 if re.search(r"\b(we|our|the bank|the company)\b", sentence, re.I) else 0
    return hits * 2 + first_person


def extract(pages: list[tuple[int, str]]) -> dict[str, list[dict]]:
    """Topic -> up to MAX_PER_TOPIC quoted sentences with their page. Each sentence is used once."""
    candidates: dict[str, list[tuple[int, int, str]]] = {k: [] for k in TOPICS}
    seen: set[str] = set()
    for page, text in pages:
        for s in sentences(text):
            key = re.sub(r"\W+", " ", s.lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            for topic, rx in _COMPILED.items():
                if rx.search(s):
                    candidates[topic].append((_score(s, rx), page, s))
    used: set[str] = set()
    result: dict[str, list[dict]] = {}
    for topic in TOPICS:
        chosen = []
        for _, page, s in sorted(candidates[topic], key=lambda t: (-t[0], t[1])):
            if s in used:
                continue
            used.add(s)
            chosen.append({"text": s, "page": page})
            if len(chosen) == MAX_PER_TOPIC:
                break
        result[topic] = chosen
    return result


def _pages(session: Session, section: DocumentSection) -> list[tuple[int, str]]:
    rows = session.execute(
        select(DocumentPage.page_number, DocumentPage.text)
        .where(
            DocumentPage.extraction_id == section.extraction_id,
            DocumentPage.page_number.between(section.start_page, section.end_page),
        )
        .order_by(DocumentPage.page_number)
    ).all()
    return [(n, t) for n, t in rows]


def commentary(session: Session, company_id: uuid.UUID) -> dict:
    """Structured commentary from the latest management-discussion section(s), or a stated gap."""
    from app.research.profile import private_document_ids

    hidden = private_document_ids(session, company_id)
    rows = session.execute(
        select(DocumentSection, Document)
        .join(Document, Document.id == DocumentSection.document_id)
        .where(Document.company_id == company_id, DocumentSection.kind == "MANAGEMENT_DISCUSSION")
        .where(Document.id.not_in(hidden) if hidden else True)
        .order_by(
            Document.created_at.desc(),
            DocumentSection.extraction_id.desc(),
            DocumentSection.start_page,
        )
    ).all()
    if not rows:
        return {
            "available": False,
            "reason": "no management-discussion section is stored for this company",
        }
    # the newest document's newest extraction only
    first_doc = rows[0][1].id
    first_ext = rows[0][0].extraction_id
    sections = [s for s, d in rows if d.id == first_doc and s.extraction_id == first_ext]
    doc = rows[0][1]
    pages: list[tuple[int, str]] = []
    for sec in sections:
        pages += _pages(session, sec)
    if not pages:
        return {"available": False, "reason": "the section's page text is not stored"}
    topics = extract(pages)
    return {
        "available": True,
        "version": VERSION,
        "document": {
            "id": str(doc.id),
            "title": doc.title,
            "type": doc.doc_type,
            "period": doc.financial_period,
            "sections": [
                {"heading": s.heading, "start_page": s.start_page, "end_page": s.end_page}
                for s in sections
            ],
        },
        "topics": [
            {
                "key": k,
                "title": TOPICS[k][0],
                "forward_looking": TOPICS[k][2],
                "items": topics[k],
            }
            for k in TOPICS
            if topics[k]
        ],
        "pages_read": len(pages),
        "method": (
            "extractive: sentences quoted verbatim from the stored section, filed by keyword cues; "
            "nothing is paraphrased. All items are management statements, not verified facts"
        ),
        "comparison": (
            "A change from earlier management commentary needs a second annual report; only one is "
            "stored, so no change is reported."
        ),
    }
