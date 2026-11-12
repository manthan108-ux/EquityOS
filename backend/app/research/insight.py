"""Assembles the analytical layer of a report: trends, peers, valuation, commentary, thesis.

Called by `report.build_content`. It only reads validated evidence; a failure here must never stop a
report, so the caller records the failure and the report says the analysis is unavailable.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Company
from app.research import analysis as an
from app.research import cyclicality, industry, mgmt, profile, thesis, valuation, verdict
from app.research import readiness as rd
from app.research.sectors import sector_for

VERSION = "1.0"
VALUATION_NAMES = {
    "PRICE.close",
    "VALUATION.pe_annual",
    "VALUATION.pe_ttm",
    "VALUATION.price_to_book",
    "INCOME.eps_basic",
    "TECH.return_1y",
    "TECH.return_6m",
    "PEERMEDIAN.TECH.return_1y",
    "PEERMEDIAN.TECH.return_6m",
}
GROWTH_NAMES = {
    "INCOME.revenue_from_operations",
    "INCOME.total_income",
    "INCOME.net_profit",
    "INCOME.attributable_profit",
    "INCOME.profit_after_tax",
}


def build(
    session: Session,
    company: Company,
    ready: rd.Readiness,
    *,
    score: dict,
    missing: list[str],
    applied: list[dict],
    auditor_flags,
) -> dict:
    sector = sector_for(ready.profile)
    prefer = ready.preferences.basis
    names = (
        {n for m in (*sector.trend, *sector.quarterly, *sector.peer) for n in m.names}
        | VALUATION_NAMES
        | GROWTH_NAMES
        | profile.SEGMENT_NAMES
    )
    own_store = rd.load_store(session, company.id, names)

    def store_for(cid, wanted):
        # peers are loaded with every name the analysis needs (growth comparison included)
        return own_store if cid == company.id else rd.load_store(session, cid, set(wanted) | names)

    trend = an.trends(own_store, sector, prefer)
    peers, _, peer_stores = an.peer_analysis(session, company, store_for, sector, prefer)
    trend_by_key = {r["key"]: r for r in trend["annual"]}
    val = valuation.build(
        session, company.id, own_store, sector, prefer, peers["rows"], trend_by_key
    )
    commentary = mgmt.commentary(session, company.id)
    peer_names = [p["name"] for p in peers["peers"]]
    ind = industry.build(session, company, own_store, peer_stores, sector.code, prefer, peer_names)
    prof = profile.build(session, company.id, own_store, prefer)
    result = {
        "version": VERSION,
        "available": True,
        "sector": {
            "code": sector.code,
            "label": sector.label,
            "lens": sector.lens,
            "notes": list(sector.notes),
            "cash_flow_meaningful": sector.cash_flow_meaningful,
        },
        "basis": prefer,
        "trend": trend,
        "peers": peers,
        "valuation": val,
        "management": commentary,
        "profile": prof,
        "industry": ind["overview"],
        "growth": ind["growth"],
    }
    monitoring = []  # filled once the thesis exists; cyclicality quotes the stored indicators
    result["thesis"] = thesis.build(
        result,
        sector,
        score=score,
        missing=missing,
        mgmt=commentary,
        applied=applied,
        auditor_flags=auditor_flags,
    )
    result["cyclicality"] = cyclicality.build(
        trend, sector.code, ind["growth"], result["thesis"].get("monitoring", monitoring)
    )
    result["verdict"] = verdict.build(
        session, company.id, result, score=score, readiness_score=ready.score
    )
    return result
