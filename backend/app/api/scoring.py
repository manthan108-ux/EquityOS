"""Scores, methodology, benchmarks and explanations (Phase 10).

Reads are open like the rest of the API. The two POST endpoints recompute stored results, so they
are off unless SCORING_ADMIN_ENABLED=true (there is no authentication layer yet). Nothing here lets
a language model decide a number.
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.models import (
    BenchmarkObservation,
    BenchmarkStat,
    BenchmarkVersion,
    Company,
    MetricDefinition,
    ScoreMetricResult,
    ScoreRun,
)
from app.scoring import benchmarks, engine, registry

router = APIRouter(tags=["scoring"])
SessionDep = Annotated[Session, Depends(get_session)]
METHODS = ("AUTO", "ABSOLUTE", "RELATIVE", "HYBRID")


class MetricOut(BaseModel):
    metric: str
    name: str
    pillar: str
    status: str
    reason: str | None
    actual_value: str | None
    unit: str | None
    score: str | None
    scoring_kind: str | None
    weight: str
    benchmark: dict | None
    period_end: date | None
    basis: str | None
    evidence_id: int | None


class ScoreOut(BaseModel):
    run_id: int
    company_id: uuid.UUID
    company: str
    ticker: str | None
    sector: str | None
    industry: str | None
    methodology: str
    requested: str
    label: str
    overall: str | None
    unavailable_reason: str | None
    profile: str | None
    pillar_scores: dict | None
    weight_coverage: str | None
    confidence: str | None
    peer_count: int
    eligible_peer_count: int
    benchmark_version: str | None
    config_version: str | None
    as_of: date
    calculated_at: datetime
    input_hash: str
    explanation: dict | None


LABEL = {
    "RELATIVE": "Relative Peer Score",
    "ABSOLUTE": "Absolute Fundamental Score",
    "HYBRID": "Hybrid Score",
    "UNAVAILABLE": "Score unavailable",
}


def company_or_404(session: Session, company_id: uuid.UUID) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "unknown company")
    return company


def ticker(company: Company) -> str | None:
    return company.securities[0].symbol if getattr(company, "securities", None) else None


def run_out(session: Session, run: ScoreRun) -> ScoreOut:
    company = session.get(Company, run.company_id)
    version = (
        session.get(BenchmarkVersion, run.benchmark_version_id)
        if run.benchmark_version_id
        else None
    )
    return ScoreOut(
        run_id=run.id,
        company_id=run.company_id,
        company=company.common_name,
        ticker=ticker(company),
        sector=company.sector.code if company.sector else None,
        industry=company.industry.code if company.industry else None,
        methodology=run.methodology,
        requested=run.requested,
        label=LABEL[run.methodology],
        overall=None if run.overall is None else f"{run.overall:.2f}",
        unavailable_reason=run.unavailable_reason,
        profile=run.profile,
        pillar_scores=run.pillar_scores,
        weight_coverage=None if run.weight_coverage is None else str(run.weight_coverage),
        confidence=run.confidence,
        peer_count=run.peer_count,
        eligible_peer_count=run.eligible_peer_count,
        benchmark_version=version.code if version else None,
        config_version=run.config_version,
        as_of=run.as_of,
        calculated_at=run.calculated_at,
        input_hash=run.input_hash,
        explanation=run.explanation,
    )


def latest_run(session: Session, company_id: uuid.UUID, method: str) -> ScoreRun | None:
    stmt = select(ScoreRun).where(ScoreRun.company_id == company_id)
    stmt = stmt.where(ScoreRun.requested == method)
    return session.scalar(stmt.order_by(ScoreRun.id.desc()).limit(1))


@router.get("/api/companies/{company_id}/score", response_model=ScoreOut)
def get_score(
    company_id: uuid.UUID,
    session: SessionDep,
    method: Annotated[str, Query(description="AUTO | ABSOLUTE | RELATIVE | HYBRID")] = "AUTO",
) -> ScoreOut:
    """The latest stored score for the methodology asked for (AUTO = the engine's own choice)."""
    method = method.upper()
    if method not in METHODS:
        raise HTTPException(422, f"method must be one of {', '.join(METHODS)}")
    company_or_404(session, company_id)
    run = latest_run(session, company_id, method)
    if run is None:
        raise HTTPException(
            404, "no stored score for this company and method yet; POST .../score/recalculate"
        )
    return run_out(session, run)


@router.get("/api/companies/{company_id}/score/metrics", response_model=list[MetricOut])
def get_metric_scores(
    company_id: uuid.UUID,
    session: SessionDep,
    method: str = "AUTO",
    pillar: str | None = None,
) -> list[MetricOut]:
    """Metric-level scoring of the latest run: actual value, benchmark read, score, evidence."""
    company_or_404(session, company_id)
    run = latest_run(session, company_id, method.upper())
    if run is None:
        raise HTTPException(404, "no stored score for this company and method yet")
    stmt = select(ScoreMetricResult).where(ScoreMetricResult.run_id == run.id)
    if pillar:
        stmt = stmt.where(ScoreMetricResult.pillar == pillar.upper())
    return [
        MetricOut(
            metric=r.metric_code,
            name=r.display_name,
            pillar=r.pillar,
            status=r.status,
            reason=r.reason,
            actual_value=None if r.actual_value is None else f"{r.actual_value:.6g}",
            unit=r.unit,
            score=None if r.normalized_score is None else f"{r.normalized_score:.1f}",
            scoring_kind=r.kind,
            weight=str(r.weight),
            benchmark=r.benchmark,
            period_end=r.period_end,
            basis=r.basis,
            evidence_id=r.evidence_id,
        )
        for r in session.scalars(stmt.order_by(ScoreMetricResult.id))
    ]


@router.get("/api/companies/{company_id}/score/methodology")
def get_methodology(company_id: uuid.UUID, session: SessionDep) -> dict:
    """Which methodology applies and why: peers, eligible peers, profile, rules, hybrid status."""
    company = company_or_404(session, company_id)
    profile = registry.profile_for(
        company.sector.code if company.sector else None,
        company.industry.code if company.industry else None,
    )
    eligible, picks = engine.eligible_peers(session, company, profile)
    n = len(eligible)
    chosen = "RELATIVE" if n >= engine.MIN_ELIGIBLE_PEERS else "ABSOLUTE"
    return {
        "profile": profile.code,
        "peers_in_industry": picks,
        "eligible_peers": eligible,
        "eligible_peer_count": n,
        "preferred": chosen,
        "preferred_reason": (
            f"{n} eligible peer(s): relative scoring needs {engine.MIN_ELIGIBLE_PEERS}"
            if chosen == "ABSOLUTE"
            else f"{n} eligible peers: relative scoring applies (if its coverage rule passes)"
        ),
        "rules": {
            "eligible_peer": (
                f"same industry and statement profile, >= {engine.PEER_MIN_SHARED_METRICS} "
                f"comparable metrics over >= {engine.PEER_MIN_PILLARS} pillars at the same period"
            ),
            "relative_min_weight_coverage_pct": 60,
            "absolute_min_computable_coverage_pct": engine.MIN_COMPUTABLE_COVERAGE,
            "absolute_min_scored_metrics": engine.MIN_SCORED_METRICS,
        },
        "hybrid": {"enabled": engine.HYBRID_ENABLED, **engine.calibration({})},
    }


@router.get("/api/companies/{company_id}/score/history", response_model=list[dict])
def get_score_history(company_id: uuid.UUID, session: SessionDep, limit: int = 50) -> list[dict]:
    company_or_404(session, company_id)
    rows = session.scalars(
        select(ScoreRun)
        .where(ScoreRun.company_id == company_id)
        .order_by(ScoreRun.id.desc())
        .limit(min(limit, 200))
    )
    return [
        {
            "run_id": r.id,
            "methodology": r.methodology,
            "requested": r.requested,
            "overall": None if r.overall is None else f"{r.overall:.2f}",
            "calculated_at": r.calculated_at,
            "as_of": r.as_of,
            "benchmark_version_id": r.benchmark_version_id,
            "input_hash": r.input_hash,
        }
        for r in rows
    ]


@router.get("/api/scores/compare")
def compare(
    session: SessionDep, ids: Annotated[str, Query(description="comma-separated company ids")]
) -> dict:
    """Companies side by side - only if they share a methodology (and so a scale)."""
    try:
        company_ids = [uuid.UUID(x) for x in ids.split(",") if x]
    except ValueError as exc:
        raise HTTPException(422, "ids must be UUIDs") from exc
    if len(company_ids) < 2:
        raise HTTPException(422, "give at least two company ids")
    runs = []
    for cid in company_ids:
        company_or_404(session, cid)
        run = latest_run(session, cid, "AUTO")
        if run is None:
            raise HTTPException(404, f"no stored score for {cid}")
        runs.append(run_out(session, run))
    methods = {r.methodology for r in runs}
    sectors = {r.sector for r in runs}
    issues = []
    if len(methods) > 1:
        issues.append(
            "different methodologies (" + ", ".join(sorted(methods)) + "): scores are on "
            "different scales and are not ranked together"
        )
    if "UNAVAILABLE" in methods:
        issues.append("at least one company has no score")
    if len(sectors) > 1:
        issues.append("different sectors: shown side by side, not ranked")
    comparable = not issues
    ordered = (
        sorted(runs, key=lambda r: -(float(r.overall) if r.overall is not None else -1))
        if comparable
        else runs
    )
    return {
        "comparable": comparable,
        "issues": issues,
        "companies": [r.model_dump(mode="json") for r in ordered],
    }


@router.get("/api/scoring/definitions", response_model=list[dict])
def definitions(session: SessionDep, profile: str | None = None) -> list[dict]:
    stmt = select(MetricDefinition).where(MetricDefinition.version == registry.VERSION)
    if profile:
        stmt = stmt.where(MetricDefinition.profile == profile.upper())
    return [
        {
            "code": d.code,
            "profile": d.profile,
            "pillar": d.pillar,
            "name": d.display_name,
            "formula": d.formula,
            "unit": d.unit,
            "direction": d.direction,
            "kind": d.kind,
            "curve": d.curve,
            "curve_source_class": d.curve_source_class,
            "curve_source_note": d.curve_source_note,
            "calc_metric": d.calc_metric,
            "unavailable_reason": d.unavailable_reason,
            "weight": str(d.weight),
            "version": d.version,
            "effective_date": d.effective_date,
        }
        for d in session.scalars(stmt.order_by(MetricDefinition.profile, MetricDefinition.id))
    ]


@router.get("/api/benchmarks")
def list_benchmarks(
    session: SessionDep,
    scope: str | None = None,
    metric: str | None = None,
    version: str | None = None,
) -> dict:
    """Benchmark distributions (latest version unless one is named) with confidence and counts."""
    ver = (
        session.scalar(select(BenchmarkVersion).where(BenchmarkVersion.code == version))
        if version
        else benchmarks.latest_version(session)
    )
    if ver is None:
        raise HTTPException(404, "no benchmark version")
    stmt = select(BenchmarkStat).where(BenchmarkStat.version_id == ver.id)
    if scope:
        stmt = stmt.where(BenchmarkStat.scope_code == scope)
    if metric:
        stmt = stmt.where(BenchmarkStat.metric_code == metric)
    return {
        "version": ver.code,
        "created_at": ver.created_at,
        "input_hash": ver.input_hash,
        "methodology": ver.methodology,
        "distributions": [
            {
                "id": s.id,
                "scope": f"{s.scope_type}:{s.scope_code}",
                "metric": s.metric_code,
                "observations": s.n_observations,
                "companies": s.n_companies,
                "excluded": s.n_excluded,
                "period": [s.period_from, s.period_to],
                "confidence": s.confidence,
                "stats": s.stats,
                "computed_at": s.computed_at,
            }
            for s in session.scalars(
                stmt.order_by(BenchmarkStat.scope_code, BenchmarkStat.metric_code)
            )
        ],
    }


@router.get("/api/benchmarks/{stat_id}/observations", response_model=list[dict])
def benchmark_observations(stat_id: int, session: SessionDep) -> list[dict]:
    stat = session.get(BenchmarkStat, stat_id)
    if stat is None:
        raise HTTPException(404, "unknown benchmark distribution")
    rows = session.scalars(
        select(BenchmarkObservation).where(
            BenchmarkObservation.version_id == stat.version_id,
            BenchmarkObservation.scope_type == stat.scope_type,
            BenchmarkObservation.scope_code == stat.scope_code,
            BenchmarkObservation.metric_code == stat.metric_code,
        )
    )
    return [
        {
            "company_id": r.company_id,
            "period_end": r.period_end,
            "basis": r.basis,
            "value": f"{r.value:.6g}",
            "included": r.included,
            "exclusion_reason": r.exclusion_reason,
            "evidence_id": r.evidence_id,
        }
        for r in rows
    ]


def require_admin() -> None:
    if not get_settings().scoring_admin_enabled:
        raise HTTPException(
            403, "recomputation is disabled; set SCORING_ADMIN_ENABLED=true to allow it"
        )


@router.post("/api/benchmarks/build", dependencies=[Depends(require_admin)])
def build_benchmarks(session: SessionDep) -> dict:
    version = benchmarks.build(session, notes="built through the API")
    session.commit()
    return {"version": version.code, "input_hash": version.input_hash}


@router.post("/api/companies/{company_id}/score/recalculate", dependencies=[Depends(require_admin)])
def recalculate(company_id: uuid.UUID, session: SessionDep, method: str = "AUTO") -> ScoreOut:
    method = method.upper()
    if method not in METHODS:
        raise HTTPException(422, f"method must be one of {', '.join(METHODS)}")
    company = company_or_404(session, company_id)
    out = engine.score_company(session, company, method)
    session.commit()
    return run_out(session, out.run)
