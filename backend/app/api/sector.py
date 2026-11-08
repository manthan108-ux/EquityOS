"""Read-only API for peers, sector weights and relative scores (Phase 8)."""

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.models import (
    Company,
    EvidenceRecord,
    EvidenceStatus,
    Peer,
    PeerSet,
    SectorWeightSet,
    WeightAdjustment,
)

router = APIRouter(tags=["sector"])
SessionDep = Annotated[Session, Depends(get_session)]


class PeerOut(BaseModel):
    rank: int
    company_id: uuid.UUID
    name: str
    tier: str
    size_ratio: str | None
    reason: str


class PeersOut(BaseModel):
    peer_set_id: int
    built_at: datetime
    as_of: date
    sector: str | None
    criteria: dict
    candidates: int
    thin: bool
    peers: list[PeerOut]
    left_out: list[dict]


class ComparisonOut(BaseModel):
    evidence_id: int
    metric: str
    label: str
    pillar: str
    direction: str
    company_value: str
    peer_median: str | None
    percent_of_peers_beaten: str
    peers_compared: int
    thin_peer_group: bool
    peers: list[dict]


class WeightOut(BaseModel):
    pillar: str
    base_weight: str
    adjusted_weight: str
    reason: str


class ScorecardOut(BaseModel):
    peer_set_id: int
    basis: str | None
    pillar_scores: dict
    overall: str | None
    overall_withheld: str | None
    weight_coverage_pct: str | None
    weights: list[WeightOut]
    comparisons: list[ComparisonOut]
    gaps: list[dict]
    meaning: str


class WeightSetOut(BaseModel):
    sector_code: str
    version: str
    profile: str
    pillar_weights: dict
    rationale: dict
    note: str


def _company(session: Session, company_id: uuid.UUID) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _latest(session: Session, company_id: uuid.UUID) -> PeerSet:
    ps = session.scalar(
        select(PeerSet).where(PeerSet.company_id == company_id).order_by(PeerSet.id.desc()).limit(1)
    )
    if ps is None:
        raise HTTPException(
            status_code=404, detail="No sector analysis has been run for this company"
        )
    return ps


@router.get("/api/companies/{company_id}/peers", response_model=PeersOut)
def peers(company_id: uuid.UUID, session: SessionDep) -> PeersOut:
    """The chosen peers and why, and the candidates that were left out and why."""
    _company(session, company_id)
    ps = _latest(session, company_id)
    rows = session.scalars(select(Peer).where(Peer.peer_set_id == ps.id).order_by(Peer.rank))
    return PeersOut(
        peer_set_id=ps.id,
        built_at=ps.built_at,
        as_of=ps.as_of,
        sector=ps.sector_code,
        criteria=ps.criteria,
        candidates=ps.candidates,
        thin=ps.thin,
        peers=[
            PeerOut(
                rank=p.rank,
                company_id=p.company_id,
                name=session.get(Company, p.company_id).common_name,
                tier=p.tier,
                size_ratio=str(p.size_ratio) if p.size_ratio is not None else None,
                reason=p.reason,
            )
            for p in rows
        ],
        left_out=(ps.summary or {}).get("rejected", []),
    )


@router.get("/api/companies/{company_id}/scorecard", response_model=ScorecardOut)
def scorecard(company_id: uuid.UUID, session: SessionDep) -> ScorecardOut:
    _company(session, company_id)
    ps = _latest(session, company_id)
    summary = ps.summary or {}
    medians = {
        r.details["scored_metric"]: r
        for r in session.scalars(
            select(EvidenceRecord).where(
                EvidenceRecord.company_id == company_id,
                EvidenceRecord.metric.like("PEERMEDIAN.%"),
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
            )
        )
    }
    comparisons = []
    for r in session.scalars(
        select(EvidenceRecord)
        .where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.metric.like("PEERRANK.%"),
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
        .order_by(EvidenceRecord.metric)
    ):
        d = r.details
        med = medians.get(d["scored_metric"])
        comparisons.append(
            ComparisonOut(
                evidence_id=r.id,
                metric=d["scored_metric"],
                label=d["label"],
                pillar=d["pillar"],
                direction=d["direction"],
                company_value=d["subject"]["value"],
                peer_median=str(med.value) if med else None,
                percent_of_peers_beaten=str(r.value),
                peers_compared=d["n"],
                thin_peer_group=d["thin_peer_group"],
                peers=d["peers"],
            )
        )
    weights = [
        WeightOut(
            pillar=w.pillar,
            base_weight=str(w.base_weight),
            adjusted_weight=str(w.adjusted_weight),
            reason=w.reason,
        )
        for w in session.scalars(
            select(WeightAdjustment).where(WeightAdjustment.peer_set_id == ps.id)
        )
    ]
    return ScorecardOut(
        peer_set_id=ps.id,
        basis=ps.criteria.get("basis"),
        pillar_scores=summary.get("pillar_scores", {}),
        overall=summary.get("overall"),
        overall_withheld=summary.get("overall_withheld"),
        weight_coverage_pct=summary.get("weight_coverage_pct"),
        weights=weights,
        comparisons=comparisons,
        gaps=summary.get("gaps", []),
        meaning=(
            "Scores are relative standing among the chosen peers (share of peers beaten), not a "
            "measure of quality or a forecast. With fewer than 3 peers they are coarse and are "
            "marked thin."
        ),
    )


@router.get("/api/sector-weights", response_model=list[WeightSetOut])
def sector_weights(session: SessionDep, sector: str | None = None) -> list[WeightSetOut]:
    """The curated pillar weights and the reason for each. Project judgement, not derived data."""
    query = select(SectorWeightSet).order_by(SectorWeightSet.sector_code, SectorWeightSet.version)
    if sector:
        query = query.where(SectorWeightSet.sector_code == sector)
    return [
        WeightSetOut(
            sector_code=w.sector_code,
            version=w.version,
            profile=w.profile,
            pillar_weights=w.pillar_weights,
            rationale=w.rationale,
            note=w.note,
        )
        for w in session.scalars(query)
    ]
