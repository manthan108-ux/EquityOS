"""Scoring transformations and benchmark statistics (no database)."""

from decimal import Decimal as D

import pytest

from app.scoring import engine, registry, stats
from app.scoring import transform as tf
from app.scoring.registry import PROFILES

# ---- transformations ------------------------------------------------------------------------------


def test_threshold_curve_is_piecewise_linear_and_clamped():
    curve = {"points": [[0, 0], [10, 50], [20, 100]]}
    assert tf.score_threshold(D(5), curve).score == D("25")
    assert tf.score_threshold(D(-50), curve).score == D(0)  # a negative value is not an error
    assert tf.score_threshold(D(10**9), curve).score == D(
        100
    )  # an extreme outlier cannot exceed 100


def test_lower_is_better_curve_descends():
    de = registry.C_DE
    assert tf.score_threshold(D(0), de).score == D(100)
    assert tf.score_threshold(D(3), de).score == D(0)
    assert tf.score_threshold(D("0.5"), de).score > tf.score_threshold(D(2), de).score


def test_optimal_range_penalises_both_sides():
    r = registry.R_CURRENT
    assert tf.score_range(D("1.5"), r).score == D(100)
    assert tf.score_range(D("0.5"), r).score == D(0)
    assert tf.score_range(D(9), r).score == D(0)  # too much idle liquidity is also not good
    assert D(0) < tf.score_range(D("0.95"), r).score < D(100)
    with pytest.raises(tf.NotScorable):
        tf.score_range(D(1), {"zero_low": 3, "full_low": 2, "full_high": 4, "zero_high": 5})


STATS = {"min": 0, "p25": 10, "p50": 20, "p75": 30, "p90": 40, "max": 50}


def test_percentile_scoring_higher_and_lower_and_extremes():
    assert tf.score_percentile(D(20), STATS, "HIGHER").score == D(50)
    assert tf.score_percentile(D(20), STATS, "LOWER").score == D(50)
    assert tf.score_percentile(D(30), STATS, "HIGHER").score == D(75)
    assert tf.score_percentile(D(30), STATS, "LOWER").score == D(25)
    assert tf.score_percentile(D(10**6), STATS, "HIGHER").score == D(100)
    assert tf.score_percentile(D(-(10**6)), STATS, "HIGHER").score == D(0)


def test_degenerate_distribution_is_not_scorable_instead_of_dividing_by_zero():
    flat = {"min": 5, "p25": 5, "p50": 5, "p75": 5, "p90": 5, "max": 5}
    with pytest.raises(tf.NotScorable):
        tf.score_percentile(D(5), flat, "HIGHER")


def test_multiples_on_negative_earnings_are_not_meaningful_rather_than_zero():
    assert tf.not_meaningful("VALUATION.pe_ttm", D(-4)) is not None
    assert tf.not_meaningful("VALUATION.pe_ttm", D(0)) is not None
    assert tf.not_meaningful("VALUATION.pe_ttm", D(20)) is None
    assert tf.not_meaningful("LEVERAGE.debt_to_equity", D(-1)) is not None
    assert tf.not_meaningful("RETURN.roce", D(-5)) is None  # a negative return is just poor


# ---- statistics -----------------------------------------------------------------------------------


def test_percentiles_by_linear_interpolation():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert stats.percentile(xs, 50) == 3.0
    assert stats.percentile(xs, 25) == 2.0
    assert stats.percentile(xs, 90) == pytest.approx(4.6)
    assert stats.percentile([7.0], 90) == 7.0


def test_summary_has_every_required_statistic():
    s = stats.summarize([D(x) for x in (1, 2, 3, 4, 100)])
    assert {"min", "max", "mean", "median", "std", "p25", "p50", "p75", "p90", "n"} <= set(s)
    assert s["min"] == 1 and s["max"] == 100 and s["median"] == 3 and s["n"] == 5


def test_outliers_are_found_by_mad_and_small_samples_are_never_trimmed():
    values = [D(x) for x in (10, 11, 12, 10, 11, 12, 11, 10, 500)]
    kept, dropped = stats.split_outliers(values)
    assert dropped == [8] and len(kept) == 8
    small = [D(x) for x in (1, 2, 1000)]
    assert stats.split_outliers(small) == ([0, 1, 2], [])
    same = [D(5)] * 10  # MAD is 0: nothing can be called an outlier
    assert stats.split_outliers(same)[1] == []


def test_confidence_classification_refuses_small_or_narrow_samples():
    assert stats.classify(11, 5) == "INSUFFICIENT"  # too few observations
    assert stats.classify(40, 3) == "INSUFFICIENT"  # too few companies
    assert stats.classify(12, 4) == "MEDIUM"
    assert stats.classify(30, 8) == "HIGH"


# ---- registry -------------------------------------------------------------------------------------


def test_every_profile_is_complete_and_documents_what_it_cannot_measure():
    for profile in PROFILES.values():
        assert sum(profile.weights.values()) == 100 and set(profile.weights) == set(
            registry.PILLARS
        )
        assert all(len(profile.rationale[p]) > 20 for p in registry.PILLARS)
        codes = [s.code for s in profile.metrics]
        assert len(codes) == len(set(codes))
        for s in profile.metrics:
            assert s.pillar in registry.PILLARS
            if s.calc is None:
                assert s.unavailable, s.code  # a metric without a source says why
            if s.kind in ("THRESHOLD", "RANGE"):
                assert s.curve and s.curve.get("why")
        pillars_with_metrics = {s.pillar for s in profile.metrics}
        assert pillars_with_metrics == set(registry.PILLARS), profile.code


def test_profiles_follow_the_specification():
    names = {s.code for s in PROFILES["BANK"].metrics}
    # banks never use industrial debt/equity or EBITDA margin
    assert "LEVERAGE.debt_to_equity" not in names and "MARGIN.ebitda_margin" not in names
    assert {"ASSET_QUALITY.gross_npa", "ASSET_QUALITY.net_npa", "CAPITAL.capital_adequacy"} <= names
    it = {s.code: s for s in PROFILES["IT_SERVICES"].metrics}
    assert it["GROWTH.constant_currency"].calc is None  # reported growth is not constant currency
    assert "GROWTH.revenue_yoy" in it
    conglomerate = {s.code: s for s in PROFILES["CONGLOMERATE"].metrics}
    assert conglomerate["VALUATION.sotp"].calc is None  # never manufactured
    assert registry.profile_for("fmcg", "diversified_fmcg").code == "FMCG"
    assert registry.profile_for("automobiles", "two_wheelers").code == "AUTOMOBILES"
    assert registry.profile_for("oil_and_gas", "diversified_conglomerate").code == "CONGLOMERATE"
    assert registry.profile_for("oil_and_gas", "refineries_and_marketing").code == "GENERIC_NONFIN"
    assert registry.profile_for("banking", "public_sector_bank").code == "BANK"


def test_every_convention_curve_carries_its_rationale():
    for profile in PROFILES.values():
        for s in profile.metrics:
            if s.curve:
                assert len(s.curve["why"]) > 20, s.code


# ---- hybrid ---------------------------------------------------------------------------------------


def test_spearman_rank_correlation():
    assert engine.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert engine.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert engine.spearman([1, 2], [1, 2]) is None


def test_hybrid_stays_disabled_without_enough_companies_in_every_group():
    small = {"IT": [(60.0, 62.0), (50.0, 51.0), (40.0, 41.0), (30.0, 33.0), (20.0, 21.0)]}
    result = engine.calibration(small)
    assert result["justified"] is False  # perfectly correlated but only 5 companies
    assert result["groups"]["IT"]["companies"] == 5 and result["groups"]["IT"]["ok"] is False
    big = {"IT": [(float(i), float(i) + 1) for i in range(10)]}
    assert engine.calibration(big)["justified"] is True
    anti = {"IT": [(float(i), float(10 - i)) for i in range(10)]}
    assert engine.calibration(anti)["justified"] is False
    assert engine.calibration({})["justified"] is False
    assert engine.HYBRID_ENABLED is False
