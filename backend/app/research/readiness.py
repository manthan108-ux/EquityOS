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
            "requirements": [status_dict(s) for s in self.statuses],
            "missing": {
                "CRITICAL": [status_dict(s) for s in crit],
                "IMPORTANT": [status_dict(s) for s in imp],
                "OPTIONAL": [status_dict(s) for s in opt],
            },
            "sections": self.sections,
            "documents_held": self.held,
        }


def status_dict(s: ReqStatus) -> dict:
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
        "preferred_period": preferred_period(r),
        "basis_required": basis_text(r),
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
        }
    if not s.ok:
        d["reason"] = s.reason
        d["missing_inputs"] = s.missing_inputs
        d["blocked_by"] = s.blocked_by
        d["held_documents"] = s.held_documents
    return d


# ---------------------------------------------------------------------------- helpers


def fiscal_year(period_end: date) -> str:
    """Indian fiscal year label: year ended 31 March 2026 is FY2026."""
    return f"FY{period_end.year + (1 if period_end.month > 3 else 0)}"


def latest_fiscal_year_end(today: date) -> date:
    year = today.year if today >= date(today.year, 6, 1) else today.year - 1
    return date(year, 3, 31)


def preferred_period(r: rq.Requirement) -> str:
    today = clock.today()
    if r.period == "ANNUAL":
        end = latest_fiscal_year_end(today)
        return f"{fiscal_year(end)} (year ended 31 Mar {end.year}), annual"
    if r.period in ("QUARTERLY", "LATEST"):
        return "latest published quarter (or the latest year if no quarter is published yet)"
    if r.period == "PRICE":
        return "the most recent trading day"
    return "latest annual report"


def basis_text(r: rq.Requirement) -> str:
    if r.basis == "NA":
        return "not applicable"
    if r.basis == "STANDALONE_OK":
        return "standalone (as banks report it); consolidated is used only if the peer set uses it"
    return "consolidated; standalone only if the company publishes no consolidated statements"


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


def _pick(store: _Store, req: rq.Requirement, today: date) -> tuple[ReqStatus, str | None]:
    """Evaluate one requirement against the store (no inputs/actions yet)."""
    st = ReqStatus(req)
    wanted_type = {"ANNUAL": "ANNUAL", "QUARTERLY": "QUARTERLY"}.get(req.period)
    candidates: list[EvidenceRecord] = []
    for metric in req.metrics:
        for rec in store.by_metric.get(metric, []):
            if wanted_type and rec.period_type != wanted_type:
                continue
            if rec.value is None and not rec.statement_text and rec.kind != "DOCUMENT_SECTION":
                continue
            candidates.append(rec)
    if not candidates:
        rejected = [r for m in req.metrics for r in store.rejected.get(m, [])]
        if rejected:
            codes = sorted({c for r in rejected for c in (store.blocked[r.id].blocked_by or [])})
            st.blocked_by = codes
            st.reason = "values exist but failed validation and are blocked" + (
                f" ({', '.join(codes)})" if codes else ""
            )
        return st, None
    # newest period first; then the preferred basis (consolidated unless standalone is accepted)
    prefer = "STANDALONE" if req.basis == "STANDALONE_OK" else "CONSOLIDATED"

    def rank(rec: EvidenceRecord):
        return (
            rec.period_end.toordinal() if rec.period_end else 0,
            1 if (rec.basis or prefer) == prefer else 0,
            -(rec.authority_tier or 9),
        )

    best = max(candidates, key=rank)
    st.metric, st.value, st.unit = best.metric, best.value, best.unit
    st.period_end, st.period_type, st.basis = best.period_end, best.period_type, best.basis
    st.evidence_id, st.document_id, st.page = best.id, best.document_id, best.source_page
    st.provider, st.tier, st.quality = best.provider_code, store.tier(best), store.quality(best)
    if req.period == "NARRATIVE" or _age_ok(best.period_end, today, req.max_age_days):
        st.status = AVAILABLE
    else:
        st.status = STALE
        st.reason = (
            f"newest value is for {best.period_end:%d %b %Y}, older than the "
            f"{req.max_age_days}-day limit for this requirement"
        )
    note = None
    if (
        req.basis == "CONSOLIDATED_PREFERRED"
        and best.basis == "STANDALONE"
        and st.status == AVAILABLE
    ):
        note = "standalone figures only"
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


def _decide_action(st: ReqStatus, store: _Store, held: list[dict]) -> None:
    req = st.req
    if st.ok:
        st.action = "NONE"
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
        st.reason = "a stored document of the right type has not been processed yet"
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


def evaluate(session: Session, company: Company, *, today: date | None = None) -> Readiness:
    today = today or clock.today()
    sector = company.sector.code if company.sector else None
    industry = company.industry.code if company.industry else None
    profile = rq.profile_for(sector, industry)
    reqs = rq.requirements_for(profile)
    th = rq.thresholds_for(profile)

    needed = {m for r in reqs for m in (*r.metrics, *r.inputs)}
    store = _Store(session, company.id, needed)
    held = _held_documents(session, company.id)

    statuses = []
    notes: list[str] = []
    for req in reqs:
        st, note = _pick(store, req, today)
        _decide_action(st, store, held)
        statuses.append(st)
        if note:
            notes.append(f"{req.label}: {note}")

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
    if not (revenue and revenue.ok) and not (profit and profit.ok):
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
            if c["score"] is not None and c["score"] < th.min_component
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
