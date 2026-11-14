"""Keep peer ranks, peer medians and sector scores in step with the data they were built from.

A peer rank or median is a calculation over other evidence (the company's own metric and its
peers' values). When a refresh replaces one of those inputs, the stored rank still points at the
old version and is stale until the sector analysis runs again. `analyze_company` already retires
stale calculations and writes current ones; what was missing is something that notices and runs it.

`refresh_stale_peers` finds the companies whose peer or score evidence points at inputs that are no
longer ACTIVE and re-runs the existing sector analysis for them. It adds versions only (old ones are
kept); it never edits a stored value. The pipeline calls it at the end of every run.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, aliased

from app.models import EvidenceClass, EvidenceLink, EvidenceRecord, EvidenceStatus

log = logging.getLogger("equityos.pipeline")
PEER_PREFIXES = ("PEERRANK.", "PEERMEDIAN.", "SCORE.")
CREATED_BY = "sector-engine"
DEFAULT_LIMIT = 10


def stale_peer_companies(session: Session, *, limit: int | None = None) -> list[uuid.UUID]:
    """Companies with an ACTIVE peer rank, median or score whose input is no longer ACTIVE."""
    inp = aliased(EvidenceRecord)
    query = (
        select(EvidenceRecord.company_id)
        .join(EvidenceLink, EvidenceLink.evidence_id == EvidenceRecord.id)
        .join(inp, inp.id == EvidenceLink.input_id)
        .where(
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.evidence_class == EvidenceClass.CALCULATION,
            EvidenceRecord.created_by == CREATED_BY,
            or_(*(EvidenceRecord.metric.like(f"{p}%") for p in PEER_PREFIXES)),
            inp.status != EvidenceStatus.ACTIVE,
        )
        .group_by(EvidenceRecord.company_id)
        .order_by(EvidenceRecord.company_id)
    )
    if limit:
        query = query.limit(limit)
    return list(session.scalars(query))


def refresh_stale_peers(
    session: Session, *, limit: int = DEFAULT_LIMIT, now: datetime | None = None
) -> dict:
    """Re-run the sector analysis for companies with stale peer evidence (at most `limit`).

    Returns {"stale": n found, "refreshed": [company ids], "failed": {id: error}, "remaining": n}.
    One company's failure is recorded and does not stop the others.
    """
    from app.sector.engine import all_candidates, analyze_company

    found = stale_peer_companies(session)
    result: dict = {"stale": len(found), "refreshed": [], "failed": {}, "remaining": 0}
    if not found:
        return result
    now = now or datetime.now(UTC)
    pool = all_candidates(session)  # built once: it does not depend on the company
    for company_id in found[:limit]:
        try:
            analyze_company(session, company_id, now=now, pool=pool)
            session.commit()
            result["refreshed"].append(str(company_id))
        except Exception as exc:  # noqa: BLE001 - isolate: record and carry on
            session.rollback()
            log.exception("peer refresh failed for %s", company_id)
            result["failed"][str(company_id)] = f"{type(exc).__name__}: {exc}"[:200]
    result["remaining"] = len(stale_peer_companies(session))
    return result
