"""One sector and peer analysis for one company.

    sector profile + curated weights -> peers -> metric by metric comparison -> pillar scores
    -> weighted overall -> record the peer set, adjustments and gaps -> validate again

Everything numeric is CALCULATION evidence linked to the evidence it came from. No LLM, no
estimates: a metric a company or its peers lack is a recorded gap.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calc.engine import retire_stale_calculations
from app.evidence.engine import ValidationReport, validate_company
from app.models import (
    Company,
    EvidenceRecord,
    EvidenceResolution,
    Peer,
    PeerSet,
    SectorWeightSet,
    WeightAdjustment,
)
from app.sector import profiles
from app.sector.core import (
    Candidate,
    PeerPick,
    Scorecard,
    score,
    select_peers,
)
from app.sector.profiles import MIN_PEERS, PILLARS, SectorProfile
from app.sector.store import (
    MetricIndex,
    PeerStats,
    candidate_for,
    comparable_from_for,
    register_overall_score,
    register_peer_stats,
    register_pillar_score,
)

ENGINE_VERSION = "8.0"


@dataclass
class MetricOutcome:
    code: str
    label: str
    pillar: str
    status: str  # COMPARED | GAP
    reason: str | None = None
    peers: int = 0
    stats: PeerStats | None = None


@dataclass
class SectorReport:
    company: Company
    candidate: Candidate | None
    profile: SectorProfile | None
    peer_set: PeerSet | None
    picks: list[PeerPick] = field(default_factory=list)
    rejected: list[tuple[Candidate, str]] = field(default_factory=list)
    outcomes: list[MetricOutcome] = field(default_factory=list)
    scorecard: Scorecard | None = None
    pillar_records: dict[str, EvidenceRecord] = field(default_factory=dict)
    overall: EvidenceRecord | None = None
    validation: ValidationReport | None = None
    basis: str | None = None


def ensure_weight_sets(session: Session) -> dict[str, SectorWeightSet]:
    """Store the curated weight sets (one row per sector and version). Existing rows are kept."""
    rows: dict[str, SectorWeightSet] = {}
    for p in [*profiles.PROFILES.values(), profiles.GENERIC_BANK, profiles.GENERIC_NONFIN]:
        row = session.scalar(
            select(SectorWeightSet).where(
                SectorWeightSet.sector_code == p.sector_code,
                SectorWeightSet.version == profiles.VERSION,
            )
        )
        if row is None:
            row = SectorWeightSet(
                sector_code=p.sector_code,
                version=profiles.VERSION,
                profile=p.profile,
                pillar_weights=dict(p.weights),
                rationale=dict(p.rationale),
                note=p.note,
            )
            session.add(row)
        rows[p.sector_code] = row
    session.flush()
    return rows


def all_candidates(session: Session) -> list[tuple[Company, Candidate]]:
    ids = set(session.scalars(select(EvidenceResolution.company_id).distinct()))
    out = []
    for company in session.scalars(
        select(Company).where(Company.id.in_(ids)).order_by(Company.common_name)
    ):
        candidate = candidate_for(session, company)
        if candidate is not None:
            out.append((company, candidate))
    return out


def _choose_basis(index: MetricIndex, scored) -> str:
    counts = index.bases_for(scored)
    if not counts:
        return "CONSOLIDATED"
    return max(counts, key=lambda b: (counts[b], b == "CONSOLIDATED"))


def _best_alignment(sm, basis, subject_index, picks, peer_indexes):
    """The company's value at the period that the most peers can be compared at.

    Not simply the company's newest period: a peer whose latest year is not stored would then drop
    out. Among the company's periods the first with at least MIN_PEERS comparable peers wins
    (newest first); if none reaches that, the period with the most peers does (newest on ties).
    """
    best = (None, [])
    for subject in subject_index.candidates(sm, basis):
        comparable = []
        for pick in picks:
            value = peer_indexes[pick.candidate.company_id].pick(sm, basis, align=subject)
            if value is not None:
                comparable.append((pick.candidate, value))
        if len(comparable) >= MIN_PEERS:
            return subject, comparable
        if best[0] is None or len(comparable) > len(best[1]):
            best = (subject, comparable)
    return best


def analyze_company(
    session: Session,
    company_id: uuid.UUID,
    *,
    today: date | None = None,
    now: datetime | None = None,
    validate: bool = True,
    pool: list[tuple[Company, Candidate]] | None = None,
) -> SectorReport:
    now = now or datetime.now(UTC)
    today = today or now.date()
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError(f"no company {company_id}")
    retire_stale_calculations(session, company_id, now)
    weight_rows = ensure_weight_sets(session)
    pool = pool if pool is not None else all_candidates(session)
    candidate = next((c for co, c in pool if co.id == company.id), None)
    if candidate is None:
        return SectorReport(company, None, None, None)
    profile = profiles.profile_for(candidate.sector_code, candidate.profile)
    picks, rejected = select_peers(candidate, [c for _, c in pool])
    thin = len(picks) < MIN_PEERS
    by_id = {str(co.id): co for co, _ in pool}

    report = SectorReport(company, candidate, profile, None, picks, rejected)
    scored = list(profiles.METRICS[candidate.profile])
    subject_index = MetricIndex(session, company.id, comparable_from_for(session, company.id))
    peer_indexes = {
        p.candidate.company_id: MetricIndex(
            session,
            by_id[p.candidate.company_id].id,
            comparable_from_for(session, by_id[p.candidate.company_id].id),
        )
        for p in picks
    }
    report.basis = basis = _choose_basis(subject_index, scored)

    ranks_by_pillar: dict[str, list[EvidenceRecord]] = {p: [] for p in PILLARS}
    for sm in scored:
        subject, comparable = _best_alignment(sm, basis, subject_index, picks, peer_indexes)
        if subject is None:
            report.outcomes.append(
                MetricOutcome(
                    sm.code, sm.label, sm.pillar, "GAP", "the company has no value for it"
                )
            )
            continue
        if not comparable:
            reason = (
                "no peer has a value for the same period and basis"
                if picks
                else "the company has no peers"
            )
            report.outcomes.append(MetricOutcome(sm.code, sm.label, sm.pillar, "GAP", reason))
            continue
        stats = register_peer_stats(session, company, sm, subject, comparable, now)
        ranks_by_pillar[sm.pillar].append(stats.rank)
        report.outcomes.append(
            MetricOutcome(sm.code, sm.label, sm.pillar, "COMPARED", None, len(comparable), stats)
        )

    card = score({p: [r.value for r in rs] for p, rs in ranks_by_pillar.items()}, profile.weights)
    if thin and card.overall is not None:
        # A percentile against one or two peers is a head-to-head, not a sector view. The pillar
        # scores stay visible (flagged thin) but no overall score is manufactured from them.
        card.overall = None
        card.withheld = (
            f"only {len(picks)} peer(s) in the sector (minimum {MIN_PEERS}): pillar scores are "
            "shown as a small-sample comparison, no overall score"
        )
    report.scorecard = card
    for pillar in PILLARS:
        if ranks_by_pillar[pillar]:
            report.pillar_records[pillar] = register_pillar_score(
                session, company, pillar, ranks_by_pillar[pillar], thin, now
            )
    if report.pillar_records and card.overall is not None:
        report.overall = register_overall_score(
            session,
            company,
            report.pillar_records,
            {p: w for p, w in card.weights.items() if p in report.pillar_records},
            thin,
            now,
        )

    peer_set = PeerSet(
        company_id=company.id,
        built_at=now,
        as_of=today,
        sector_code=candidate.sector_code,
        weight_set_id=weight_rows[profile.sector_code].id,
        criteria={
            "engine_version": ENGINE_VERSION,
            "weights_version": profiles.VERSION,
            "profile": candidate.profile,
            "same_statement_profile": True,
            "same_sector_required": True,
            "order": "same industry first, then closest in annual total income",
            "max_peers": 8,
            "minimum_for_reliable_view": MIN_PEERS,
            "basis": basis,
        },
        candidates=len(pool) - 1,
        peer_count=len(picks),
        thin=thin,
        summary={
            "compared": sum(1 for o in report.outcomes if o.status == "COMPARED"),
            "gaps": [
                {"metric": o.code, "reason": o.reason} for o in report.outcomes if o.status == "GAP"
            ],
            "rejected": [{"company": c.name, "reason": why} for c, why in rejected],
            "pillar_scores": {
                p: str(v.quantize(Decimal("0.01"))) for p, v in card.pillar_scores.items()
            },
            "overall": str(card.overall.quantize(Decimal("0.01")))
            if card.overall is not None
            else None,
            "weight_coverage_pct": str(card.coverage.quantize(Decimal("0.1"))),
            "overall_withheld": card.withheld,
        },
    )
    session.add(peer_set)
    session.flush()
    report.peer_set = peer_set
    for pick in picks:
        session.add(
            Peer(
                peer_set_id=peer_set.id,
                company_id=by_id[pick.candidate.company_id].id,
                tier=pick.tier,
                rank=pick.rank,
                size_value=pick.candidate.size,
                size_ratio=pick.size_ratio,
                reason=pick.reason,
            )
        )
    for change in card.changes:
        session.add(
            WeightAdjustment(
                peer_set_id=peer_set.id,
                pillar=change.pillar,
                base_weight=change.base,
                adjusted_weight=change.adjusted,
                reason=change.reason,
            )
        )
    session.flush()
    if validate:
        report.validation = validate_company(session, company.id, today=today, now=now, sync=False)
    return report


def analyze_all(
    session: Session, *, today: date | None = None, now: datetime | None = None
) -> list[SectorReport]:
    pool = all_candidates(session)
    return [analyze_company(session, co.id, today=today, now=now, pool=pool) for co, _ in pool]


__all__ = ["ENGINE_VERSION", "MetricOutcome", "SectorReport", "analyze_all", "analyze_company"]
