"""Methodology selection, absolute scoring, benchmarks and the scoring API against a real database."""

from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_session
from app.main import create_app
from app.models import BenchmarkObservation, BenchmarkStat, ScoreMetricResult, ScoreRun
from app.scoring import benchmarks, engine, registry
from app.sector.engine import all_candidates
from tests.test_sector import FY26, TODAY, metric, mk_company, run, seeded, size  # noqa: F401

FY25, FY24 = date(2025, 3, 31), date(2024, 3, 31)

IT_ALL = {  # every calculable IT metric, with a plausible value
    "GROWTH.revenue_yoy": 12,
    "GROWTH.net_profit_yoy": 10,
    "GROWTH.eps_yoy": 11,
    "MARGIN.ebit_margin": 22,
    "MARGIN.net_margin": 18,
    "RETURN.roce": 30,
    "RETURN.roe_closing": 25,
    "CASH.cfo_to_net_profit": 1.0,
    "LIQUIDITY.current_ratio": 2.0,
    "LEVERAGE.debt_to_equity": 0.1,
    "VALUATION.pe_ttm": 25,
    "EFFICIENCY.asset_turnover": 0.9,
}


IT_INDUSTRY = "it_services_and_consulting"


def add_company(session, name, values=None, years=(FY26,), scale=1.0, industry=IT_INDUSTRY):
    company = mk_company(session, name, industry=industry)
    size(session, company, 1000)
    for code, base in (values or IT_ALL).items():
        for i, end in enumerate(years):
            if code.startswith("VALUATION.") and i:
                continue  # a price-date metric exists once
            metric(session, company, code, round(base * scale * (1 + 0.02 * i), 4), end=end)
    return company


def peers(session, n, **kw):
    return [add_company(session, f"P{i}", scale=0.8 + 0.1 * i, **kw) for i in range(n)]


def full_history(session, n=5, industry=None):
    """n companies with three years of data, so benchmarks can reach MEDIUM.

    By default they are in the IT *sector* without an industry: they feed the sector benchmark but
    are not same-industry peers of the company under test.
    """
    return [
        add_company(
            session, f"H{i}", years=(FY26, FY25, FY24), scale=0.8 + 0.1 * i, industry=industry
        )
        for i in range(n)
    ]


def pool_run(session, company, method=engine.AUTO):
    return engine.score_company(
        session, company, method, as_of=TODAY, pool=all_candidates(session), persist=True
    )


# ---- methodology by eligible peers ----------------------------------------------------------------


@pytest.mark.parametrize("n_peers", [0, 1, 2])
def test_fewer_than_three_eligible_peers_uses_absolute(seeded, n_peers):  # noqa: F811
    full_history(seeded)  # sector benchmark only: not peers
    target = add_company(seeded, "Target", years=(FY26, FY25, FY24))
    peers(seeded, n_peers)
    benchmarks.build(seeded, today=TODAY)
    out = pool_run(seeded, target)
    assert out.methodology == engine.ABSOLUTE and out.overall is not None
    assert len(out.eligible_peers) == n_peers
    if n_peers:
        assert "peer_context" in out.explanation  # peers are shown as context, not scored against
    else:
        assert "peer_context" not in out.explanation


def test_a_peer_with_almost_no_data_is_not_eligible(seeded):  # noqa: F811
    target = add_company(seeded, "Target", years=(FY26, FY25, FY24))
    add_company(seeded, "Thin", values={"GROWTH.revenue_yoy": 5})
    profile = registry.PROFILES["IT_SERVICES"]
    eligible, picks = engine.eligible_peers(seeded, target, profile)
    assert "Thin" in picks and "Thin" not in eligible


def test_three_eligible_peers_use_the_relative_score_unchanged(seeded):  # noqa: F811
    target = add_company(seeded, "Target")
    peers(seeded, 3)
    phase8 = run(seeded, target)
    assert phase8.overall is not None
    out = pool_run(seeded, target)
    assert out.methodology == engine.RELATIVE
    assert out.overall == D(str(phase8.peer_set.summary["overall"]))
    assert out.pillar_scores == phase8.peer_set.summary["pillar_scores"]
    assert len(out.eligible_peers) == 3


def test_relative_is_withheld_not_absolute_when_it_is_explicitly_requested_with_two_peers(seeded):  # noqa: F811
    target = add_company(seeded, "Target")
    peers(seeded, 2)
    out = pool_run(seeded, target, engine.RELATIVE)
    assert out.methodology == engine.UNAVAILABLE and "needs" in out.reason
    assert out.overall is None


def test_hybrid_is_refused_with_the_reason(seeded):  # noqa: F811
    target = add_company(seeded, "Target")
    out = pool_run(seeded, target, engine.HYBRID)
    assert out.methodology == engine.UNAVAILABLE and "disabled" in out.reason
    assert out.overall is None


# ---- absolute scoring -----------------------------------------------------------------------------


def test_absolute_score_with_benchmarks_is_explainable_and_traceable(seeded):  # noqa: F811
    full_history(seeded)
    target = add_company(seeded, "Target", years=(FY26, FY25, FY24))
    version = benchmarks.build(seeded, today=TODAY)
    out = engine.score_absolute(seeded, target, as_of=TODAY, version_id=version.id)
    assert out.overall is not None and out.methodology == engine.ABSOLUTE
    assert D(0) <= out.overall <= D(100)
    assert out.benchmark_version == version.id and out.config_version == registry.VERSION
    kinds = {
        m.spec.code: (m.benchmark or {}).get("type") for m in out.metrics if m.status == "SCORED"
    }
    assert kinds["MARGIN.net_margin"] == "EMPIRICAL"  # five companies x three years
    assert kinds["LEVERAGE.debt_to_equity"] == "CONVENTION"
    scored = next(m for m in out.metrics if m.spec.code == "MARGIN.net_margin")
    assert scored.value == D("18.0000") or scored.value == D(18)
    assert scored.record.id  # evidence reference
    assert out.confidence in ("LOW", "MEDIUM", "HIGH")
    assert out.explanation["missing"] and out.explanation["assumptions"]
    assert any("convention" in a.lower() for a in out.explanation["assumptions"])
    # stored run carries the benchmark version and metric-level results with evidence ids
    run_row = engine.store(seeded, out, TODAY)
    assert run_row.benchmark_version_id == version.id
    rows = list(
        seeded.scalars(select(ScoreMetricResult).where(ScoreMetricResult.run_id == run_row.id))
    )
    assert any(r.evidence_id and r.status == "SCORED" for r in rows)
    assert all(r.normalized_score is None for r in rows if r.status != "SCORED")  # never zero


def test_missing_data_is_a_gap_and_never_a_zero(seeded):  # noqa: F811
    full_history(seeded)
    version = benchmarks.build(seeded, today=TODAY)
    complete = add_company(seeded, "Complete", years=(FY26, FY25, FY24))
    values = {k: v for k, v in IT_ALL.items() if k != "RETURN.roce"}
    partial = add_company(seeded, "Partial", values=values, years=(FY26, FY25, FY24))
    a = engine.score_absolute(seeded, complete, as_of=TODAY, version_id=version.id)
    b = engine.score_absolute(seeded, partial, as_of=TODAY, version_id=version.id)
    gap = next(m for m in b.metrics if m.spec.code == "RETURN.roce")
    assert gap.status == "GAP" and gap.score is None
    # the profitability pillar of the partial company is the mean of what exists, not dragged to 0
    assert D(b.pillar_scores["PROFITABILITY"]) > D(0)
    assert abs(D(a.pillar_scores["PROFITABILITY"]) - D(b.pillar_scores["PROFITABILITY"])) < D(30)


def test_a_company_with_too_little_data_gets_no_score_and_a_reason(seeded):  # noqa: F811
    thin = add_company(seeded, "Thin", values={"GROWTH.revenue_yoy": 10, "MARGIN.net_margin": 9})
    out = engine.score_absolute(seeded, thin, as_of=TODAY)
    assert out.methodology == engine.UNAVAILABLE and out.overall is None
    assert "metric(s) scored" in out.reason and "critical pillar" in out.reason


def test_stale_financials_make_the_score_unavailable(seeded):  # noqa: F811
    full_history(seeded)
    version = benchmarks.build(seeded, today=TODAY)
    old = add_company(seeded, "Old", years=(FY26, FY25, FY24))
    out = engine.score_absolute(seeded, old, as_of=date(2028, 6, 1), version_id=version.id)
    assert out.methodology == engine.UNAVAILABLE and "older than" in out.reason


def test_negative_earnings_multiple_is_excluded_not_scored_as_poor(seeded):  # noqa: F811
    values = {**IT_ALL, "VALUATION.pe_ttm": -12}
    company = add_company(seeded, "Loss", values=values, years=(FY26, FY25, FY24))
    out = engine.score_absolute(seeded, company, as_of=TODAY)
    pe = next(m for m in out.metrics if m.spec.code == "VALUATION.pe_ttm")
    assert pe.status == "EXCLUDED" and "not meaningful" in pe.reason and pe.score is None


def test_scores_are_reproducible(seeded):  # noqa: F811
    full_history(seeded)
    version = benchmarks.build(seeded, today=TODAY)
    company = add_company(seeded, "Target", years=(FY26, FY25, FY24))
    a = engine.score_absolute(seeded, company, as_of=TODAY, version_id=version.id)
    b = engine.score_absolute(seeded, company, as_of=TODAY, version_id=version.id)
    assert a.overall == b.overall and a.input_hash == b.input_hash


# ---- benchmarks -----------------------------------------------------------------------------------


def stat(session, version, metric_code, scope="it_services"):
    return session.scalar(
        select(BenchmarkStat).where(
            BenchmarkStat.version_id == version.id,
            BenchmarkStat.scope_code == scope,
            BenchmarkStat.metric_code == metric_code,
        )
    )


def test_benchmarks_need_enough_companies_and_observations(seeded):  # noqa: F811
    add_company(seeded, "A", years=(FY26, FY25, FY24))
    add_company(seeded, "B", years=(FY26, FY25, FY24))
    version = benchmarks.build(seeded, today=TODAY)
    s = stat(seeded, version, "MARGIN.net_margin")
    assert s.confidence == "INSUFFICIENT" and s.stats is None and s.n_companies == 2


def test_a_representative_sample_gives_a_full_distribution_with_provenance(seeded):  # noqa: F811
    full_history(seeded)
    version = benchmarks.build(seeded, today=TODAY)
    s = stat(seeded, version, "MARGIN.net_margin")
    assert s.confidence == "MEDIUM" and s.n_observations == 15 and s.n_companies == 5
    assert {"min", "max", "mean", "median", "std", "p25", "p50", "p75", "p90"} <= set(s.stats)
    assert s.period_from == FY24 and s.period_to == FY26
    assert "outliers" in s.method and s.source and s.computed_at
    obs = list(
        seeded.scalars(
            select(BenchmarkObservation).where(BenchmarkObservation.version_id == version.id)
        )
    )
    assert all(o.evidence_id for o in obs)  # every observation traces to evidence


def test_outliers_are_excluded_from_the_statistics_but_kept_with_the_reason(seeded):  # noqa: F811
    full_history(seeded)
    odd = add_company(seeded, "Odd", values={"MARGIN.net_margin": 9000}, years=(FY26, FY25, FY24))
    version = benchmarks.build(seeded, today=TODAY)
    s = stat(seeded, version, "MARGIN.net_margin")
    assert s.stats["max"] < 100 and s.n_excluded >= 1
    flagged = list(
        seeded.scalars(
            select(BenchmarkObservation).where(
                BenchmarkObservation.version_id == version.id,
                BenchmarkObservation.company_id == odd.id,
                BenchmarkObservation.scope_type == "SECTOR",
                BenchmarkObservation.metric_code == "MARGIN.net_margin",
            )
        )
    )
    assert flagged and all(not o.included and "outlier" in o.exclusion_reason for o in flagged)


def test_identical_inputs_reproduce_the_same_version_and_new_data_makes_a_new_one(seeded):  # noqa: F811
    full_history(seeded)
    v1 = benchmarks.build(seeded, today=TODAY)
    again = benchmarks.build(seeded, today=TODAY)
    assert again.id == v1.id  # reproducible
    before = stat(seeded, v1, "MARGIN.net_margin").stats
    add_company(seeded, "New", years=(FY26, FY25, FY24), scale=2.0)
    v2 = benchmarks.build(seeded, today=TODAY)
    assert v2.id != v1.id and v2.code != v1.code
    assert stat(seeded, v1, "MARGIN.net_margin").stats == before  # the old version is untouched
    assert stat(seeded, v2, "MARGIN.net_margin").n_companies == 6


# ---- API ------------------------------------------------------------------------------------------


@pytest.fixture
def client(seeded):  # noqa: F811
    app = create_app()
    app.dependency_overrides[get_session] = lambda: seeded
    return TestClient(app)


def test_score_api_exposes_methodology_metrics_and_labels(seeded, client):  # noqa: F811
    full_history(seeded)
    benchmarks.build(seeded, today=TODAY)
    company = add_company(seeded, "Target", years=(FY26, FY25, FY24))
    assert client.get(f"/api/companies/{company.id}/score").status_code == 404
    engine.score_company(seeded, company, engine.ABSOLUTE, as_of=TODAY)
    body = client.get(f"/api/companies/{company.id}/score?method=ABSOLUTE").json()
    assert body["methodology"] == "ABSOLUTE" and body["label"] == "Absolute Fundamental Score"
    assert body["benchmark_version"].startswith("bench.") and body["overall"]
    assert set(body["pillar_scores"]) <= set(registry.PILLARS)
    metrics = client.get(
        f"/api/companies/{company.id}/score/metrics?method=ABSOLUTE&pillar=profitability"
    ).json()
    assert metrics and all(m["pillar"] == "PROFITABILITY" for m in metrics)
    assert any(m["evidence_id"] and m["benchmark"] for m in metrics if m["status"] == "SCORED")
    method = client.get(f"/api/companies/{company.id}/score/methodology").json()
    assert method["hybrid"]["enabled"] is False and method["profile"] == "IT_SERVICES"
    assert client.get(f"/api/companies/{company.id}/score/history").json()
    assert client.get(f"/api/companies/{company.id}/score?method=bogus").status_code == 422


def test_comparison_refuses_to_rank_different_methodologies(seeded, client):  # noqa: F811
    a = add_company(seeded, "A", years=(FY26, FY25, FY24))
    b = add_company(seeded, "B", years=(FY26, FY25, FY24))
    engine.score_company(seeded, a, engine.ABSOLUTE, as_of=TODAY)
    engine.score_company(seeded, b, engine.HYBRID, as_of=TODAY)  # unavailable
    # AUTO runs are what compare reads
    engine.score_company(seeded, a, engine.AUTO, as_of=TODAY, pool=all_candidates(seeded))
    engine.score_company(seeded, b, engine.AUTO, as_of=TODAY, pool=all_candidates(seeded))
    body = client.get(f"/api/scores/compare?ids={a.id},{b.id}").json()
    assert body["comparable"] is False or body["issues"] == []
    assert client.get(f"/api/scores/compare?ids={a.id}").status_code == 422


def test_recomputation_endpoints_are_off_by_default(seeded, client):  # noqa: F811
    assert get_settings().scoring_admin_enabled is False
    assert client.post("/api/benchmarks/build").status_code == 403
    company = add_company(seeded, "X")
    assert client.post(f"/api/companies/{company.id}/score/recalculate").status_code == 403


def test_benchmark_and_definition_endpoints(seeded, client):  # noqa: F811
    full_history(seeded)
    benchmarks.build(seeded, today=TODAY)
    registry.seed(seeded)
    body = client.get("/api/benchmarks?scope=it_services&metric=MARGIN.net_margin").json()
    assert (
        body["version"].startswith("bench.") and body["distributions"][0]["confidence"] == "MEDIUM"
    )
    stat_id = body["distributions"][0]["id"]
    assert len(client.get(f"/api/benchmarks/{stat_id}/observations").json()) == 15
    defs = client.get("/api/scoring/definitions?profile=BANK").json()
    assert {d["code"] for d in defs} >= {"ASSET_QUALITY.gross_npa", "RATIO.cost_to_income"}
    assert all(d["unavailable_reason"] for d in defs if d["calc_metric"] is None)


def test_every_stored_run_is_immutable_history(seeded):  # noqa: F811
    company = add_company(seeded, "Target", years=(FY26, FY25, FY24))
    engine.score_company(seeded, company, engine.ABSOLUTE, as_of=TODAY)
    engine.score_company(seeded, company, engine.ABSOLUTE, as_of=TODAY)
    runs = list(seeded.scalars(select(ScoreRun).where(ScoreRun.company_id == company.id)))
    assert len(runs) == 2 and runs[0].id != runs[1].id


def test_bank_specific_gaps_are_reported_and_never_replaced_by_non_bank_metrics(seeded):  # noqa: F811
    from app.sector.bank_coverage import CONCEPTS, coverage

    bank = mk_company(seeded, "SomeBank", "banking", "private_sector_bank")
    size(seeded, bank, 1000, bank=True)  # interest earned only
    result = coverage(seeded)["SomeBank"]
    assert result["interest_earned"] is True
    assert result["interest_expended"] is False and result["gross_npa"] is False
    assert set(result) == {c.code for c in CONCEPTS}
    # the bank profile has no generic industrial metrics to fall back on
    codes = {s.code for s in registry.PROFILES["BANK"].metrics}
    assert not codes & {"LEVERAGE.debt_to_equity", "MARGIN.ebitda_margin", "RATIO.interest_cover"}


def test_bank_coverage_reads_the_evidence_codes_the_disclosure_reader_really_writes(seeded):  # noqa: F811
    """Regression: the report once looked for ASSET_QUALITY.* codes and so denied that a bank with
    validated RATIOS.gross_npa_ratio / capital_adequacy_ratio evidence had them."""
    from app.sector.bank_coverage import coverage
    from tests.research_helpers import evidence

    bank = mk_company(seeded, "RatioBank", "banking", "private_sector_bank")
    for code in (
        "RATIOS.gross_npa_ratio",
        "RATIOS.net_npa_ratio",
        "RATIOS.capital_adequacy_ratio",
        "RATIOS.net_interest_margin",
    ):
        evidence(seeded, bank, code, 1.5, kind="DISCLOSURE", unit="percent", basis="STANDALONE")
    result = coverage(seeded)["RatioBank"]
    assert (
        result["gross_npa"] and result["net_npa"] and result["capital_adequacy"] and result["nim"]
    )
    assert result["casa"] is False
