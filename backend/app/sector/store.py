"""Database side of the sector engine: finding comparable metric values, and recording peer
statistics and scores as evidence that can be recomputed.

Peer statistics (`peer.v1`) and scores (`score.v1`) are CALCULATION evidence linked to the exact
evidence they were computed from: the company's own metric and each peer's. Validation recomputes
them from those links, and flags them if an input was superseded, withdrawn or no longer gives the
stored value.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.core import canon
from app.evidence.ledger import ENGINE_PROVIDER
from app.models import (
    Company,
    EvidenceClass,
    EvidenceLink,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    LinkRole,
)
from app.sector.core import (
    HIGHER,
    Candidate,
    favourable_share,
    mean,
    median,
)
from app.sector.profiles import MIN_PEERS, ScoredMetric

PEER_METHOD = "peer.v1"
SCORE_METHOD = "score.v1"
TOLERANCE = Decimal("0.0001")
KINDS = ("CALCULATION", "TECHNICAL")


class MetricIndex:
    """A company's usable calculated metrics: selected by validation and not blocked."""

    def __init__(self, session: Session, company_id: uuid.UUID) -> None:
        usable = {
            r.selected_evidence_id
            for r in session.scalars(
                select(EvidenceResolution).where(
                    EvidenceResolution.company_id == company_id,
                    EvidenceResolution.selected_evidence_id.is_not(None),
                    EvidenceResolution.blocked.is_(False),
                )
            )
        }
        self.by_metric: dict[str, list[EvidenceRecord]] = {}
        for r in session.scalars(
            select(EvidenceRecord).where(
                EvidenceRecord.company_id == company_id,
                EvidenceRecord.kind.in_(KINDS),
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
            )
        ):
            if r.id in usable and r.value is not None:
                self.by_metric.setdefault(r.metric, []).append(r)

    def bases_for(self, scored: list[ScoredMetric]) -> dict[str, int]:
        """How many of the given period-based metrics exist, per basis."""
        counts: dict[str, int] = {}
        for sm in scored:
            if not sm.periods:
                continue
            seen = {
                r.basis
                for r in self.by_metric.get(sm.code, [])
                if r.period_type in sm.periods and r.basis
            }
            for basis in seen:
                counts[basis] = counts.get(basis, 0) + 1
        return counts

    def candidates(self, sm: ScoredMetric, basis: str | None) -> list[EvidenceRecord]:
        """Every usable value of the metric on the basis, newest period first."""
        records = self.by_metric.get(sm.code, [])
        if sm.periods:
            order = {pt: i for i, pt in enumerate(sm.periods)}
            found = [
                r for r in records if r.period_type in order and (basis is None or r.basis == basis)
            ]
            return sorted(found, key=lambda r: (order[r.period_type], -r.period_end.toordinal()))
        found = [r for r in records if basis is None or not r.basis or r.basis == basis]
        return sorted(found, key=lambda r: -r.period_end.toordinal())

    def pick(
        self,
        sm: ScoredMetric,
        basis: str | None,
        align: EvidenceRecord | None = None,
    ) -> EvidenceRecord | None:
        """The metric's value for one company; with `align`, only the same period and basis."""
        records = self.by_metric.get(sm.code, [])
        if sm.periods:
            for ptype in sm.periods:
                cands = [
                    r
                    for r in records
                    if r.period_type == ptype and (basis is None or r.basis == basis)
                ]
                if align is not None:
                    cands = [
                        r
                        for r in cands
                        if r.period_end == align.period_end and r.period_type == align.period_type
                    ]
                if cands:
                    return max(cands, key=lambda r: r.period_end)
            return None
        # as of the price date: technical metrics carry no basis, valuation ones do
        cands = [r for r in records if basis is None or not r.basis or r.basis == basis]
        if align is not None:
            cands = [r for r in cands if r.period_end == align.period_end]
        return max(cands, key=lambda r: (r.period_end, r.basis == basis)) if cands else None


def candidate_for(session: Session, company: Company) -> Candidate | None:
    """A company as a peer candidate, or None if it has no calculated metrics at all."""
    index_rows = list(
        session.scalars(
            select(EvidenceResolution).where(
                EvidenceResolution.company_id == company.id,
                EvidenceResolution.selected_evidence_id.is_not(None),
                EvidenceResolution.blocked.is_(False),
            )
        )
    )
    if not index_rows:
        return None
    # A bank is recognised by its statements (interest earned) or, where no bank-format statement
    # has been read yet, by its sector: scoring a bank on non-financial metrics would be wrong.
    sector_code = company.sector.code if company.sector else None
    bank = sector_code == "banking" or any(r.metric == "INCOME.interest_earned" for r in index_rows)
    size = None
    totals = [
        r for r in index_rows if r.metric == "INCOME.total_income" and r.period_type == "ANNUAL"
    ]
    if totals:
        # latest year, consolidated preferred; this is only used to rank peers by size
        best = max(totals, key=lambda r: (r.period_end, r.basis == "CONSOLIDATED"))
        record = session.get(EvidenceRecord, best.selected_evidence_id)
        size = record.value if record else None
    return Candidate(
        company_id=str(company.id),
        name=company.common_name,
        sector_code=sector_code,
        industry_code=company.industry.code if company.industry else None,
        profile="BANK" if bank else "NONFIN",
        size=size,
    )


# ---- registration -------------------------------------------------------------------------------


def _digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _register(
    session: Session,
    *,
    company_id: uuid.UUID,
    metric: str,
    value: Decimal,
    unit: str,
    period_end: date,
    period_type: str | None,
    basis: str | None,
    expression: str,
    details: dict,
    inputs: list[tuple[str, EvidenceRecord]],
    now: datetime,
) -> EvidenceRecord:
    value = value.quantize(Decimal("0.000001"))
    tiers = [r.authority_tier for _, r in inputs if r.authority_tier is not None]
    digest = _digest(
        str(company_id),
        metric,
        canon(value),
        unit,
        str(period_end),
        period_type,
        basis,
        [(a, r.id, canon(r.value)) for a, r in inputs],
        details.get("weights"),
    )
    existing = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.evidence_class == EvidenceClass.CALCULATION,
            EvidenceRecord.content_hash == digest,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )
    if existing is not None:
        return existing
    record = EvidenceRecord(
        company_id=company_id,
        evidence_class=EvidenceClass.CALCULATION,
        kind="SECTOR",
        metric=metric,
        period_end=period_end,
        period_type=period_type,
        basis=basis,
        value=value,
        unit=unit,
        expression=expression,
        provider_code=ENGINE_PROVIDER,
        authority_tier=max(tiers) if tiers else None,
        source_url=f"calculation:{metric}",
        retrieved_at=now,
        details=details,
        content_hash=digest,
        created_by="sector-engine",
        status_changed_at=now,
    )
    session.add(record)
    session.flush()
    for alias, source in inputs:
        session.add(
            EvidenceLink(
                evidence_id=record.id, input_id=source.id, role=LinkRole.INPUT, alias=alias
            )
        )
    for old in session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.metric == metric,
            EvidenceRecord.period_end == period_end,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.id != record.id,
        )
    ):
        old.status = EvidenceStatus.SUPERSEDED
        old.status_reason = f"recalculated as evidence {record.id}"
        old.status_changed_at = now
    session.flush()
    return record


@dataclass
class PeerStats:
    rank: EvidenceRecord
    median: EvidenceRecord


def register_peer_stats(
    session: Session,
    company: Company,
    sm: ScoredMetric,
    subject: EvidenceRecord,
    peers: list[tuple[Candidate, EvidenceRecord]],
    now: datetime,
) -> PeerStats:
    values = [r.value for _, r in peers]
    details = {
        "method": PEER_METHOD,
        "scored_metric": sm.code,
        "label": sm.label,
        "pillar": sm.pillar,
        "direction": sm.direction,
        "n": len(peers),
        "thin_peer_group": len(peers) < MIN_PEERS,
        "subject": {"evidence_id": subject.id, "value": str(subject.value)},
        "peers": [
            {"company": c.name, "evidence_id": r.id, "value": str(r.value)} for c, r in peers
        ],
    }
    inputs = [("subject", subject), *[(f"peer{i}", r) for i, (_, r) in enumerate(peers, 1)]]
    common = dict(
        company_id=company.id,
        period_end=subject.period_end,
        period_type=subject.period_type,
        basis=subject.basis,
        inputs=inputs,
        now=now,
    )
    rank = _register(
        session,
        metric=f"PEERRANK.{sm.code}",
        value=favourable_share(subject.value, values, sm.direction),
        unit="percent",
        expression=f"share of {len(peers)} peers beaten ({sm.direction} is favourable)",
        details={**details, "stat": "rank"},
        **common,
    )
    med = _register(
        session,
        metric=f"PEERMEDIAN.{sm.code}",
        value=median(values),
        unit=subject.unit or "",
        expression=f"median of {len(peers)} peer values",
        details={**details, "stat": "median"},
        **common,
    )
    return PeerStats(rank, med)


def register_pillar_score(
    session: Session,
    company: Company,
    pillar: str,
    ranks: list[EvidenceRecord],
    thin: bool,
    now: datetime,
) -> EvidenceRecord:
    return _register(
        session,
        company_id=company.id,
        metric=f"SCORE.{pillar}",
        value=mean([r.value for r in ranks]),
        unit="percent",
        period_end=max(r.period_end for r in ranks),
        period_type=None,
        basis=None,
        expression=f"average of {len(ranks)} peer rank(s) in {pillar}",
        details={
            "method": SCORE_METHOD,
            "level": "pillar",
            "pillar": pillar,
            "thin_peer_group": thin,
            "metrics": [r.metric.removeprefix("PEERRANK.") for r in ranks],
        },
        inputs=[(f"m{i}", r) for i, r in enumerate(ranks, 1)],
        now=now,
    )


def register_overall_score(
    session: Session,
    company: Company,
    pillar_records: dict[str, EvidenceRecord],
    weights: dict[str, Decimal],
    thin: bool,
    now: datetime,
) -> EvidenceRecord:
    total = sum((weights[p] * r.value for p, r in pillar_records.items()), Decimal(0)) / sum(
        weights[p] for p in pillar_records
    )
    return _register(
        session,
        company_id=company.id,
        metric="SCORE.overall",
        value=total,
        unit="percent",
        period_end=max(r.period_end for r in pillar_records.values()),
        period_type=None,
        basis=None,
        expression="sector-weighted average of pillar scores",
        details={
            "method": SCORE_METHOD,
            "level": "overall",
            "thin_peer_group": thin,
            "weights": {p: canon(weights[p]) for p in pillar_records},
        },
        inputs=list(pillar_records.items()),
        now=now,
    )


# ---- recomputation (used by validation) ------------------------------------------------------


def recompute(session: Session, record: EvidenceRecord) -> tuple[Decimal | None, list[str]]:
    """(value recomputed from the linked evidence, problems). Used by the lineage check."""
    details = record.details or {}
    links = list(session.scalars(select(EvidenceLink).where(EvidenceLink.evidence_id == record.id)))
    inputs: dict[str, EvidenceRecord] = {}
    problems: list[str] = []
    for link in links:
        source = session.get(EvidenceRecord, link.input_id)
        if source is None or source.status is not EvidenceStatus.ACTIVE:
            problems.append(
                f"input {link.input_id} is "
                f"{source.status.value if source else 'missing'}; recalculate"
            )
        elif source.value is None:
            problems.append(f"input {link.input_id} has no value")
        else:
            inputs[link.alias] = source
    if problems:
        return None, problems
    method = details.get("method")
    if method == PEER_METHOD:
        subject = inputs.get("subject")
        peers = [r.value for a, r in sorted(inputs.items()) if a.startswith("peer")]
        if subject is None or not peers:
            return None, ["a peer statistic needs the company's value and at least one peer"]
        if details.get("stat") == "median":
            return median(peers), []
        return favourable_share(subject.value, peers, details.get("direction", HIGHER)), []
    if method == SCORE_METHOD:
        if details.get("level") == "overall":
            weights = {p: Decimal(w) for p, w in details["weights"].items()}
            if set(weights) != set(inputs):
                return None, ["the pillar scores no longer match the recorded weights"]
            return (
                sum((weights[p] * inputs[p].value for p in weights), Decimal(0))
                / sum(weights.values()),
                [],
            )
        return mean([r.value for r in inputs.values()]), []
    return None, [f"unknown method {method!r}"]


__all__ = [
    "MetricIndex",
    "PEER_METHOD",
    "PeerStats",
    "SCORE_METHOD",
    "TOLERANCE",
    "candidate_for",
    "recompute",
    "register_overall_score",
    "register_peer_stats",
    "register_pillar_score",
]
