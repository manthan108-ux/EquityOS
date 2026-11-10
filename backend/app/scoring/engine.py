"""Choosing a scoring methodology for a company and producing an explainable score.

Methodologies (the `methodology` of a run):

RELATIVE     the Phase 8 peer-percentile score, unchanged: >= 3 ELIGIBLE peers and its own 60%
             weight-coverage rule. This module only reads and re-presents it.
ABSOLUTE     the company's metrics read against benchmarks (empirical distributions where a
             representative sample exists, documented CONVENTION curves otherwise).
HYBRID       disabled: it needs a validated calibration between the two scales (see `calibration`),
             and with the sample sizes held it cannot be justified.
UNAVAILABLE  nothing can be justified; the reason is stored and shown. Never a zero.

Absolute and relative scores are different scales and are never ranked together.
LLMs have no role here: every number comes from deterministic code over validated evidence.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BenchmarkStat,
    Company,
    PeerSet,
    ScoreMetricResult,
    ScoreRun,
    ScoringConfig,
    Security,
)
from app.scoring import benchmarks, disclosures, registry
from app.scoring import transform as tf
from app.scoring.registry import Spec
from app.sector.core import select_peers
from app.sector.engine import all_candidates
from app.sector.store import MetricIndex, comparable_from_for

MIN_ELIGIBLE_PEERS = 3
PEER_MIN_SHARED_METRICS = 4  # comparable metrics a peer must share with the company ...
PEER_MIN_PILLARS = 2  # ... spread over at least this many pillars, at the same period
MIN_COMPUTABLE_COVERAGE = 60.0  # of the weight that has a source at all (see _finish_absolute)
MIN_SCORED_METRICS = 6
HYBRID_MIN_GROUP = 8  # companies with both scores, per profile group, to justify a calibration
HYBRID_MIN_RHO = 0.6
HYBRID_ENABLED = False  # flipped only by a calibration that passes (see `calibration`)

ABSOLUTE, RELATIVE, HYBRID, UNAVAILABLE, AUTO = (
    "ABSOLUTE",
    "RELATIVE",
    "HYBRID",
    "UNAVAILABLE",
    "AUTO",
)


@dataclass
class MetricResult:
    spec: Spec
    status: str  # SCORED | GAP | EXCLUDED
    reason: str | None = None
    value: Decimal | None = None
    score: Decimal | None = None
    kind_used: str | None = None
    record: object | None = None
    stat: BenchmarkStat | None = None
    benchmark: dict | None = None
    effective_weight: float = 0.0


@dataclass
class Outcome:
    company: Company
    methodology: str
    requested: str
    profile: registry.Profile | None
    overall: Decimal | None = None
    reason: str | None = None
    coverage: Decimal | None = None
    computable_coverage: Decimal | None = None
    confidence: str | None = None
    pillar_scores: dict[str, str] = field(default_factory=dict)
    pillar_coverage: dict[str, float] = field(default_factory=dict)
    metrics: list[MetricResult] = field(default_factory=list)
    peer_count: int = 0
    eligible_peers: list[str] = field(default_factory=list)
    explanation: dict = field(default_factory=dict)
    benchmark_version: int | None = None
    config_version: str | None = None
    input_hash: str = ""
    run: ScoreRun | None = None


# ---- peers --------------------------------------------------------------------------------------


def eligible_peers(session: Session, company: Company, profile: registry.Profile, pool=None):
    """Peers that share the industry AND have enough aligned data to be compared at all.

    A peer is eligible only if, at the company's own latest annual period, it has at least
    PEER_MIN_SHARED_METRICS of the profile's calculable metrics (over PEER_MIN_PILLARS pillars).
    Being in the database is not enough.
    """
    pool = pool if pool is not None else all_candidates(session)
    me = next((c for co, c in pool if co.id == company.id), None)
    if me is None:
        return [], []
    picks, _ = select_peers(me, [c for _, c in pool])
    by_id = {str(co.id): co for co, _ in pool}
    codes = [s.calc for s in profile.metrics if s.calc and not s.disclosure]
    pillar_of = {s.calc: s.pillar for s in profile.metrics if s.calc and not s.disclosure}
    subject = MetricIndex(session, company.id, comparable_from_for(session, company.id))
    period = _latest_annual_period(subject, codes)
    eligible = []
    for pick in picks:
        peer_company = by_id[pick.candidate.company_id]
        index = MetricIndex(session, peer_company.id, comparable_from_for(session, peer_company.id))
        have = [code for code in codes if _has_value_at(index, code, period)]
        pillars = {pillar_of[code] for code in have}
        if (
            period is not None
            and len(have) >= PEER_MIN_SHARED_METRICS
            and len(pillars) >= PEER_MIN_PILLARS
        ):
            eligible.append(pick.candidate.name)
    return eligible, [p.candidate.name for p in picks]


def _latest_annual_period(index: MetricIndex, codes: list[str]) -> date | None:
    ends = [
        r.period_end for c in codes for r in index.by_metric.get(c, []) if r.period_type == "ANNUAL"
    ]
    return max(ends) if ends else None


def _has_value_at(index: MetricIndex, code: str, period: date | None) -> bool:
    if period is None:
        return False
    return any(
        r.period_end == period and r.period_type == "ANNUAL" for r in index.by_metric.get(code, [])
    )


# ---- absolute scoring ---------------------------------------------------------------------------


def _candidates(index: MetricIndex, spec: Spec, basis: str | None) -> list:
    """The newest value of the metric and of each alternate (in order), where they exist."""
    found = [_pick_one(index, code, basis) for code in (spec.calc, *spec.alts)]
    return [r for r in found if r is not None]


def _pick_one(index: MetricIndex, code: str, basis: str | None):
    records = index.by_metric.get(code, [])
    annual = [
        r for r in records if r.period_type == "ANNUAL" and (basis is None or r.basis == basis)
    ]
    if annual:
        return max(annual, key=lambda r: r.period_end)
    asof = [
        r
        for r in records
        if (not r.period_type or r.period_type == "TTM")
        and (not r.basis or basis in (None, r.basis))
    ]
    return max(asof, key=lambda r: r.period_end) if asof else None


def _choose_basis(index: MetricIndex, specs: list[Spec]) -> str:
    counts: dict[str, int] = {}
    for spec in specs:
        if not spec.calc:
            continue
        for basis in {
            r.basis
            for r in index.by_metric.get(spec.calc, [])
            if r.period_type == "ANNUAL" and r.basis
        }:
            counts[basis] = counts.get(basis, 0) + 1
    if not counts:
        return "CONSOLIDATED"
    return max(counts, key=lambda b: (counts[b], b == "CONSOLIDATED"))


def _score_metric(session, spec: Spec, record, company: Company, version_id) -> MetricResult:
    value = Decimal(record.value)
    bad = tf.not_meaningful(record.metric, value)
    if bad:
        return MetricResult(spec, "EXCLUDED", bad, value, record=record)
    industry = company.industry.code if company.industry else None
    sector = company.sector.code if company.sector else None
    stat = (
        benchmarks.stat_for(session, version_id, record.metric, industry, sector)
        if version_id
        else None
    )
    try:
        if spec.kind == tf.PERCENTILE:
            if stat is not None:
                scored = tf.score_percentile(value, stat.stats, spec.direction)
                bench = {
                    "type": "EMPIRICAL",
                    "scope": f"{stat.scope_type}:{stat.scope_code}",
                    "confidence": stat.confidence,
                    "observations": stat.n_observations,
                    "companies": stat.n_companies,
                    "period": [str(stat.period_from), str(stat.period_to)],
                    "p25": stat.stats["p25"],
                    "median": stat.stats["median"],
                    "p75": stat.stats["p75"],
                    "p90": stat.stats["p90"],
                    **scored.detail,
                }
                return MetricResult(
                    spec, "SCORED", None, value, scored.score, "PERCENTILE", record, stat, bench
                )
            if spec.curve is not None:
                scored = tf.score_threshold(value, spec.curve)
                bench = {
                    "type": "CONVENTION",
                    "note": "no representative sample for a benchmark; documented convention curve",
                    "why": spec.curve.get("why"),
                    **scored.detail,
                }
                return MetricResult(
                    spec, "SCORED", None, value, scored.score, "THRESHOLD", record, None, bench
                )
            return MetricResult(
                spec,
                "GAP",
                "no benchmark: the sample is too small for a distribution",
                value,
                record=record,
            )
        if spec.kind == tf.THRESHOLD:
            scored = tf.score_threshold(value, spec.curve)
            bench = {"type": "CONVENTION", "why": spec.curve.get("why"), **scored.detail}
            return MetricResult(
                spec, "SCORED", None, value, scored.score, "THRESHOLD", record, None, bench
            )
        scored = tf.score_range(value, spec.curve)
        bench = {"type": "CONVENTION", "why": spec.curve.get("why"), **scored.detail}
        return MetricResult(spec, "SCORED", None, value, scored.score, "RANGE", record, None, bench)
    except tf.NotScorable as exc:
        return MetricResult(spec, "GAP", str(exc), value, record=record)


def score_absolute(
    session: Session, company: Company, *, as_of: date | None = None, version_id: int | None = None
) -> Outcome:
    registry.seed(session)
    as_of = as_of or datetime.now(UTC).date()
    sector = company.sector.code if company.sector else None
    industry = company.industry.code if company.industry else None
    profile = registry.profile_for(sector, industry)
    version = (
        session.get(benchmarks.BenchmarkVersion, version_id)
        if version_id
        else benchmarks.latest_version(session)
    )
    out = Outcome(company, ABSOLUTE, ABSOLUTE, profile, config_version=registry.VERSION)
    out.benchmark_version = version.id if version else None
    config = session.scalar(
        select(ScoringConfig).where(
            ScoringConfig.profile == profile.code, ScoringConfig.version == registry.VERSION
        )
    )
    if config is None:
        out.methodology, out.reason = UNAVAILABLE, "the scoring configuration has not been seeded"
        return out
    index = MetricIndex(session, company.id, comparable_from_for(session, company.id))
    specs = list(profile.metrics)
    basis = _choose_basis(index, specs)
    picks: dict[str, disclosures.Pick] = {}
    pillar_total = {p: sum(s.weight for s in specs if s.pillar == p) for p in registry.PILLARS}
    for spec in specs:
        pillar_weight = float(config.pillar_weights.get(spec.pillar, 0))
        share = spec.weight / pillar_total[spec.pillar] if pillar_total[spec.pillar] else 0
        effective = pillar_weight * share
        if spec.calc is None:
            out.metrics.append(
                MetricResult(spec, "GAP", spec.unavailable, effective_weight=effective)
            )
            continue
        if spec.disclosure:
            picked = disclosures.pick(
                session,
                company.id,
                spec.disclosure,
                as_of=as_of,
                bounds=spec.bounds,
                max_age_days=spec.max_age_days,
            )
            picks[spec.code] = picked
            if picked.record is None:
                out.metrics.append(
                    MetricResult(spec, "GAP", picked.reason, effective_weight=effective)
                )
                continue
            result = _score_metric(session, spec, picked.record, company, out.benchmark_version)
            result.effective_weight = effective
            if result.benchmark is not None:
                result.benchmark["disclosure"] = picked.note
            out.metrics.append(result)
            continue
        records = _candidates(index, spec, basis)
        if not records:
            reason = "the company has no validated value for it"
            if index.excluded and any(r.metric == spec.calc for r in index.excluded):
                reason = "excluded: its comparison period lies before a corporate restructuring"
            out.metrics.append(MetricResult(spec, "GAP", reason, effective_weight=effective))
            continue
        # the first of the metric and its alternates that can actually be scored; else the first gap
        results = [_score_metric(session, spec, r, company, out.benchmark_version) for r in records]
        result = next((x for x in results if x.status == "SCORED"), results[0])
        result.effective_weight = effective
        out.metrics.append(result)
    _check_disclosure_pair(out, picks)
    _finish_absolute(out, config, as_of, basis)
    return out


def _check_disclosure_pair(out: Outcome, picks: dict) -> None:
    """Net NPA above gross NPA is impossible: drop both rather than score either."""
    problem = disclosures.check_pair(
        picks.get("ASSET_QUALITY.gross_npa", disclosures.Pick(None)),
        picks.get("ASSET_QUALITY.net_npa", disclosures.Pick(None)),
    )
    if problem is None:
        return
    for m in out.metrics:
        if m.spec.code in ("ASSET_QUALITY.gross_npa", "ASSET_QUALITY.net_npa"):
            m.status, m.reason, m.score, m.benchmark = "GAP", problem, None, None


def _finish_absolute(out: Outcome, config: ScoringConfig, as_of: date, basis: str) -> None:
    scored = [m for m in out.metrics if m.status == "SCORED"]
    total = sum(m.effective_weight for m in out.metrics) or 1.0
    computable = sum(m.effective_weight for m in out.metrics if m.spec.calc) or 1.0
    got = sum(m.effective_weight for m in scored)
    # Two coverages. PROFILE coverage counts every metric the profile wants, including those that no
    # source in the system can supply yet; it sets the confidence. COMPUTABLE coverage counts only
    # metrics that have a source; it is the gate. A missing source is a platform gap, not a
    # weakness of the company, but the score must still rest on most of what CAN be measured.
    out.coverage = Decimal(str(round(got / total * 100, 1)))
    computable_coverage = round(got / computable * 100, 1)
    by_pillar: dict[str, list[MetricResult]] = {p: [] for p in registry.PILLARS}
    for m in out.metrics:
        by_pillar[m.spec.pillar].append(m)
    weights = {p: float(config.pillar_weights.get(p, 0)) for p in registry.PILLARS}
    for pillar, items in by_pillar.items():
        have = [m for m in items if m.status == "SCORED"]
        pillar_w = sum(m.spec.weight for m in items) or 1.0
        out.pillar_coverage[pillar] = round(sum(m.spec.weight for m in have) / pillar_w * 100, 1)
        if have:
            w = sum(m.spec.weight for m in have)
            mean = sum(float(m.score) * m.spec.weight for m in have) / w
            out.pillar_scores[pillar] = str(Decimal(str(round(mean, 3))))
    reasons = []
    if not scored:
        reasons.append("no metric could be scored")
    elif len(scored) < MIN_SCORED_METRICS:
        reasons.append(f"only {len(scored)} metric(s) scored (minimum {MIN_SCORED_METRICS})")
    for code in out.profile.critical_metrics if out.profile else ():
        m = next((x for x in out.metrics if x.spec.code == code), None)
        if m is not None and m.status != "SCORED":
            reasons.append(f"critical metric not available: {m.spec.name} ({m.reason})")
    missing_critical = [p for p in config.critical_pillars if p not in out.pillar_scores]
    if missing_critical:
        reasons.append(
            "critical pillar(s) without any scored metric: "
            + ", ".join(registry.PRETTY[p] for p in missing_critical)
        )
    if computable_coverage < float(config.min_weight_coverage):
        reasons.append(
            f"only {computable_coverage}% of the weight that has a data source could be scored "
            f"(minimum {config.min_weight_coverage}%); profile coverage {out.coverage}%"
        )
    ends = [
        m.record.period_end for m in scored if m.record is not None and m.spec.pillar != "VALUATION"
    ]
    if ends and (as_of - max(ends)).days > config.max_staleness_days:
        reasons.append(
            f"newest financial period ({max(ends)}) is older than {config.max_staleness_days} days"
        )
    present = {p: w for p, w in weights.items() if p in out.pillar_scores}
    if reasons:
        out.methodology, out.reason = UNAVAILABLE, "; ".join(reasons)
    else:
        wsum = sum(present.values())
        overall = sum(float(out.pillar_scores[p]) * w for p, w in present.items()) / wsum
        out.overall = Decimal(str(round(overall, 3)))
    out.computable_coverage = Decimal(str(computable_coverage))
    out.confidence = _confidence(out, scored)
    out.explanation = _explain(out, scored, basis, present, weights)
    out.explanation["computable_coverage_pct"] = computable_coverage
    out.input_hash = _hash(out, basis)


def _confidence(out: Outcome, scored: list[MetricResult]) -> str | None:
    if not scored:
        return None
    empirical = sum(
        m.effective_weight for m in scored if (m.benchmark or {}).get("type") == "EMPIRICAL"
    )
    share = empirical / (sum(m.effective_weight for m in scored) or 1.0)
    cover = float(out.coverage or 0)
    if cover >= 75 and share >= 0.5:
        return "HIGH"
    if cover >= 60 and share >= 0.25:
        return "MEDIUM"
    return "LOW"


def _explain(out: Outcome, scored, basis, present, weights) -> dict:
    ranked = sorted(scored, key=lambda m: float(m.score) * m.effective_weight)
    helped = [m for m in sorted(scored, key=lambda m: -float(m.score)) if float(m.score) >= 60][:3]
    hurt = [m for m in ranked if float(m.score) < 40][:3]
    conv = [m.spec.name for m in scored if (m.benchmark or {}).get("type") == "CONVENTION"]
    return {
        "basis": basis,
        "improved": [f"{m.spec.name}: {m.score} ({m.value:.4g} {m.spec.unit})" for m in helped],
        "reduced": [f"{m.spec.name}: {m.score} ({m.value:.4g} {m.spec.unit})" for m in hurt],
        "missing": [
            {"metric": m.spec.name, "pillar": m.spec.pillar, "reason": m.reason}
            for m in out.metrics
            if m.status != "SCORED"
        ],
        "assumptions": (
            [
                "Convention curves (not validated industry benchmarks) were used for: "
                + ", ".join(conv)
            ]
            if conv
            else []
        )
        + [
            "Missing metrics are excluded from the average, never scored as zero.",
            "Pillar weights are renormalised over pillars that have at least one scored metric.",
        ],
        "pillars_used": sorted(present),
        "note": "improved/reduced list metrics scoring >=60 / <40",
    }


def _hash(out: Outcome, basis: str) -> str:
    payload = {
        "company": str(out.company.id),
        "profile": out.profile.code if out.profile else None,
        "config": out.config_version,
        "benchmark": out.benchmark_version,
        "basis": basis,
        "metrics": [
            (m.spec.code, m.status, str(m.value), str(m.score), getattr(m.record, "id", None))
            for m in out.metrics
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# ---- relative (read from Phase 8) ---------------------------------------------------------------


def latest_peer_set(session: Session, company_id) -> PeerSet | None:
    return session.scalar(
        select(PeerSet).where(PeerSet.company_id == company_id).order_by(PeerSet.id.desc()).limit(1)
    )


def relative_outcome(
    session: Session, company: Company, profile, eligible: list[str]
) -> Outcome | None:
    """The stored Phase 8 relative score, or None if it withheld an overall score."""
    ps = latest_peer_set(session, company.id)
    if ps is None or not ps.summary or not ps.summary.get("overall"):
        return None
    out = Outcome(
        company, RELATIVE, RELATIVE, profile, config_version=ps.criteria.get("weights_version")
    )
    out.overall = Decimal(ps.summary["overall"])
    out.coverage = Decimal(ps.summary["weight_coverage_pct"])
    out.pillar_scores = dict(ps.summary.get("pillar_scores") or {})
    out.peer_count = ps.peer_count
    out.eligible_peers = eligible
    out.confidence = "MEDIUM" if ps.peer_count >= 4 and out.coverage >= 75 else "LOW"
    out.explanation = {
        "source": f"Phase 8 relative peer scoring (peer_sets id {ps.id})",
        "basis": ps.criteria.get("basis"),
        "gaps": ps.summary.get("gaps", []),
        "assumptions": ["Percentile of each metric among eligible same-industry peers, unchanged."],
    }
    out.input_hash = hashlib.sha256(
        f"peer_set:{ps.id}:{ps.summary.get('overall')}".encode()
    ).hexdigest()
    return out


# ---- hybrid -------------------------------------------------------------------------------------


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for pos, i in enumerate(order):
        ranks[i] = pos + 1.0
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) != len(b) or len(a) < 3:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else None


def calibration(pairs_by_group: dict[str, list[tuple[float, float]]]) -> dict:
    """Is a hybrid justified? Needs, per profile group, >= HYBRID_MIN_GROUP companies that have BOTH
    an absolute and a relative score and a rank correlation >= HYBRID_MIN_RHO between them.

    Why: the two scales answer different questions (distance from absolute benchmarks vs standing
    among peers). Blending is only meaningful if they order companies consistently, and that can
    only be tested with enough companies. If the test cannot pass, HYBRID stays off.
    """
    groups, justified = {}, bool(pairs_by_group)
    for group, pairs in pairs_by_group.items():
        n = len(pairs)
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs]) if n >= 3 else None
        ok = n >= HYBRID_MIN_GROUP and rho is not None and rho >= HYBRID_MIN_RHO
        groups[group] = {
            "companies": n,
            "spearman": None if rho is None else round(rho, 3),
            "ok": ok,
        }
        justified = justified and ok
    return {
        "justified": justified,
        "minimum_companies_per_group": HYBRID_MIN_GROUP,
        "minimum_rank_correlation": HYBRID_MIN_RHO,
        "groups": groups,
        "rationale": (
            "A hybrid blends an absolute score with a calibrated relative score only if both order "
            "companies consistently; with fewer companies than the minimum that cannot be tested."
        ),
    }


# ---- orchestration and storage ------------------------------------------------------------------


def score_company(
    session: Session,
    company: Company,
    requested: str = AUTO,
    *,
    as_of: date | None = None,
    pool=None,
    persist: bool = True,
) -> Outcome:
    registry.seed(session)
    sector = company.sector.code if company.sector else None
    industry = company.industry.code if company.industry else None
    profile = registry.profile_for(sector, industry)
    eligible, picks = eligible_peers(session, company, profile, pool)
    n_eligible = len(eligible)
    if requested == HYBRID:
        out = Outcome(company, UNAVAILABLE, HYBRID, profile, peer_count=len(picks))
        out.reason = "hybrid scoring is disabled: " + calibration({})["rationale"]
    elif requested == RELATIVE or (requested == AUTO and n_eligible >= MIN_ELIGIBLE_PEERS):
        out = relative_outcome(session, company, profile, eligible)
        if out is None:
            if requested == RELATIVE:
                out = Outcome(company, UNAVAILABLE, RELATIVE, profile, peer_count=len(picks))
                out.reason = (
                    f"{n_eligible} eligible peer(s); relative needs >= {MIN_ELIGIBLE_PEERS} "
                    "and enough weight coverage (the peer engine withheld an overall score)"
                )
            else:
                out = score_absolute(session, company, as_of=as_of)
                out.requested = AUTO
                if out.explanation is not None:
                    out.explanation["fallback"] = (
                        "relative score withheld by the peer engine (coverage); absolute shown"
                    )
        else:
            out.requested = requested
    else:
        out = score_absolute(session, company, as_of=as_of)
        out.requested = requested
        if requested == RELATIVE:
            out.methodology, out.overall = UNAVAILABLE, None
            out.reason = (
                f"only {n_eligible} eligible peer(s); relative scoring needs {MIN_ELIGIBLE_PEERS}"
            )
    out.peer_count = len(picks)
    out.eligible_peers = eligible
    if comparable_from_for(session, company.id) is not None and out.explanation is not None:
        from app.seeds.identity import IDENTITIES

        for symbol in session.scalars(
            select(Security.symbol).where(Security.company_id == company.id)
        ):
            if symbol in IDENTITIES and IDENTITIES[symbol].comparable_from:
                out.explanation["restructuring"] = IDENTITIES[symbol].note
    if (
        out.explanation is not None
        and out.methodology == ABSOLUTE
        and 0 < n_eligible < MIN_ELIGIBLE_PEERS
    ):
        out.explanation["peer_context"] = (
            f"{n_eligible} eligible peer(s) ({', '.join(eligible)}): below the "
            f"{MIN_ELIGIBLE_PEERS} "
            "needed for relative scoring, shown as context only"
        )
    if persist:
        store(session, out, as_of or datetime.now(UTC).date())
    return out


def store(session: Session, out: Outcome, as_of: date) -> ScoreRun:
    run = ScoreRun(
        company_id=out.company.id,
        methodology=out.methodology,
        requested=out.requested,
        profile=out.profile.code if out.profile else None,
        overall=out.overall,
        unavailable_reason=out.reason,
        weight_coverage=out.coverage,
        confidence=out.confidence,
        peer_count=out.peer_count,
        eligible_peer_count=len(out.eligible_peers),
        benchmark_version_id=out.benchmark_version
        if out.methodology == ABSOLUTE or out.reason
        else None,
        config_version=out.config_version,
        as_of=as_of,
        calculated_at=datetime.now(UTC),
        input_hash=out.input_hash or hashlib.sha256(b"none").hexdigest(),
        pillar_scores=out.pillar_scores or None,
        explanation={
            **out.explanation,
            "eligible_peers": out.eligible_peers,
            "pillar_coverage": out.pillar_coverage,
        },
    )
    session.add(run)
    session.flush()
    for m in out.metrics:
        session.add(
            ScoreMetricResult(
                run_id=run.id,
                metric_code=m.spec.code,
                pillar=m.spec.pillar,
                display_name=m.spec.name,
                status=m.status,
                reason=m.reason,
                actual_value=m.value,
                unit=m.spec.unit,
                normalized_score=m.score,
                kind=m.kind_used,
                weight=Decimal(str(round(m.effective_weight, 3))),
                benchmark_stat_id=m.stat.id if m.stat is not None else None,
                benchmark=m.benchmark,
                period_end=getattr(m.record, "period_end", None),
                basis=getattr(m.record, "basis", None),
                evidence_id=getattr(m.record, "id", None),
            )
        )
    session.flush()
    out.run = run
    return run
