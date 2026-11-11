"""Assembles the analytical layer of a report: trends, peers, valuation, commentary, thesis.

Called by `report.build_content`. It only reads validated evidence; a failure here must never stop a
report, so the caller records the failure and the report says the analysis is unavailable.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Company
from app.research import analysis as an
from app.research import mgmt, thesis, valuation
from app.research import readiness as rd
from app.research.sectors import sector_for

VERSION = "1.0"
VALUATION_NAMES = {
    "PRICE.close",
    "VALUATION.pe_annual",
    "VALUATION.pe_ttm",
    "VALUATION.price_to_book",
    "INCOME.eps_basic",
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
    names = {
        n for m in (*sector.trend, *sector.quarterly, *sector.peer) for n in m.names
    } | VALUATION_NAMES
    own_store = rd.load_store(session, company.id, names)

    def store_for(cid, wanted):
        return own_store if cid == company.id else rd.load_store(session, cid, set(wanted))

    trend = an.trends(own_store, sector, prefer)
    peers, _, _peer_stores = an.peer_analysis(session, company, store_for, sector, prefer)
    trend_by_key = {r["key"]: r for r in trend["annual"]}
    val = valuation.build(
        session, company.id, own_store, sector, prefer, peers["rows"], trend_by_key
    )
    commentary = mgmt.commentary(session, company.id)
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
    }
    result["thesis"] = thesis.build(
        result,
        sector,
        score=score,
        missing=missing,
        mgmt=commentary,
        applied=applied,
        auditor_flags=auditor_flags,
    )
    return result
