"""Evidence readiness: can a research report be written from what EquityOS already holds?

This module only READS. It looks at the validated evidence store (selected, unblocked values),
compares it with the company's data requirements (`requirements.py`) and returns:

* the status of every requirement (AVAILABLE / STALE / MISSING), with the value, period, basis,
  source document and page behind it, or the exact reason it is missing and which inputs are absent;
* the missing-data assessment (what, why it matters, which calculation or section it affects, which
  document supplies it, which period and basis, what EquityOS already holds);
* a Research Readiness Score with its seven components. It measures the EVIDENCE, never the
  company: it is unrelated to the financial score;
* an eligibility decision that does not rest on a percentage alone: any CRITICAL gap, a weak
  component or an open validation error makes the research ASSISTED however high the average is.
"""

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Company,
    CorporateAction,
    Document,
    DocumentExtraction,
    EodPrice,
    EvidenceLink,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    ExtractedFact,
    FinancialFact,
    Security,
    ShareholdingFact,
)
from app.research import clock
from app.research import preferences as pf
from app.research import requirements as rq

AVAILABLE, STALE, MISSING = "AVAILABLE", "STALE", "MISSING"
AUTOMATIC, ASSISTED, BLOCKED = "AUTOMATIC", "ASSISTED", "BLOCKED"
UNRESOLVED_KINDS = ("PRICE", "DOCUMENT_SECTION", "TECHNICAL")  # not subject to cell resolution
TIER_SCORE = {1: 100, 2: 95, 3: 85, 4: 70, 5: 50, 6: 40}
QUALITY_SCORE = {"HIGH": 1.0, "MEDIUM": 0.75, "LOW": 0.4}
STATUS_VALUE = {AVAILABLE: 1.0, STALE: 0.25, MISSING: 0.0}
COMPONENTS = (
    "financial_completeness",
    "document_coverage",
    "source_reliability",
    "period_consistency",
    "validation_coverage",
    "sector_metrics",
    "valuation_inputs",
)
COMPONENT_LABELS = {
    "financial_completeness": "Financial data completeness",
    "document_coverage": "Document coverage",
    "source_reliability": "Source reliability",
    "period_consistency": "Reporting-period consistency",
    "validation_coverage": "Metric validation coverage",
    "sector_metrics": "Sector-specific metrics",
    "valuation_inputs": "Valuation inputs",
}
HELD_TYPES = ("annual_report", "quarterly_result", "investor_presentation", "earnings_transcript")


@dataclass
class ReqStatus:
    req: rq.Requirement
    status: str = MISSING
    metric: str | None = None
    value: Decimal | None = None
    unit: str | None = None
    period_end: date | None = None
    period_type: str | None = None
    basis: str | None = None
    evidence_id: int | None = None
    document_id: uuid.UUID | None = None
    page: int | None = None
    provider: str | None = None
    tier: int | None = None
    quality: str | None = None
    reason: str | None = None  # why it is not AVAILABLE
    missing_inputs: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    action: str = "NONE"
    held_documents: list[dict] = field(default_factory=list)
    dimension: str = ""  # which measure of the metric (domestic / whole-bank NIM, ...)
    period_note: str | None = None  # e.g. the annual figure was used: no recent quarter
    other_basis: dict | None = None  # the basis the user did NOT ask for, when only that exists

    @property
    def ok(self) -> bool:
        return self.status == AVAILABLE


@dataclass
class Readiness:
    profile: str
    profile_label: str
    as_of: date
    requirement_version: str
    statuses: list[ReqStatus]
    components: dict[str, dict]
    score: float
    eligibility: str
    confidence: str
    reasons: list[str]
    open_errors: int
    sections: list[dict]
    held: list[dict]
    thresholds: dict
    preferences: pf.Preferences = pf.DEFAULT

    def by_id(self, rid: str) -> ReqStatus | None:
        return next((s for s in self.statuses if s.req.id == rid), None)

    def missing(self, importance: str) -> list[ReqStatus]:
        return [s for s in self.statuses if s.req.importance == importance and not s.ok]

    def as_dict(self) -> dict:
        crit, imp, opt = (self.missing(i) for i in rq.IMPORTANCE_ORDER)
        return {
            "profile": self.profile,
            "profile_label": self.profile_label,
            "as_of": self.as_of.isoformat(),
            "requirement_version": self.requirement_version,
            "score": self.score,
            "eligibility": self.eligibility,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "open_validation_errors": self.open_errors,
            "components": self.components,
            "thresholds": self.thresholds,
            "counts": {
                "requirements": len(self.statuses),
                "available": sum(1 for s in self.statuses if s.ok),
                "critical_missing": len(crit),
                "important_missing": len(imp),
                "optional_missing": len(opt),
            },
            "requirements": [status_dict(s, self.preferences) for s in self.statuses],
            "missing": {
                "CRITICAL": [status_dict(s, self.preferences) for s in crit],
                "IMPORTANT": [status_dict(s, self.preferences) for s in imp],
                "OPTIONAL": [status_dict(s, self.preferences) for s in opt],
            },
            "sections": self.sections,
            "documents_held": self.held,
            "preferences": {
                **self.preferences.to_dict(),
                "period_label": self.preferences.period_label(),
            },
        }


def status_dict(s: ReqStatus, prefs: pf.Preferences = pf.DEFAULT) -> dict:
    r = s.req
    d = {
        "id": r.id,
        "label": r.label,
        "importance": r.importance,
        "pillar": r.pillar,
        "status": s.status,
        "sections": list(r.sections),
        "why_it_matters": r.why,
        "affects": r.affects,
        "document": r.document,
        "document_types": list(r.doc_types),
        "preferred_period": preferred_period(r, prefs),
        "basis_required": basis_text(r, prefs),
        "official_source": r.official_source,
        "alternative": r.alternative,
        "extractor": r.extractor,
        "upload_helps": r.upload_helps,
        "action": s.action,
    }
    if s.ok or s.status == STALE:
        d["found"] = {
            "metric": s.metric,
            "value": None if s.value is None else str(s.value),
            "unit": s.unit,
            "period_end": s.period_end.isoformat() if s.period_end else None,
            "period_type": s.period_type,
            "basis": s.basis,
            "evidence_id": s.evidence_id,
            "document_id": str(s.document_id) if s.document_id else None,
            "page": s.page,
            "provider": s.provider,
            "quality": s.quality,
            "dimension": s.dimension or None,
            "period_note": s.period_note,
        }
    if not s.ok:
        d["reason"] = s.reason
        d["missing_inputs"] = s.missing_inputs
        d["blocked_by"] = s.blocked_by
        d["held_documents"] = s.held_documents
        d["other_basis_available"] = s.other_basis
    return d


# ---------------------------------------------------------------------------- helpers


def fiscal_year(period_end: date) -> str:
    """Indian fiscal year label: year ended 31 March 2026 is FY2026."""
    return f"FY{period_end.year + (1 if period_end.month > 3 else 0)}"


def latest_fiscal_year_end(today: date) -> date:
    year = today.year if today >= date(today.year, 6, 1) else today.year - 1
    return date(year, 3, 31)


def preferred_period(r: rq.Requirement, prefs: pf.Preferences = pf.DEFAULT) -> str:
    if r.period == "PRICE":
        return "the most recent trading day"
    if r.period == "NARRATIVE":
        return "latest annual report"
    if r.period == "QUARTERLY":
        return (
            prefs.period_label() if prefs.period_mode == "QUARTER" else "latest published quarter"
        )
    text = prefs.period_label()
    if prefs.period_mode == "LATEST_QUARTER":
        text += (
            " (the latest annual figure is used, and labelled, only if no recent quarter exists)"
        )
    return text


def basis_text(r: rq.Requirement, prefs: pf.Preferences = pf.DEFAULT) -> str:
    if r.basis == "NA":
        return "not applicable: a bank-level ratio that is not published per basis"
    return f"{prefs.basis.lower()} (your choice; never switched automatically)"


def _age_ok(period_end: date | None, today: date, max_age: int) -> bool:
    return period_end is None or (today - period_end).days <= max_age


ENGINE_AUTHORS = ("calc-engine", "sector-engine")


def _h(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def stage_fingerprints(session: Session, company_id: uuid.UUID) -> dict[str, str]:
    """Hashes of what each kind of work depends on, so a resumed run repeats only what changed.

    Each hash covers INPUTS that the stage itself does not write: documents (for the inventory),
    documents + extraction runs (for extraction), the fact tables (for evidence validation), and
    the validated evidence that is not made by the calculation engines (for calculation, peers and
    scoring). A stage that produces new evidence therefore never invalidates its own fingerprint.
    """
    docs = session.execute(
        select(func.count(), func.max(Document.retrieved_at)).where(
            Document.company_id == company_id
        )
    ).one()
    extraction = session.scalar(
        select(func.max(DocumentExtraction.id))
        .join(Document, Document.id == DocumentExtraction.document_id)
        .where(Document.company_id == company_id)
    )
    price = session.scalar(
        select(func.max(EodPrice.trade_date))
        .join(Security, Security.id == EodPrice.security_id)
        .where(Security.company_id == company_id)
    )
    facts = [
        session.execute(
            select(func.count(), func.coalesce(func.max(model.id), 0)).where(
                model.company_id == company_id
            )
        ).one()
        for model in (FinancialFact, ShareholdingFact, CorporateAction)
    ]
    promoted = session.scalar(
        select(func.count())
        .select_from(ExtractedFact)
        .where(ExtractedFact.company_id == company_id, ExtractedFact.promoted.is_(True))
    )

    def evidence_part(*, synced: bool) -> tuple:
        stmt = select(func.count(), func.coalesce(func.max(EvidenceRecord.id), 0)).where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.created_by.not_in(ENGINE_AUTHORS),
        )
        if not synced:
            stmt = stmt.where(EvidenceRecord.created_by != "evidence-sync")
        return tuple(session.execute(stmt).one())

    docs_fp = _h("docs", *docs)
    extraction_fp = _h(docs_fp, extraction)
    evidence_fp = _h(extraction_fp, price, promoted, *facts, *evidence_part(synced=False))
    source_fp = _h(evidence_fp, *evidence_part(synced=True))
    return {
        "DOCUMENTS": docs_fp,
        "EXTRACTION": extraction_fp,
        "EVIDENCE": evidence_fp,
        "CALCULATION": source_fp,
        "PEERS": source_fp,
        "SCORING": source_fp,
    }


def evidence_fingerprint(session: Session, company_id: uuid.UUID) -> str:
    """One hash of everything above (shown to the user; also used to detect any change)."""
    return _h(*sorted(stage_fingerprints(session, company_id).values()))


# ---------------------------------------------------------------------------- evaluation


class _Store:
    """The company's validated evidence, indexed for the requirement checks."""

    def __init__(self, session: Session, company_id: uuid.UUID, metrics: set[str]) -> None:
        resolutions = list(
            session.scalars(
                select(EvidenceResolution).where(
                    EvidenceResolution.company_id == company_id,
                    EvidenceResolution.selected_evidence_id.is_not(None),
                )
            )
        )
        self.usable = {r.selected_evidence_id: r for r in resolutions if not r.blocked}
        self.blocked = {r.selected_evidence_id: r for r in resolutions if r.blocked}
        self.by_metric: dict[str, list[EvidenceRecord]] = {}
        self.rejected: dict[str, list[EvidenceRecord]] = {}
        for rec in session.scalars(
            select(EvidenceRecord).where(
                EvidenceRecord.company_id == company_id,
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
                EvidenceRecord.metric.in_(metrics),
            )
        ):
            if rec.kind in UNRESOLVED_KINDS or rec.id in self.usable:
                self.by_metric.setdefault(rec.metric, []).append(rec)
            elif rec.id in self.blocked:
                self.rejected.setdefault(rec.metric, []).append(rec)
        self.open_errors = len(self.blocked)
        self._session = session

    def quality(self, rec: EvidenceRecord) -> str | None:
        res = self.usable.get(rec.id)
        return res.quality.value if res is not None else None

    def tier(self, rec: EvidenceRecord) -> int | None:
        """The authority tier behind a record; a calculation takes the weakest input's tier."""
        if rec.authority_tier is not None:
            return rec.authority_tier
        tiers = [
            t
            for (t,) in self._session.execute(
                select(EvidenceRecord.authority_tier)
                .join(EvidenceLink, EvidenceLink.input_id == EvidenceRecord.id)
                .where(EvidenceLink.evidence_id == rec.id)
            )
            if t is not None
        ]
        return max(tiers) if tiers else None

    def has(self, metric: str) -> bool:
        return bool(self.by_metric.get(metric))


def load_store(session: Session, company_id: uuid.UUID, metrics: set[str]) -> "_Store":
    """The company's validated evidence for the given metrics (used by the report builder)."""
    return _Store(session, company_id, metrics)


DIMENSION_ORDER = {
    "": 0,
    "whole_bank": 1,
    "interest_earning_assets": 2,
    "domestic": 3,
    "total_assets": 4,
}
_TYPE_ORDER = {"QUARTERLY": 0, None: 1, "HALF_YEAR": 2, "NINE_MONTH": 2, "ANNUAL": 3}


def _by_type(records: list[EvidenceRecord], *types: str | None) -> list[EvidenceRecord]:
    return [r for r in records if r.period_type in types]


def _scope(
    cands: list[EvidenceRecord],
    req: rq.Requirement,
    prefs: pf.Preferences,
    today: date,
) -> tuple[list[EvidenceRecord], int | None, str | None]:
    """(records in scope, age limit in days or None, note) for the period the user asked for."""
    if req.period in ("NARRATIVE", "PRICE"):
        return cands, (req.max_age_days if req.period == "PRICE" else None), None
    target = prefs.target_end()
    if target is not None:  # a chosen period is exact: no newer or older one is substituted
        if prefs.period_mode == "FISCAL_YEAR":
            pool = (
                _by_type(cands, "ANNUAL")
                if req.period != "LATEST"
                else _by_type(cands, "ANNUAL", None)
            )
        elif req.period == "ANNUAL":
            pool = _by_type(cands, "QUARTERLY")
        else:
            pool = (
                _by_type(cands, "QUARTERLY", None)
                if req.period == "LATEST"
                else _by_type(cands, "QUARTERLY")
            )
        return [r for r in pool if r.period_end == target], None, None
    if req.period == "QUARTERLY":
        return _by_type(cands, "QUARTERLY"), req.max_age_days, None
    if req.period == "LATEST":
        return _by_type(cands, "QUARTERLY", None, "ANNUAL", "HALF_YEAR"), req.max_age_days, None
    # period == ANNUAL: a flow or balance figure that follows the user's period mode
    annual = _by_type(cands, "ANNUAL")
    if prefs.period_mode == "LATEST_ANNUAL":
        return annual, req.max_age_days, None
    quarterly = _by_type(cands, "QUARTERLY")
    if any(_age_ok(r.period_end, today, rq.QUARTER_AGE) for r in quarterly):
        return quarterly, rq.QUARTER_AGE, None
    return (
        annual,
        req.max_age_days,
        f"annual figure used: no quarter within {rq.QUARTER_AGE} days",
    )


def _pick(
    store: _Store, req: rq.Requirement, today: date, prefs: pf.Preferences = pf.DEFAULT
) -> tuple[ReqStatus, str | None]:
    """Evaluate one requirement against the store under the user's preferences."""
    st = ReqStatus(req)
    cands: list[EvidenceRecord] = []
    for metric in req.metrics:
        for rec in store.by_metric.get(metric, []):
            if rec.value is None and not rec.statement_text and rec.kind != "DOCUMENT_SECTION":
                continue
            cands.append(rec)
    if not cands:
        rejected = [r for m in req.metrics for r in store.rejected.get(m, [])]
        if rejected:
            codes = sorted({c for r in rejected for c in (store.blocked[r.id].blocked_by or [])})
            st.blocked_by = codes
            st.reason = "values exist but failed validation and are blocked" + (
                f" ({', '.join(codes)})" if codes else ""
            )
        return st, None

    pool, age_limit, note = _scope(cands, req, prefs, today)
    if not pool:
        newest = max((r.period_end for r in cands if r.period_end), default=None)
        if prefs.strict_period:
            st.reason = f"nothing is stored for the period you chose ({prefs.period_label()})" + (
                f"; the newest stored value is for {newest:%d %b %Y}" if newest else ""
            )
        else:
            st.reason = "no stored value of the kind this requirement needs"
        return st, None

    # The basis is the user's: statement figures on the other basis are never used in its place.
    if req.basis != "NA":
        matching = [r for r in pool if r.basis in (prefs.basis, None)]
        if not matching:
            other = max(pool, key=lambda r: r.period_end.toordinal() if r.period_end else 0)
            other_name = "standalone" if prefs.basis == "CONSOLIDATED" else "consolidated"
            st.other_basis = {
                "basis": other.basis,
                "period_end": other.period_end.isoformat() if other.period_end else None,
            }
            st.reason = (
                f"not available on the {prefs.basis.lower()} basis you chose; {other_name} figures "
                f"exist for {other.period_end:%d %b %Y}. They are not used unless you change the "
                "basis preference"
                if other.period_end
                else f"not available on the {prefs.basis.lower()} basis you chose"
            )
            return st, None
        pool = matching

    def rank(rec: EvidenceRecord):
        return (
            rec.period_end.toordinal() if rec.period_end else 0,
            -DIMENSION_ORDER.get(rec.dimension or "", 9),
            -_TYPE_ORDER.get(rec.period_type, 4),
            -(rec.authority_tier or 9),
        )

    best = max(pool, key=rank)
    st.metric, st.value, st.unit = best.metric, best.value, best.unit
    st.period_end, st.period_type, st.basis = best.period_end, best.period_type, best.basis
    st.evidence_id, st.document_id, st.page = best.id, best.document_id, best.source_page
    st.provider, st.tier, st.quality = best.provider_code, store.tier(best), store.quality(best)
    st.dimension, st.period_note = best.dimension or "", note
    if age_limit is None or _age_ok(best.period_end, today, age_limit):
        st.status = AVAILABLE
    else:
        st.status = STALE
        st.reason = (
            f"newest value is for {best.period_end:%d %b %Y}, older than the "
            f"{age_limit}-day limit for this requirement"
        )
        # The chosen basis stopped reporting (e.g. no consolidated statements since FY2018) while
        # the other basis is current: say so, not that the company has no recent data.
        if req.basis != "NA":
            newer = [
                r
                for r in cands
                if r.basis not in (prefs.basis, None)
                and r.period_end
                and best.period_end
                and r.period_end > best.period_end
                and _age_ok(r.period_end, today, age_limit)
            ]
            if newer:
                other = max(newer, key=lambda r: r.period_end.toordinal())
                other_name = "standalone" if prefs.basis == "CONSOLIDATED" else "consolidated"
                st.other_basis = {"basis": other.basis, "period_end": other.period_end.isoformat()}
                st.reason = (
                    f"the {prefs.basis.lower()} figures were last reported for "
                    f"{best.period_end:%d %b %Y}, older than the {age_limit}-day limit; "
                    f"{other_name} figures exist for {other.period_end:%d %b %Y}. They are not "
                    "used unless you change the basis preference"
                )
    return st, note


def _held_documents(session: Session, company_id: uuid.UUID) -> list[dict]:
    rows = session.execute(
        select(Document, DocumentExtraction.status)
        .outerjoin(
            DocumentExtraction,
            (DocumentExtraction.document_id == Document.id)
            & (DocumentExtraction.superseded_at.is_(None)),
        )
        .where(
            Document.company_id == company_id,
            Document.association_status != "MISMATCH",
            Document.doc_type.in_(HELD_TYPES),
        )
        .order_by(Document.financial_period.desc().nulls_last(), Document.retrieved_at.desc())
    ).all()
    seen: set[uuid.UUID] = set()
    out = []
    for doc, status in rows:
        if doc.id in seen:
            continue
        seen.add(doc.id)
        out.append(
            {
                "document_id": str(doc.id),
                "title": doc.title,
                "doc_type": doc.doc_type,
                "financial_period": doc.financial_period,
                "extraction": status.value if status is not None else "NOT_RUN",
                "retrieved_at": doc.retrieved_at.isoformat() if doc.retrieved_at else None,
            }
        )
    return out


def _decide_action(
    st: ReqStatus, store: _Store, held: list[dict], prefs: pf.Preferences = pf.DEFAULT
) -> None:
    req = st.req
    if st.ok:
        st.action = "NONE"
        return
    if st.other_basis:  # exists, but on the basis the user did not choose: their decision
        st.action = "CHANGE_BASIS_OR_UPLOAD"
        return
    if req.extractor == rq.MARKET_DATA:
        st.action = "REFRESH_MARKET_DATA"
        st.reason = st.reason or "no recent end-of-day price is stored"
        return
    if req.extractor == rq.NONE:
        st.action = "NOT_RESOLVABLE_BY_UPLOAD"
        st.reason = st.reason or (
            "EquityOS has no extractor for this figure yet: a document would be stored but "
            "the figure would not be read from it. It stays disclosed as unavailable."
        )
        return
    if req.extractor == rq.DERIVED:
        st.missing_inputs = [m for m in req.inputs if not store.has(m)]
        if st.missing_inputs:
            names = ", ".join(st.missing_inputs)
            st.reason = st.reason or f"cannot be calculated: missing input(s) {names}"
            st.action = "UPLOAD"
        else:
            st.action = "RECALCULATE"
            st.reason = st.reason or "inputs are present but the calculation has not produced it"
    else:
        st.action = "UPLOAD"
        st.reason = st.reason or "no validated value is stored for this figure"
    relevant = [d for d in held if d["doc_type"] in req.doc_types]
    st.held_documents = relevant[:6]
    if relevant and any(d["extraction"] == "NOT_RUN" for d in relevant):
        st.action = "EXTRACT_HELD"
        pending = "a stored document of the right type has not been processed yet"
        # keep a specific reason (a stale value, a blocked one) and add to it, never replace it
        st.reason = f"{st.reason}; {pending}" if st.status == STALE and st.reason else pending
    elif relevant and st.action == "UPLOAD":
        st.reason += (
            "; EquityOS already holds a matching document type but the figure was not found in it"
        )


def _component(statuses: list[ReqStatus]) -> float | None:
    if not statuses:
        return None
    total = sum(rq.IMPORTANCE_WEIGHT[s.req.importance] for s in statuses)
    got = sum(rq.IMPORTANCE_WEIGHT[s.req.importance] * STATUS_VALUE[s.status] for s in statuses)
    return round(100 * got / total, 1)


def _sections(statuses: list[ReqStatus]) -> list[dict]:
    out = []
    for key, title in rq.SECTIONS:
        essential = [s for s in statuses if key in s.req.sections]
        if not essential:
            out.append(
                {"key": key, "title": title, "status": "DERIVED", "missing": [], "essential": 0}
            )
            continue
        ok = [s for s in essential if s.ok]
        crit_gap = [s for s in essential if not s.ok and s.req.importance == rq.CRITICAL]
        if len(ok) == len(essential):
            status = "COMPLETE"
        elif not ok:
            status = "UNAVAILABLE"
        else:
            status = "PARTIAL"
        out.append(
            {
                "key": key,
                "title": title,
                "status": status,
                "essential": len(essential),
                "available": len(ok),
                "critical_missing": [s.req.id for s in crit_gap],
                "missing": [s.req.id for s in essential if not s.ok],
            }
        )
    return out


def evaluate(
    session: Session,
    company: Company,
    *,
    today: date | None = None,
    prefs: pf.Preferences | None = None,
) -> Readiness:
    today = today or clock.today()
    prefs = prefs or pf.DEFAULT
    sector = company.sector.code if company.sector else None
    industry = company.industry.code if company.industry else None
    profile = rq.profile_for(sector, industry)
    reqs = [
        r
        for r in rq.requirements_for(profile)
        if prefs.include_optional or r.importance != rq.OPTIONAL
    ]
    th = rq.thresholds_for(profile)

    needed = {m for r in reqs for m in (*r.metrics, *r.inputs)}
    store = _Store(session, company.id, needed)
    held = _held_documents(session, company.id)

    statuses = []
    notes: list[str] = []
    fallbacks: list[str] = []
    for req in reqs:
        st, note = _pick(store, req, today, prefs)
        _decide_action(st, store, held, prefs)
        statuses.append(st)
        if note:
            fallbacks.append(req.label)

    fin = [s for s in statuses if s.req.pillar in (rq.FINANCIALS, rq.NARRATIVE)]
    # Only requirements that matter to the conclusion AND that a document could settle count
    # here: a sector figure that is optional, or that no extractor can read yet (attrition,
    # volumes), is disclosed as a gap but must not hold a company back where the user cannot fix
    # it. A profile with only such figures has no sector component at all.
    sector_reqs = [
        s
        for s in statuses
        if (s.req.pillar == rq.SECTOR or s.req.id.startswith("segment_"))
        and s.req.importance != rq.OPTIONAL
        and s.req.upload_helps  # a figure nobody can supply cannot hold the research back
    ]
    val = [s for s in statuses if s.req.pillar in (rq.VALUATION, rq.MARKET)]
    scored = [s for s in statuses if s.ok]

    components: dict[str, dict] = {}

    def put(name: str, value: float | None, detail: str) -> None:
        components[name] = {
            "label": COMPONENT_LABELS[name],
            "score": value,
            "applicable": value is not None,
            "weight": th.weights[name],
            "detail": detail,
        }

    put(
        "financial_completeness",
        _component(fin),
        f"{sum(1 for s in fin if s.ok)} of {len(fin)} financial requirements available",
    )
    put("sector_metrics", _component(sector_reqs), _count_text(sector_reqs, "sector-specific"))
    # An IMPORTANT sector figure (vehicle volumes, attrition) lowers the score but cannot hold the
    # research back by itself: only a component that carries a CRITICAL requirement can.
    components["sector_metrics"]["gating"] = any(
        s.req.importance == rq.CRITICAL for s in sector_reqs
    )
    if not sector_reqs:
        components["sector_metrics"]["detail"] = (
            "this kind of company has no critical or important sector-specific requirement"
        )
    put("valuation_inputs", _component(val), _count_text(val, "valuation"))

    # document coverage: what the validated figures were actually read from
    doc_ids = {s.document_id for s in scored if s.document_id}
    core = [s for s in statuses if s.req.id in ("revenue", "total_income", "net_profit") and s.ok]
    annual_from_doc = any(s.document_id for s in core)
    q = by_id(statuses, "latest_quarter")
    quarter_from_doc = bool(q and q.ok and q.document_id)
    narrative = any(s.ok for s in statuses if s.req.pillar == rq.NARRATIVE and s.document_id)
    coverage = 60 * annual_from_doc + 25 * quarter_from_doc + 15 * narrative
    put(
        "document_coverage",
        float(coverage),
        f"annual figures {'from a filing' if annual_from_doc else 'not from a filing'}; "
        f"latest quarter {'from a filing' if quarter_from_doc else 'not from a filing'}; "
        f"{len(doc_ids)} source document(s) behind the available figures",
    )

    # source reliability: authority of the evidence behind the figures that exist
    tiers = [TIER_SCORE.get(s.tier or 0, 50) for s in scored if s.req.pillar != rq.NARRATIVE]
    put(
        "source_reliability",
        round(sum(tiers) / len(tiers), 1) if tiers else None,
        "average authority of the sources behind the available figures"
        if tiers
        else "no figures available",
    )

    # period consistency: annual figures should describe the same year on the same basis
    annual = [s for s in scored if s.period_type == "ANNUAL" and s.period_end]
    if annual:
        periods = Counter(s.period_end for s in annual)
        bases = Counter(s.basis for s in annual)
        top_p, n_p = periods.most_common(1)[0]
        n_b = bases.most_common(1)[0][1]
        consistency = round(100 * (n_p / len(annual)) * 0.7 + 100 * (n_b / len(annual)) * 0.3, 1)
        detail = f"{n_p} of {len(annual)} annual figures are for {fiscal_year(top_p)}"
    else:
        consistency, detail = None, "no annual figures available"
    put("period_consistency", consistency, detail)

    # validation coverage: quality of the validated figures, reduced by open errors
    qs = [QUALITY_SCORE.get(s.quality or "", 0.6) for s in scored if s.req.pillar != rq.NARRATIVE]
    if qs:
        base = 100 * sum(qs) / len(qs)
        v = round(max(0.0, base * (1 - 0.1 * store.open_errors)), 1)
        detail = f"{store.open_errors} open validation error(s)"
    else:
        v, detail = None, "no figures to validate"
    put("validation_coverage", v, detail)

    live = {k: c for k, c in components.items() if c["applicable"]}
    weight = sum(c["weight"] for c in live.values())
    score = (
        round(sum(c["score"] * c["weight"] for c in live.values()) / weight, 1) if weight else 0.0
    )

    crit_missing = [s for s in statuses if s.req.importance == rq.CRITICAL and not s.ok]
    reasons: list[str] = []
    revenue = by_id(statuses, "revenue") or by_id(statuses, "total_income")
    profit = by_id(statuses, "net_profit")
    only_other_basis = [x for x in (revenue, profit) if x and x.other_basis]
    if not (revenue and revenue.ok) and not (profit and profit.ok) and only_other_basis:
        # Figures exist, but only on the basis the user did not choose: that is the user's decision
        # to make, not a company without data.
        other = "standalone" if prefs.basis == "CONSOLIDATED" else "consolidated"
        eligibility = ASSISTED
        reasons.append(
            f"revenue and profit exist only on the {other} basis, not on the "
            f"{prefs.basis.lower()} basis you chose; change the basis preference to use them"
        )
        if crit_missing:
            reasons.append(
                f"{len(crit_missing)} critical item(s) missing or stale: "
                + ", ".join(x.req.label for x in crit_missing)
            )
    elif not (revenue and revenue.ok) and not (profit and profit.ok):
        eligibility = BLOCKED
        reasons.append("no validated revenue or profit is stored for this company")
    else:
        eligibility = AUTOMATIC
        if crit_missing:
            eligibility = ASSISTED
            names = ", ".join(s.req.label for s in crit_missing)
            reasons.append(f"{len(crit_missing)} critical item(s) missing or stale: {names}")
        if score < th.min_score:
            eligibility = ASSISTED
            reasons.append(f"readiness score {score} is below the {th.min_score} minimum")
        weak = [
            c["label"]
            for c in live.values()
            if c["score"] is not None and c["score"] < th.min_component and c.get("gating", True)
        ]
        if weak:
            eligibility = ASSISTED
            reasons.append(f"component(s) below the {th.min_component} minimum: {', '.join(weak)}")
        if store.open_errors > th.max_open_errors:
            eligibility = ASSISTED
            reasons.append(f"{store.open_errors} unresolved validation error(s) on the evidence")
    if eligibility == AUTOMATIC:
        reasons.append("every critical requirement is met and every readiness threshold passes")
    reasons.extend(notes)
    if fallbacks:
        reasons.append(
            f"{len(fallbacks)} figure(s) use the latest ANNUAL period because no quarter within "
            f"{rq.QUARTER_AGE} days is stored for them: {', '.join(fallbacks)}"
        )
    if prefs.basis != "CONSOLIDATED" or prefs.period_mode != "LATEST_QUARTER":
        reasons.append(
            f"researched on your chosen {prefs.basis.lower()} basis and period: "
            f"{prefs.period_label()}"
        )

    vc = components["validation_coverage"]["score"] or 0
    if eligibility == BLOCKED:
        confidence = "NONE"
    elif score >= 85 and vc >= 80 and not crit_missing:
        confidence = "HIGH"
    elif score >= 65 and not crit_missing:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return Readiness(
        profile=profile,
        profile_label=rq.PROFILE_DEFS[profile][0],
        as_of=today,
        requirement_version=rq.VERSION,
        statuses=statuses,
        components=components,
        score=score,
        eligibility=eligibility,
        confidence=confidence,
        reasons=reasons,
        open_errors=store.open_errors,
        sections=_sections(statuses),
        held=held,
        preferences=prefs,
        thresholds={
            "min_score": th.min_score,
            "min_component": th.min_component,
            "max_open_errors": th.max_open_errors,
            "weights": th.weights,
        },
    )


def by_id(statuses: list[ReqStatus], rid: str) -> ReqStatus | None:
    return next((s for s in statuses if s.req.id == rid), None)


def _count_text(statuses: list[ReqStatus], what: str) -> str:
    if not statuses:
        return f"no {what} requirements apply to this kind of company"
    return f"{sum(1 for s in statuses if s.ok)} of {len(statuses)} {what} requirements available"


__all__ = [
    "ASSISTED",
    "AUTOMATIC",
    "AVAILABLE",
    "BLOCKED",
    "MISSING",
    "STALE",
    "Readiness",
    "ReqStatus",
    "evaluate",
    "load_store",
    "evidence_fingerprint",
    "fiscal_year",
]
