"""Research report assembly: the same structure whether the evidence is complete or not.

Two modes share one layout, so a reader can always see what is there and what is not:

* AVAILABLE_EVIDENCE  built at once from validated evidence; every gap is listed in the section it
                      affects and in the limitations block, and no conclusion is drawn from a
                      missing figure.
* COMPLETE            allowed only when every CRITICAL requirement is met, the readiness thresholds
                      pass and no validation error is open. Otherwise it is refused, with the
                      reasons, rather than produced with a "complete" label it has not earned.

Every number carries the evidence record, document and page it came from. Nothing is calculated or
scored here: figures are read from the evidence store, the score from the stored score run, peers
from the stored peer set. Narrative sections are quoted excerpts with their page, never summarised
by a model.
"""

import hashlib
import json
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.calc.adjust import EXPLAINED_NOTE
from app.models import (
    AdjustmentOrigin,
    AdjustmentStatus,
    Company,
    DocumentSection,
    EvidenceRecord,
    Peer,
    PeerSet,
    PriceAdjustment,
    ResearchReport,
    ResearchSession,
    ScoreRun,
    Security,
)
from app.research import clock
from app.research import preferences as pf
from app.research import readiness as rd
from app.research import requirements as rq

AVAILABLE_EVIDENCE, COMPLETE = "AVAILABLE_EVIDENCE", "COMPLETE"
MODES = (AVAILABLE_EVIDENCE, COMPLETE)
EXCERPT_CHARS = 1500
SERIES_METRICS = {
    "revenue": ("INCOME.revenue_from_operations", "INCOME.total_income"),
    "net_profit": ("INCOME.net_profit", "INCOME.profit_after_tax", "INCOME.attributable_profit"),
    "total_assets": ("BALANCE_SHEET.total_assets",),
    "total_equity": ("BALANCE_SHEET.total_equity",),
}


class ReportNotAllowed(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _item(s: rd.ReqStatus) -> dict:
    return {
        "requirement": s.req.id,
        "label": s.req.label,
        "importance": s.req.importance,
        "value": None if s.value is None else str(s.value),
        "unit": s.unit,
        "period_end": s.period_end.isoformat() if s.period_end else None,
        "period_type": s.period_type,
        "basis": s.basis,
        "dimension": s.dimension or None,
        "period_note": s.period_note,
        "source": {
            "evidence_id": s.evidence_id,
            "document_id": str(s.document_id) if s.document_id else None,
            "page": s.page,
            "provider": s.provider,
            "quality": s.quality,
        },
    }


def _gap(s: rd.ReqStatus) -> dict:
    return {
        "requirement": s.req.id,
        "label": s.req.label,
        "importance": s.req.importance,
        "status": s.status,
        "reason": s.reason,
        "missing_inputs": s.missing_inputs,
        "action": s.action,
        "affects": s.req.affects,
        "other_basis_available": s.other_basis,
    }


def _series(store: rd._Store, metrics: tuple[str, ...], prefer: str) -> list[dict]:
    """Annual values, one per year (preferred basis), oldest first."""
    best: dict[date, EvidenceRecord] = {}
    for m in metrics:
        for r in store.by_metric.get(m, []):
            if r.period_type != "ANNUAL" or r.value is None or r.period_end is None:
                continue
            cur = best.get(r.period_end)
            if cur is None or (r.basis == prefer and cur.basis != prefer):
                best[r.period_end] = r
    return [
        {
            "period_end": d.isoformat(),
            "fiscal_year": rd.fiscal_year(d),
            "value": str(r.value),
            "unit": r.unit,
            "basis": r.basis,
            "source": {
                "evidence_id": r.id,
                "document_id": str(r.document_id) if r.document_id else None,
                "page": r.source_page,
            },
        }
        for d, r in sorted(best.items())
    ]


def _segments(store: rd._Store) -> list[dict]:
    rows = []
    for metric in (
        "SEGMENT.segment_revenue",
        "SEGMENT.segment_result",
        "SEGMENT.segment_result_ebitda",
    ):
        recs = [r for r in store.by_metric.get(metric, []) if r.value is not None and r.period_end]
        if not recs:
            continue
        newest = max(r.period_end for r in recs)
        for r in sorted((r for r in recs if r.period_end == newest), key=lambda r: r.dimension):
            rows.append(
                {
                    "metric": metric,
                    "segment": r.dimension or None,
                    "period_end": newest.isoformat(),
                    "period_type": r.period_type,
                    "basis": r.basis,
                    "value": str(r.value),
                    "unit": r.unit,
                    "source": {
                        "evidence_id": r.id,
                        "document_id": str(r.document_id) if r.document_id else None,
                        "page": r.source_page,
                    },
                }
            )
    return rows[:60]


def _narrative(session: Session, store: rd._Store, metric: str) -> dict | None:
    recs = store.by_metric.get(metric, [])
    if not recs:
        return None
    rec = max(recs, key=lambda r: r.retrieved_at or datetime.min.replace(tzinfo=UTC))
    section = session.get(DocumentSection, (rec.details or {}).get("section_id") or -1)
    return {
        "heading": rec.statement_text,
        "excerpt": (section.text[:EXCERPT_CHARS] if section else None),
        "flags": (rec.details or {}).get("flags"),
        "source": {
            "evidence_id": rec.id,
            "document_id": str(rec.document_id) if rec.document_id else None,
            "page": rec.source_page,
        },
        "note": "quoted from the document; not summarised",
    }


def _price_notes(session: Session, company_id: uuid.UUID) -> list[dict]:
    """Price-history events that look like an unadjusted split/bonus but are not confirmed.

    They are listed, never applied: any multi-year price return, drawdown or moving average that
    spans one of these dates would be wrong until a corporate action confirms or refutes it.
    """
    rows = session.execute(
        select(PriceAdjustment, Security.symbol)
        .join(Security, Security.id == PriceAdjustment.security_id)
        .where(Security.company_id == company_id, PriceAdjustment.confidence == "PRICE_GAP_ONLY")
        .order_by(PriceAdjustment.ex_date)
    ).all()
    rows = [(a, sym) for a, sym in rows if not a.detail.endswith(EXPLAINED_NOTE)]
    return [
        {
            "symbol": symbol,
            "date": a.ex_date.isoformat(),
            "factor": str(a.factor),
            "note": a.detail,
        }
        for a, symbol in rows
    ]


def _applied_adjustments(session: Session, company_id: uuid.UUID) -> list[dict]:
    """Corporate-action adjustments applied to the price series, with their source."""
    rows = session.execute(
        select(PriceAdjustment, Security.symbol)
        .join(Security, Security.id == PriceAdjustment.security_id)
        .where(
            Security.company_id == company_id,
            PriceAdjustment.status == AdjustmentStatus.APPLIED,
            PriceAdjustment.origin == AdjustmentOrigin.CORPORATE_ACTION,
        )
        .order_by(PriceAdjustment.ex_date)
    ).all()
    return [
        {
            "symbol": symbol,
            "ex_date": a.ex_date.isoformat(),
            "ratio": a.ratio_text,
            "factor": str(a.factor),
            "source": a.source,
        }
        for a, symbol in rows
    ]


def _score(session: Session, company_id: uuid.UUID) -> dict:
    run = session.scalar(
        select(ScoreRun)
        .where(ScoreRun.company_id == company_id)
        .order_by(ScoreRun.calculated_at.desc())
        .limit(1)
    )
    if run is None:
        return {"available": False, "reason": "no score has been calculated for this company"}
    return {
        "available": run.overall is not None,
        "run_id": run.id,
        "methodology": run.methodology,
        "overall": None if run.overall is None else f"{run.overall:.2f}",
        "confidence": run.confidence,
        "pillar_scores": run.pillar_scores,
        "reason": run.unavailable_reason,
        "peer_count": run.peer_count,
        "eligible_peer_count": run.eligible_peer_count,
        "as_of": run.as_of.isoformat(),
        "note": "financial score from the scoring engine; unrelated to research readiness",
    }


def _peers(session: Session, company_id: uuid.UUID) -> dict:
    ps = session.scalar(
        select(PeerSet)
        .where(PeerSet.company_id == company_id)
        .order_by(PeerSet.built_at.desc())
        .limit(1)
    )
    if ps is None:
        return {"available": False, "reason": "no peer set has been built"}
    rows = session.execute(
        select(Peer, Company.common_name)
        .join(Company, Company.id == Peer.company_id)
        .where(Peer.peer_set_id == ps.id)
        .order_by(Peer.rank)
    ).all()
    return {
        "available": True,
        "as_of": ps.as_of.isoformat(),
        "thin": ps.thin,
        "peer_count": ps.peer_count,
        "criteria": ps.criteria,
        "peers": [
            {"name": name, "tier": p.tier, "rank": p.rank, "reason": p.reason}
            for p, name in rows[:12]
        ],
        "note": "a thin group means fewer peers than the minimum for a reliable comparison"
        if ps.thin
        else None,
    }


def build_content(
    session: Session, company: Company, ready: rd.Readiness, mode: str, as_of: date
) -> dict:
    statuses = ready.statuses
    needed = {m for s in statuses for m in s.req.metrics}
    needed |= {m for ms in SERIES_METRICS.values() for m in ms}
    needed |= {"SEGMENT.segment_revenue", "SEGMENT.segment_result", "SEGMENT.segment_result_ebitda"}
    store = rd.load_store(session, company.id, needed)
    prefer = ready.preferences.basis
    sec_status = {s["key"]: s for s in ready.sections}

    sections = []
    for key, title in rq.SECTIONS:
        info = sec_status[key]
        mine = [s for s in statuses if key in s.req.sections]
        body: dict = {
            "key": key,
            "title": title,
            "status": info["status"],
            "items": [_item(s) for s in mine if s.ok],
            "gaps": [_gap(s) for s in mine if not s.ok],
        }
        if key == "overview":
            body["company"] = {
                "name": company.common_name,
                "legal_name": company.legal_name,
                "sector": company.sector.code if company.sector else None,
                "industry": company.industry.code if company.industry else None,
                "symbols": [s.symbol for s in company.securities],
                "isins": [s.isin for s in company.securities],
                "profile": ready.profile_label,
            }
        elif key == "historical":
            body["series"] = {
                name: _series(store, metrics, prefer) for name, metrics in SERIES_METRICS.items()
            }
            body["price_notes"] = _price_notes(session, company.id)
        elif key == "segments":
            body["segments"] = _segments(store)
            if not body["segments"]:
                body["status"] = "UNAVAILABLE" if mine else "NOT_REPORTED"
        elif key == "peer_comparison":
            body["peers"] = _peers(session, company.id)
        elif key == "risks_management":
            body["management_discussion"] = _narrative(
                session, store, "SECTION.management_discussion"
            )
            body["auditor_report"] = _narrative(session, store, "SECTION.auditor_report")
        elif key == "scorecard":
            body["score"] = _score(session, company.id)
        sections.append(body)

    missing = {
        i: [_gap(s) for s in statuses if s.req.importance == i and not s.ok]
        for i in rq.IMPORTANCE_ORDER
    }
    derived_missing = [_gap(s) for s in statuses if not s.ok and s.req.extractor == rq.DERIVED]
    valuation_gaps = [
        _gap(s) for s in statuses if not s.ok and s.req.pillar in (rq.VALUATION, rq.MARKET)
    ]
    score = _score(session, company.id)
    conclusions = [
        f"No conclusion is drawn on {g['label'].lower()}: {g['affects']} cannot rest on it."
        for g in (*missing[rq.CRITICAL], *missing[rq.IMPORTANT])
    ]
    notes = [r for r in ready.reasons if "standalone figures only" in r]
    banner = (
        "Complete research report: every critical requirement is met by validated evidence."
        if mode == COMPLETE
        else f"Available-evidence report: {len(missing[rq.CRITICAL])} critical and "
        f"{len(missing[rq.IMPORTANT])} important item(s) are not available. Gaps are listed "
        "in each section and under Limitations; no figure has been estimated or assumed."
    )
    return {
        "meta": {
            "company_id": str(company.id),
            "company": company.common_name,
            "mode": mode,
            "as_of": as_of.isoformat(),
            "banner": banner,
            "profile": ready.profile,
            "requirement_version": ready.requirement_version,
            "preferences": {
                **ready.preferences.to_dict(),
                "period_label": ready.preferences.period_label(),
            },
            "complete": mode == COMPLETE and not missing[rq.CRITICAL],
        },
        "readiness": {
            "score": ready.score,
            "eligibility": ready.eligibility,
            "confidence": ready.confidence,
            "components": {
                k: {"label": c["label"], "score": c["score"], "detail": c["detail"]}
                for k, c in ready.components.items()
            },
            "note": "research readiness measures the evidence, not the company",
        },
        "sections": sections,
        "limitations": {
            "missing_critical": missing[rq.CRITICAL],
            "missing_important": missing[rq.IMPORTANT],
            "missing_optional": missing[rq.OPTIONAL],
            "unavailable_calculations": derived_missing,
            "incomplete_valuation": valuation_gaps,
            "unavailable_scores": None if score.get("available") else score.get("reason"),
            "limits_on_conclusions": conclusions,
            "basis_notes": notes,
            "applied_price_adjustments": _applied_adjustments(session, company.id),
            "price_history_notes": next(
                (s["price_notes"] for s in sections if s["key"] == "historical"), []
            ),
        },
    }


def _hash(content: dict) -> str:
    stable = json.loads(json.dumps(content, default=str))
    stable["meta"].pop("as_of", None)
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def generate(
    session: Session,
    sess: ResearchSession,
    mode: str,
    created_by: str,
    *,
    today: date | None = None,
) -> ResearchReport:
    if mode not in MODES:
        raise ValueError(f"unknown report mode {mode!r}")
    today = today or clock.today()
    company = session.get(Company, sess.company_id)
    prefs = pf.Preferences.from_dict(sess.preferences or {})
    ready = rd.evaluate(session, company, today=today, prefs=prefs)

    if ready.eligibility == rd.BLOCKED:
        raise ReportNotAllowed(["no report can be written: " + "; ".join(ready.reasons)])
    if mode == COMPLETE and ready.eligibility != rd.AUTOMATIC:
        raise ReportNotAllowed(
            ["a complete report needs every critical requirement met:", *ready.reasons]
        )

    content = build_content(session, company, ready, mode, today)
    digest = _hash(content)
    latest = session.scalar(
        select(ResearchReport)
        .where(ResearchReport.session_id == sess.id)
        .order_by(ResearchReport.version.desc())
        .limit(1)
    )
    if latest is not None and latest.content_hash == digest and latest.mode == mode:
        return latest  # nothing changed since the last version: no duplicate version
    version = (
        session.scalar(
            select(func.max(ResearchReport.version)).where(ResearchReport.session_id == sess.id)
        )
        or 0
    ) + 1
    report = ResearchReport(
        session_id=sess.id,
        company_id=company.id,
        version=version,
        mode=mode,
        readiness_score=ready.score,
        missing_critical=len(content["limitations"]["missing_critical"]),
        missing_important=len(content["limitations"]["missing_important"]),
        content=content,
        content_hash=digest,
        created_by=created_by,
    )
    session.add(report)
    session.flush()
    return report
