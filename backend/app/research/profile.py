"""Company profile: history, leadership, strategic changes, exposure, segments and group structure.

Nothing here is written by EquityOS about the company. Each item is either

* a short sentence quoted verbatim from the company's own latest stored annual report, with its page
  (history, leadership, strategic changes, geographic and customer exposure), filed under a topic by
  keyword cues; or
* a figure from validated evidence (segment revenue and result, group entities listed in the report).

There is no structured database of founders, management biographies or milestones in EquityOS. Where the
report does not yield a topic the profile says so instead of filling the gap.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Document, DocumentExtraction, DocumentPage, GroupEntity
from app.research import mgmt
from app.research import readiness as rd
from app.research.analysis import _s
from app.research.sectors import AMOUNT, Metric

VERSION = "1.0"
MAX_PER_TOPIC = 4
YEAR = re.compile(r"\b(?:18|19|20)\d{2}\b")
NAMED = re.compile(r"\b(?:Mr|Ms|Mrs|Dr|Shri|Smt|Prof)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+")
TITLE = (
    r"(?:Managing Director|Chief Executive Officer|CEO|Chairman|Chairperson|Whole[- ]time Director|"
    r"Chief Financial Officer|CFO|Executive Director|Deputy Managing Director|President)"
)

# topic -> (title, cue regex, extra requirement on the sentence)
TOPICS: dict[str, tuple[str, str, str]] = {
    "history": (
        "History and milestones",
        r"\b(was incorporated|incorporated (?:in|on|under)|was established|established in|"
        r"was founded|founded in|was set up|set up in|commenced (?:its )?(?:business|operations)|"
        r"began (?:its )?operations|started (?:its )?operations|was listed on)\b",
        "year",
    ),
    "strategic": (
        "Strategic changes, acquisitions and transformation",
        r"\b(merger of|merged (?:with|into)|amalgamat\w+|acquired|acquisition of|acquire[sd]? (?:a|the)|"
        r"divest\w*|demerger|demerged|scheme of arrangement|joint venture|strategic partnership|"
        r"strategic investment|restructur\w+ of)\b",
        "",
    ),
    "exposure": (
        "Geographic and customer exposure",
        r"\b(geograph\w+ (?:mix|footprint|presence|spread|segment|revenue)|footprint spanning|"
        r"(?:revenue|business|clients?|customers?)\b[^.]{0,60}\b(?:North America|Europe|United Kingdom|"
        r"Asia[- ]Pacific|Middle East|overseas|international)\b|"
        r"top (?:five|ten|5|10) (?:clients|customers)|customer concentration|"
        r"(?:North America|Continental Europe|United Kingdom|Asia[- ]Pacific)\b[^.]{0,80}"
        r"(?:revenue|contribut|grew|growth))",
        "",
    ),
}
_RX = {k: re.compile(v[1], re.I | re.S) for k, v in TOPICS.items()}
_SKIP = re.compile(
    r"(Listing Regulations|Regulation \d+|Section \d+|Companies Act|attended|remuneration|sitting fee|"
    r"Nomination and Remuneration|\bDIN\b|\bPAN\b|\bcircular\b|unclaimed|Secretarial|dematerial|"
    r"Programme|programme|CSR|beneficiar|participants|\bNPS\b|Net Promoter|scholarship|villages|donat|"
    r"volunteer|Foundation|Guinness|capital assets|acquisition cost|fair value|Committee|employees|"
    r"Takeovers|Stock Ownership|Stock Option|learning hours|competenc|Regulations, 20|Trust\b|"
    r"recognised among|Business Today|Fortune|previous roles|did not have|did not|not applicable|"
    r"no joint venture|Investor Education|Protection Fund|BRSR|Business Responsibility|incorporated in India|"
    r"\bnil\b|"
    r"benefits?|insurance|energy|regulator|provision|goodwill|impairment|deferred|tax)",
    re.I,
)

# Names seen next to a leadership title (signatures, board and management listings).
LEADER_TITLES = (
    r"Managing Director\s*(?:&|and)\s*(?:Chief Executive Officer|CEO)|"
    r"Chief Executive Officer\s*(?:&|and)\s*Managing Director|Chief Executive Officer|"
    r"Chief Financial Officer|Chairman|Chairperson|Executive Chairman|Whole[- ]time Director"
)
_LEADER = re.compile(
    r"((?:[A-Z][A-Za-z.\-]+ ){1,3}?[A-Z][A-Za-z.\-]+)\s+(" + LEADER_TITLES + r")\b"
)
_NOT_NAME = {
    "director",
    "directors",
    "independent",
    "executive",
    "non-executive",
    "non",
    "part-time",
    "nominee",
    "managing",
    "chief",
    "officer",
    "chairman",
    "chairperson",
    "whole-time",
    "the",
    "and",
    "our",
    "of",
    "message",
    "statement",
    "company",
    "bank",
    "limited",
    "board",
    "senior",
    "group",
    "head",
    "ceo",
    "cfo",
    "from",
    "dear",
    "regards",
    "warm",
    "page",
    "report",
    "annual",
    "integrated",
    "attendance",
    "audit",
    "committee",
    "partner",
    "risk",
    "management",
    "nomination",
    "remuneration",
    "stakeholders",
    "corporate",
    "social",
    "responsibility",
    "shareholders",
    "relationship",
    "sustainability",
    "name",
    "designation",
    "member",
    "members",
    "secretary",
    "compliance",
    "officer,",
    "dr",
    "mr",
    "ms",
}


def _canon_title(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    if re.match(r"(Managing Director|Chief Executive Officer)", t) and (
        "&" in t or "and" in t.lower()
    ):
        return "Managing Director and CEO"
    return {"CEO": "Chief Executive Officer"}.get(t, t)


def leaders(pages: list[tuple[int, str]]) -> list[dict]:
    """People named beside a leadership title at least twice in the report, with the pages seen."""
    found: dict[tuple[str, str], list[int]] = {}
    for page, text in pages:
        flat = re.sub(r"\s+", " ", text)
        for m in _LEADER.finditer(flat):
            words = [w for w in m.group(1).split() if w.lower().strip(".") not in _NOT_NAME]
            # the name is the run of capitalised words directly before the title
            tail = m.group(1).split()
            name_words: list[str] = []
            for w in reversed(tail):
                if w.lower().strip(".") in _NOT_NAME:
                    break
                name_words.insert(0, w)
            if len(name_words) < 2 or len(name_words) > 4 or not words:
                continue
            name = " ".join(name_words)
            found.setdefault((name, _canon_title(m.group(2))), []).append(page)
    people: dict[str, list[dict]] = {}
    for (name, title), pgs in found.items():
        if len(pgs) >= 2:
            people.setdefault(name, []).append(
                {"title": title, "mentions": len(pgs), "pages": sorted(set(pgs))[:4]}
            )
    out = [
        {
            "name": n,
            "titles": sorted(ts, key=lambda t: -t["mentions"]),
            "mentions": sum(t["mentions"] for t in ts),
        }
        for n, ts in people.items()
    ]
    return sorted(out, key=lambda r: -r["mentions"])[:8]


def _usable(sentence: str, need: str) -> bool:
    if _SKIP.search(sentence):
        return False
    if need == "year" and not YEAR.search(sentence):
        return False
    if need == "named" and not NAMED.search(sentence):
        return False
    return True


def extract(pages: list[tuple[int, str]]) -> dict[str, list[dict]]:
    """Topic -> up to MAX_PER_TOPIC quoted sentences with their page, each sentence used once."""
    pool: dict[str, list[tuple[int, int, str]]] = {k: [] for k in TOPICS}
    seen: set[str] = set()
    for page, text in pages:
        for s in mgmt.sentences(text):
            key = re.sub(r"\W+", " ", s.lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            for topic, rx in _RX.items():
                if rx.search(s) and _usable(s, TOPICS[topic][2]):
                    bonus = len(YEAR.findall(s)) + (2 if NAMED.search(s) else 0)
                    pool[topic].append((len(rx.findall(s)) * 2 + bonus, page, s))
    used: set[str] = set()
    out: dict[str, list[dict]] = {}
    for topic in TOPICS:
        chosen = []
        for _, page, s in sorted(pool[topic], key=lambda t: (-t[0], t[1])):
            if s in used:
                continue
            used.add(s)
            chosen.append({"text": s, "page": page})
            if len(chosen) == MAX_PER_TOPIC:
                break
        out[topic] = sorted(chosen, key=lambda c: c["page"])
    return out


def _latest_annual_report(session: Session, company_id: uuid.UUID):
    docs = list(
        session.scalars(
            select(Document).where(
                Document.company_id == company_id, Document.doc_type == "annual_report"
            )
        )
    )
    best = None
    for d in docs:
        ext = session.scalar(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == d.id)
            .order_by(DocumentExtraction.id.desc())
            .limit(1)
        )
        if ext is None:
            continue
        n = session.scalar(
            select(func.count())
            .select_from(DocumentPage)
            .where(DocumentPage.extraction_id == ext.id)
        )
        if n and (best is None or n > best[2]):
            best = (d, ext, n)
    return best


def narrative(session: Session, company_id: uuid.UUID) -> dict:
    found = _latest_annual_report(session, company_id)
    if found is None:
        return {
            "available": False,
            "reason": "no annual report with stored page text is held for this company",
        }
    doc, ext, n = found
    rows = session.execute(
        select(DocumentPage.page_number, DocumentPage.text)
        .where(DocumentPage.extraction_id == ext.id)
        .order_by(DocumentPage.page_number)
    ).all()
    pages = [(p, t) for p, t in rows]
    topics = extract(pages)
    return {
        "available": True,
        "document": {
            "id": str(doc.id),
            "title": doc.title,
            "period": doc.financial_period,
            "pages_read": n,
        },
        "leaders": leaders(pages),
        "topics": [{"key": k, "title": TOPICS[k][0], "items": topics[k]} for k in TOPICS],
        "method": (
            "short sentences quoted verbatim from the company's latest stored annual report, filed by "
            "keyword cues and shown with their pages; nothing is paraphrased or added"
        ),
    }


# ---------------------------------------------------------------- segments and group structure

SEG_REV = Metric("seg_rev", "Segment revenue", ("SEGMENT.segment_revenue",), AMOUNT, ptype="ANNUAL")
SEG_RES = Metric(
    "seg_res",
    "Segment result",
    ("SEGMENT.segment_result", "SEGMENT.segment_result_ebit", "SEGMENT.segment_result_ebitda"),
    AMOUNT,
    ptype="ANNUAL",
)
SEGMENT_NAMES = {n for m in (SEG_REV, SEG_RES) for n in m.names}


def _by_dimension(store: rd._Store, m: Metric, ptype: str, basis: str | None):
    """dimension -> {period_end: record}, one best record per segment and period."""
    out: dict[str, dict] = {}
    for name in m.names:
        for r in store.by_metric.get(name, []):
            if r.value is None or r.period_end is None or r.period_type != ptype:
                continue
            if basis is not None and r.basis not in (basis, None):
                continue
            if not r.dimension:
                continue
            cur = out.setdefault(r.dimension, {}).get(r.period_end)
            if cur is None or m.names.index(r.metric) < m.names.index(cur.metric):
                out[r.dimension][r.period_end] = r
    return out


def segments(store: rd._Store, prefer: str) -> dict:
    """Segment revenue, its share of the reported total, growth and result, on one period basis."""
    for ptype in ("ANNUAL", "QUARTERLY"):
        rev = _by_dimension(store, SEG_REV, ptype, prefer)
        if len(rev) < 2:
            continue
        ends = sorted({d for per in rev.values() for d in per})
        latest = ends[-1]
        prev = next((e for e in reversed(ends[:-1]) if (latest - e).days > 300), None)
        res = _by_dimension(store, SEG_RES, ptype, prefer)
        rows, total = [], Decimal(0)
        for per in rev.values():
            if latest in per:
                total += per[latest].value
        for name, per in sorted(
            rev.items(), key=lambda kv: -(kv[1][latest].value if latest in kv[1] else 0)
        ):
            r = per.get(latest)
            if r is None:
                continue
            p = per.get(prev) if prev else None
            growth = (
                ((r.value / p.value - 1) * 100)
                if p is not None and p.value and p.value > 0
                else None
            )
            rr = res.get(name, {}).get(latest)
            rows.append(
                {
                    "segment": name,
                    "revenue": _s(r.value),
                    "share_pct": _s((r.value / total * 100).quantize(Decimal("0.1")))
                    if total > 0
                    else None,
                    "growth_pct": _s(growth.quantize(Decimal("0.1")))
                    if growth is not None
                    else None,
                    "result": _s(rr.value) if rr is not None else None,
                    "result_margin_pct": _s((rr.value / r.value * 100).quantize(Decimal("0.1")))
                    if rr is not None and r.value
                    else None,
                    "evidence_id": r.id,
                    "unit": r.unit,
                }
            )
        return {
            "available": True,
            "period_end": latest.isoformat(),
            "prior_period_end": prev.isoformat() if prev else None,
            "period_type": ptype,
            "basis": prefer,
            "rows": rows,
            "note": (
                "Share is of the sum of the reported segment revenues for that period (before any "
                "inter-segment elimination). Growth compares with the same period a year earlier where it "
                "is stored; segment result is as the company defines it."
            ),
        }
    return {
        "available": False,
        "reason": "no segment revenue by segment is stored (the company reports one segment, or its "
        "segment note has not been extracted)",
    }


def group_structure(session: Session, company_id: uuid.UUID) -> dict:
    rows = list(
        session.execute(
            select(GroupEntity.entity_name, GroupEntity.relationship, GroupEntity.page_number)
            .where(GroupEntity.company_id == company_id)
            .order_by(GroupEntity.relationship, GroupEntity.entity_name)
        ).all()
    )
    if not rows:
        return {
            "available": False,
            "reason": "no group entities were extracted from the stored documents",
        }
    seen, out = set(), []
    for name, rel, page in rows:
        if (name, rel) in seen:
            continue
        seen.add((name, rel))
        out.append({"name": name, "relationship": rel, "page": page})
    counts: dict[str, int] = {}
    for r in out:
        counts[r["relationship"]] = counts.get(r["relationship"], 0) + 1
    return {"available": True, "entities": out[:40], "total": len(out), "by_relationship": counts}


def build(session: Session, company_id: uuid.UUID, store: rd._Store, prefer: str) -> dict:
    return {
        "version": VERSION,
        "narrative": narrative(session, company_id),
        "segments": segments(store, prefer),
        "group": group_structure(session, company_id),
        "not_available": (
            "Structured founder and management biographies, board histories and customer-concentration "
            "statistics are not held in EquityOS; only what the company's own annual report states is shown."
        ),
    }
