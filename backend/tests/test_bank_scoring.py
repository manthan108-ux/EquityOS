"""Bank key figures (NIM, NPA, capital adequacy, CASA) in the absolute score, and what stops them."""

from datetime import date, timedelta
from decimal import Decimal
from itertools import count

from app.scoring import disclosures, engine, registry
from tests.research_helpers import evidence
from tests.test_sector import FY26, TODAY, mk_company, seeded  # noqa: F401

D = Decimal
_n = count()
Q2 = date(2026, 9, 30)
FIGURES = {
    "RATIOS.gross_npa_ratio": 1.2,
    "RATIOS.net_npa_ratio": 0.3,
    "RATIOS.capital_adequacy_ratio": 16.0,
    "RATIOS.net_interest_margin": 3.4,
    "RATIOS.casa_ratio": 38.0,
}
CALC = {  # the calculated bank metrics, one plausible value each
    "GROWTH.advances_yoy": 12,
    "GROWTH.deposits_yoy": 10,
    "GROWTH.net_interest_income_yoy": 9,
    "GROWTH.net_profit_yoy": 14,
    "RETURN.roa_closing": 1.3,
    "RETURN.roe_closing": 14,
    "LEVERAGE.equity_multiplier": 11,
    "RATIO.provisions_to_ppop": 20,
    "RATIO.advances_to_deposits": 80,
    "VALUATION.price_to_book": 2.5,
    "VALUATION.pe_annual": 15,
    "RATIO.cost_to_income": 48,
    "MARGIN.ppop_margin": 40,
}


def bank(session, name="ScoreBank", *, figures=None, drop=(), calc=True, end=Q2):
    c = mk_company(session, name, "banking", "private_sector_bank")
    if calc:
        for spec in registry.PROFILES["BANK"].metrics:
            if spec.calc in CALC and not spec.disclosure:
                evidence(
                    session, c, spec.calc, CALC[spec.calc], kind="CALCULATION", end=FY26,
                    unit="percent",
                )  # fmt: skip
    for code, value in (FIGURES if figures is None else figures).items():
        if code not in drop:
            evidence(session, c, code, value, kind="DISCLOSURE", unit="percent", end=end,
                     ptype="QUARTERLY", basis=None)  # fmt: skip
    return c


def extra(session, company, code, value, *, dimension="", end=Q2):
    """A further disclosure row of the same figure (no resolution: a rival record)."""
    template = evidence(
        session, company, code, value, kind="DISCLOSURE", unit="percent", end=date(1999, 1, 1) + timedelta(days=next(_n)),
        ptype="QUARTERLY", basis=None,
    )  # fmt: skip
    template.period_end, template.dimension = end, dimension
    session.flush()
    return template


def score(session, company):
    return engine.score_absolute(session, company, as_of=TODAY)


def metric(out, code):
    return next(m for m in out.metrics if m.spec.code == code)


def test_every_bank_key_figure_is_scored_from_its_disclosure(seeded):  # noqa: F811
    out = score(seeded, bank(seeded))
    for code in (
        "ASSET_QUALITY.gross_npa",
        "ASSET_QUALITY.net_npa",
        "CAPITAL.capital_adequacy",
        "RATIO.net_interest_margin",
        "RATIO.casa_ratio",
    ):
        m = metric(out, code)
        assert m.status == "SCORED", (code, m.reason)
        assert m.benchmark["type"] == "CONVENTION"
        assert "disclosed for 2026-09-30" in m.benchmark["disclosure"]
        assert "AS_REPORTED" in m.benchmark["disclosure"]
    assert out.overall is not None
    assert (
        metric(out, "ASSET_QUALITY.gross_npa").score
        > metric(out, "ASSET_QUALITY.net_npa").score - 50
    )


def test_a_worse_asset_quality_gives_a_lower_score_than_a_better_one(seeded):  # noqa: F811
    good = score(seeded, bank(seeded, "GoodBank"))
    weak = score(
        seeded,
        bank(
            seeded,
            "WeakBank",
            figures={**FIGURES, "RATIOS.gross_npa_ratio": 6.0, "RATIOS.net_npa_ratio": 2.5},
        ),
    )
    assert (
        metric(weak, "ASSET_QUALITY.gross_npa").score
        < metric(good, "ASSET_QUALITY.gross_npa").score
    )
    assert weak.overall < good.overall


def test_without_gross_npa_no_score_is_given_and_the_reason_says_why(seeded):  # noqa: F811
    out = score(seeded, bank(seeded, drop=("RATIOS.gross_npa_ratio",)))
    assert out.methodology == engine.UNAVAILABLE and out.overall is None
    assert "critical metric not available: Gross NPA ratio" in out.reason
    assert "no disclosed value" in out.reason
    assert metric(out, "ASSET_QUALITY.gross_npa").status == "GAP"  # a gap, never a zero


def test_without_capital_adequacy_no_score_is_given(seeded):  # noqa: F811
    out = score(seeded, bank(seeded, drop=("RATIOS.capital_adequacy_ratio",)))
    assert out.overall is None and "Capital adequacy ratio" in out.reason


def test_missing_important_but_not_critical_figures_still_scores_with_lower_coverage(seeded):  # noqa: F811
    full = score(seeded, bank(seeded, "FullBank"))
    part = score(
        seeded, bank(seeded, "PartBank", drop=("RATIOS.casa_ratio", "RATIOS.net_interest_margin"))
    )
    assert part.overall is not None
    assert part.coverage < full.coverage
    assert metric(part, "RATIO.casa_ratio").status == "GAP"
    assert any(x["metric"] == "CASA ratio" for x in part.explanation["missing"])


def test_an_implausible_figure_is_refused_not_scored(seeded):  # noqa: F811
    out = score(seeded, bank(seeded, figures={**FIGURES, "RATIOS.gross_npa_ratio": 123.0}))
    m = metric(out, "ASSET_QUALITY.gross_npa")
    assert m.status == "GAP" and "outside the plausible range" in m.reason
    assert out.overall is None  # critical, so the score is withheld


def test_a_negative_ratio_is_refused(seeded):  # noqa: F811
    out = score(seeded, bank(seeded, figures={**FIGURES, "RATIOS.capital_adequacy_ratio": -4}))
    assert metric(out, "CAPITAL.capital_adequacy").status == "GAP" and out.overall is None


def test_a_non_percent_unit_is_refused(seeded):  # noqa: F811
    c = bank(seeded, drop=("RATIOS.gross_npa_ratio",))
    evidence(seeded, c, "RATIOS.gross_npa_ratio", 1.2, kind="DISCLOSURE", unit="inr", end=Q2,
             ptype="QUARTERLY", basis=None)  # fmt: skip
    m = metric(score(seeded, c), "ASSET_QUALITY.gross_npa")
    assert m.status == "GAP" and "is not percent" in m.reason


def test_net_npa_above_gross_npa_makes_both_unusable(seeded):  # noqa: F811
    out = score(
        seeded,
        bank(
            seeded, figures={**FIGURES, "RATIOS.gross_npa_ratio": 1.0, "RATIOS.net_npa_ratio": 2.0}
        ),
    )
    for code in ("ASSET_QUALITY.gross_npa", "ASSET_QUALITY.net_npa"):
        m = metric(out, code)
        assert m.status == "GAP" and "inconsistent" in m.reason and m.score is None
    assert out.overall is None


def test_a_stale_disclosure_is_not_used(seeded):  # noqa: F811
    old = Q2 - timedelta(days=400)
    out = score(seeded, bank(seeded, end=old))
    m = metric(out, "ASSET_QUALITY.gross_npa")
    assert m.status == "GAP" and "older than 200 days" in m.reason
    assert out.overall is None


def test_a_disclosure_dated_after_the_scoring_date_is_ignored(seeded):  # noqa: F811
    out = score(seeded, bank(seeded, end=TODAY + timedelta(days=30)))
    assert metric(out, "ASSET_QUALITY.gross_npa").status == "GAP"


def test_two_disagreeing_values_for_the_same_period_are_a_conflict(seeded):  # noqa: F811
    c = bank(seeded)
    extra(seeded, c, "RATIOS.gross_npa_ratio", 1.9)
    m = metric(score(seeded, c), "ASSET_QUALITY.gross_npa")
    assert m.status == "GAP" and "conflicting disclosed values" in m.reason


def test_the_least_qualified_nim_definition_is_preferred_and_named(seeded):  # noqa: F811
    c = bank(seeded, drop=("RATIOS.net_interest_margin",))
    for dim, value in (("domestic", 3.6), ("total_assets", 3.2), ("", 3.4)):
        extra(seeded, c, "RATIOS.net_interest_margin", value, dimension=dim)
    seeded.flush()
    m = metric(score(seeded, c), "RATIO.net_interest_margin")
    assert m.status == "SCORED" and m.value == D("3.4")


def test_only_a_qualified_nim_is_used_and_labelled_with_its_definition(seeded):  # noqa: F811
    c = bank(seeded, drop=("RATIOS.net_interest_margin",))
    extra(seeded, c, "RATIOS.net_interest_margin", 3.1, dimension="domestic")
    seeded.flush()
    m = metric(score(seeded, c), "RATIO.net_interest_margin")
    assert m.status == "SCORED" and "domestic" in m.benchmark["disclosure"]


def test_the_newest_period_wins_over_an_older_one(seeded):  # noqa: F811
    c = bank(seeded)
    evidence(seeded, c, "RATIOS.gross_npa_ratio", 2.8, kind="DISCLOSURE", unit="percent",
             end=date(2026, 6, 30), ptype="QUARTERLY", basis=None)  # fmt: skip
    m = metric(score(seeded, c), "ASSET_QUALITY.gross_npa")
    assert m.value == D("1.2")


def test_the_pick_reports_what_it_used(seeded):  # noqa: F811
    c = bank(seeded)
    p = disclosures.pick(seeded, c.id, ("RATIOS.gross_npa_ratio",), as_of=TODAY, bounds=(0, 40),
                         max_age_days=200)  # fmt: skip
    assert p.record is not None and p.reason is None and "2026-09-30" in p.note


def test_a_bank_with_no_disclosures_at_all_is_withheld_not_scored_on_the_rest(seeded):  # noqa: F811
    out = score(seeded, bank(seeded, figures={}))
    assert out.overall is None and out.methodology == engine.UNAVAILABLE
    assert "Gross NPA ratio" in out.reason and "Capital adequacy ratio" in out.reason


def test_other_profiles_are_not_given_bank_critical_metrics(seeded):  # noqa: F811
    assert registry.PROFILES["IT_SERVICES"].critical_metrics == ()
    assert registry.PROFILES["BANK"].critical_metrics == (
        "ASSET_QUALITY.gross_npa",
        "CAPITAL.capital_adequacy",
    )
