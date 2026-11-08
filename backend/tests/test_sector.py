"""Sector and peer engine: pure logic, then the engine against a real database."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import get_session
from app.evidence.engine import validate_company
from app.main import create_app
from app.models import (
    Company,
    EvidenceClass,
    EvidenceLink,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    Industry,
    Peer,
    Quality,
    ResolutionStatus,
    Sector,
    SectorWeightSet,
    Severity,
    WeightAdjustment,
)
from app.sector import profiles
from app.sector.core import (
    HIGHER,
    LOWER,
    MIN_WEIGHT_COVERAGE,
    Candidate,
    favourable_share,
    median,
    score,
    select_peers,
)
from app.sector.engine import analyze_company, ensure_weight_sets
from app.sector.store import recompute

D = Decimal
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
TODAY = date(2026, 11, 8)
FY26 = date(2026, 3, 31)
PRICE_DAY = date(2026, 11, 6)
CR = D(10) ** 7


# ---- pure logic ----------------------------------------------------------------------------------


def cand(
    name, sector="it_services", industry="it_services_and_consulting", profile="NONFIN", size=100
):
    return Candidate(name.lower(), name, sector, industry, profile, D(size) if size else None)


def test_peers_are_same_profile_and_same_sector_closest_in_size_first():
    subject = cand("Subject", size=100)
    pool = [
        subject,
        cand("Huge", size=10_000),
        cand("Close", size=110),
        cand("Bank", sector="banking", industry="private_sector_bank", profile="BANK"),
        cand("Other sector", sector="fmcg", industry="x"),
        cand("Other industry", industry="it_products", size=100),
        cand("No sector", sector=None),
        cand("Unknown size", size=None),
    ]
    picks, rejected = select_peers(subject, pool)
    assert [p.candidate.name for p in picks] == ["Close", "Huge", "Unknown size", "Other industry"]
    assert [p.tier for p in picks] == ["SAME_INDUSTRY"] * 3 + ["SAME_SECTOR"]
    assert picks[0].size_ratio == D("1.10") and "1.10x" in picks[0].reason
    assert "size unknown" in picks[2].reason
    why = {c.name: r for c, r in rejected}
    assert "different statement profile" in why["Bank"]
    assert "different sector" in why["Other sector"] and "no sector" in why["No sector"]
    assert "Subject" not in why


def test_a_company_without_a_sector_has_no_peers_and_the_reason_is_given():
    picks, rejected = select_peers(cand("A", sector=None), [cand("A", sector=None), cand("B")])
    assert picks == [] and rejected[0][1] == "the company has no sector assigned"


def test_peer_count_is_capped_and_the_rest_are_reported():
    subject = cand("S")
    pool = [subject, *[cand(f"P{i}", size=100 + i) for i in range(12)]]
    picks, rejected = select_peers(subject, pool, max_peers=8)
    assert len(picks) == 8 and len(rejected) == 4
    assert all("beyond the 8 closest" in r for _, r in rejected)


def test_favourable_share_directions_and_ties():
    peers = [D(1), D(2), D(3), D(4)]
    assert favourable_share(D(5), peers, HIGHER) == D(100)
    assert favourable_share(D("2.5"), peers, HIGHER) == D(50)
    assert favourable_share(D("2.5"), peers, LOWER) == D(50)
    assert favourable_share(D(0), peers, LOWER) == D(100)
    assert favourable_share(D(2), peers, HIGHER) == D("37.5")  # beats 1, ties 2 (half)
    with pytest.raises(ValueError):
        favourable_share(D(1), [], HIGHER)


def test_median_odd_and_even():
    assert median([D(3), D(1), D(2)]) == D(2)
    assert median([D(4), D(1), D(2), D(3)]) == D("2.5")


def test_score_renormalises_over_pillars_with_data_and_records_it():
    base = {
        "GROWTH": 20,
        "PROFITABILITY": 30,
        "RETURNS": 30,
        "BALANCE_SHEET": 10,
        "VALUATION": 5,
        "MOMENTUM": 5,
    }
    ranks = {
        "GROWTH": [D(50), D(100)],
        "PROFITABILITY": [D(80)],
        "RETURNS": [D(60)],
        "BALANCE_SHEET": [],
        "VALUATION": [],
        "MOMENTUM": [],
    }
    card = score(ranks, base)
    assert card.pillar_scores == {"GROWTH": D(75), "PROFITABILITY": D(80), "RETURNS": D(60)}
    assert card.coverage == D(80)
    assert sum(card.weights.values()) == D(100)
    assert card.weights["GROWTH"] == D(25) and card.weights["RETURNS"] == D(37.5)
    # 75*.25 + 80*.375 + 60*.375 = 18.75 + 30 + 22.5
    assert card.overall == D("71.25")
    dropped = {c.pillar for c in card.changes if c.adjusted == 0}
    assert dropped == {"BALANCE_SHEET", "VALUATION", "MOMENTUM"}
    assert card.withheld is None


def test_no_overall_score_when_too_little_of_the_weight_has_data():
    base = {p: 100 // 6 for p in profiles.PILLARS}
    base["GROWTH"] += 100 - sum(base.values())
    card = score({"MOMENTUM": [D(90)], "VALUATION": [D(10)]}, base)
    assert card.coverage < MIN_WEIGHT_COVERAGE
    assert card.overall is None and "no overall score" in card.withheld
    assert set(card.pillar_scores) == {"MOMENTUM", "VALUATION"}  # pillar scores are still shown
    assert score({}, base).overall is None


def test_curated_weights_are_complete_documented_and_labelled_as_judgement():
    for p in [*profiles.PROFILES.values(), profiles.GENERIC_BANK, profiles.GENERIC_NONFIN]:
        assert sum(p.weights.values()) == 100 and set(p.weights) == set(profiles.PILLARS)
        assert all(len(p.rationale[k]) > 20 for k in profiles.PILLARS)
        assert "not derived from data" in p.note
    assert profiles.profile_for("banking", "BANK") is profiles.BANKING
    assert profiles.profile_for("banking", "NONFIN") is profiles.GENERIC_NONFIN  # does not fit
    assert profiles.profile_for(None, "BANK") is profiles.GENERIC_BANK
    assert profiles.profile_for("pharma", "NONFIN") is profiles.GENERIC_NONFIN


def test_scored_metrics_exist_in_the_calculation_catalogue_and_point_the_right_way():
    from app.calc.catalogue import BY_CODE

    market_made = {"VALUATION.pe_ttm", "VALUATION.pe_annual", "VALUATION.price_to_book"}
    for metrics in profiles.METRICS.values():
        for sm in metrics:
            assert sm.pillar in profiles.PILLARS and sm.direction in (HIGHER, LOWER)
            if not sm.code.startswith("TECH.") and sm.code not in market_made:
                assert sm.code in BY_CODE, sm.code
    lower = {sm.code for sm in profiles.NONFIN_METRICS if sm.direction == LOWER}
    assert {"LEVERAGE.debt_to_equity", "VALUATION.pe_ttm", "VALUATION.price_to_book"} <= lower
    assert {"RATIO.cost_to_income", "RATIO.provisions_to_ppop"} <= {
        sm.code for sm in profiles.BANK_METRICS if sm.direction == LOWER
    }


# ---- database fixtures -----------------------------------------------------------------------------

_n = iter(range(1, 100000))


def mk_company(session, name, sector="it_services", industry="it_services_and_consulting"):
    sec = session.scalar(select(Sector).where(Sector.code == sector)) if sector else None
    ind = (
        session.scalar(
            select(Industry).where(Industry.code == industry, Industry.sector_id == sec.id)
        )
        if sec and industry
        else None
    )
    c = Company(
        legal_name=f"{name} Limited",
        common_name=name,
        master_data_source="test",
        issuer_key=f"T{next(_n):06d}"[:7],
        sector_id=sec.id if sec else None,
        industry_id=ind.id if ind else None,
    )
    session.add(c)
    session.flush()
    return c


def _record(
    session,
    company,
    metric,
    value,
    unit,
    *,
    end,
    ptype,
    basis,
    kind,
    dimension="",
    cls=EvidenceClass.CALCULATION,
    tier=2,
):
    r = EvidenceRecord(
        company_id=company.id,
        evidence_class=cls,
        kind=kind,
        metric=metric,
        dimension=dimension,
        period_end=end,
        period_type=ptype,
        basis=basis,
        value=D(str(value)),
        unit=unit,
        expression="synthetic" if cls is EvidenceClass.CALCULATION else None,
        provider_code="TEST",
        authority_tier=tier,
        source_url="test://",
        retrieved_at=NOW,
        # synthetic inputs have no links, so mark them as such (a WARNING, not an error)
        details={"missing_inputs": ["synthetic"]} if cls is EvidenceClass.CALCULATION else None,
        content_hash=uuid.uuid4().hex,
        created_by="test",
        status_changed_at=NOW,
    )
    session.add(r)
    session.flush()
    session.add(
        EvidenceResolution(
            company_id=company.id,
            metric=metric,
            dimension=dimension,
            period_end=end,
            period_type=ptype or "",
            basis=basis or "",
            selected_evidence_id=r.id,
            status=ResolutionStatus.SINGLE_SOURCE,
            quality=Quality.MEDIUM,
            quality_reasons=[],
            alternatives=[],
        )
    )
    session.flush()
    return r


def metric(
    session, company, code, value, *, end=FY26, ptype="ANNUAL", basis="CONSOLIDATED", unit="percent"
):
    kind = "TECHNICAL" if code.startswith("TECH.") else "CALCULATION"
    if code.startswith(("TECH.", "VALUATION.")):
        end, ptype = PRICE_DAY, None if code.startswith("TECH.") else "TTM"
        basis = None if code.startswith("TECH.") else basis
    return _record(
        session,
        company,
        code,
        value,
        unit,
        end=end,
        ptype=ptype,
        basis=basis,
        kind=kind,
        dimension=f"NSE:{company.common_name}" if kind == "TECHNICAL" else "",
    )


def size(session, company, total_income_crore, bank=False):
    _record(
        session,
        company,
        "INCOME.total_income",
        D(total_income_crore) * CR,
        "inr",
        end=FY26,
        ptype="ANNUAL",
        basis="CONSOLIDATED",
        kind="FINANCIAL_STATEMENT",
        cls=EvidenceClass.FACT,
    )
    if bank:
        _record(
            session,
            company,
            "INCOME.interest_earned",
            D(total_income_crore) * CR / 2,
            "inr",
            end=FY26,
            ptype="ANNUAL",
            basis="CONSOLIDATED",
            kind="FINANCIAL_STATEMENT",
            cls=EvidenceClass.FACT,
        )


@pytest.fixture
def seeded(session):
    from app.seeds.loader import seed_reference_data

    seed_reference_data(session)
    return session


def run(session, company):
    return analyze_company(session, company.id, today=TODAY, now=NOW)


IT_VALUES = {  # code -> {company: value}
    "GROWTH.revenue_yoy": {"A": 10, "B": 20, "C": 5, "D": 15},
    "GROWTH.net_profit_yoy": {"A": 12, "B": 8, "C": 30, "D": 4},
    "MARGIN.net_margin": {"A": 20, "B": 25, "C": 15, "D": 18},
    "MARGIN.pbt_margin": {"A": 28, "B": 33, "C": 22, "D": 25},
    "RETURN.roe_closing": {"A": 30, "B": 25, "C": 40, "D": 20},
    "LEVERAGE.equity_multiplier": {"A": 1.5, "B": 1.2, "C": 2.0, "D": 1.8},
    "VALUATION.pe_ttm": {"A": 25, "B": 30, "C": 20, "D": 35},
}


@pytest.fixture
def it_sector(seeded):
    companies = {n: mk_company(seeded, n) for n in "ABCD"}
    for n, c in companies.items():
        size(seeded, c, 1000 * (1 + "ABCD".index(n)))
        for code, by in IT_VALUES.items():
            metric(seeded, c, code, by[n], unit="ratio" if code.startswith("LEV") else "percent")
    return companies


# ---- the engine --------------------------------------------------------------------------------------


def test_ranks_medians_and_scores_match_hand_calculation(seeded, it_sector):
    a = it_sector["A"]
    report = run(seeded, a)
    assert [p.candidate.name for p in report.picks] == ["B", "C", "D"]  # closest in size first
    assert report.peer_set.peer_count == 3 and report.peer_set.thin is False
    by = {o.code: o for o in report.outcomes if o.status == "COMPARED"}
    # revenue growth A=10 vs B20 C5 D15: beats only C -> 1/3
    rank = by["GROWTH.revenue_yoy"].stats.rank
    assert rank.value == D("33.333333") and rank.unit == "percent"
    assert by["GROWTH.revenue_yoy"].stats.median.value == D(15)
    # pe_ttm LOWER is favourable: A=25 vs 30, 20, 35 -> beats 30 and 35 -> 2/3
    assert by["VALUATION.pe_ttm"].stats.rank.value == D("66.666667")
    # equity multiplier LOWER: A=1.5 vs 1.2, 2.0, 1.8 -> beats 2.0, 1.8
    assert by["LEVERAGE.equity_multiplier"].stats.rank.value == D("66.666667")
    # growth pillar = mean(33.3 [revenue], npat 12 vs 8,30,4 -> 2/3 = 66.7)
    card = report.scorecard
    assert abs(card.pillar_scores["GROWTH"] - D(50)) < D("0.001")
    assert abs(card.pillar_scores["PROFITABILITY"] - D(66.666667)) < D(
        "0.001"
    )  # npm 20: beats C, D
    assert report.overall is not None
    expected = sum(card.pillar_scores[p] * card.weights[p] for p in card.weights) / 100
    assert abs(report.overall.value - expected) < D("0.0001")
    assert report.overall.metric == "SCORE.overall" and report.overall.unit == "percent"


def test_missing_pillars_are_dropped_and_recorded_as_weight_adjustments(seeded, it_sector):
    report = run(seeded, it_sector["A"])
    ps = report.peer_set
    changes = {
        w.pillar: w
        for w in seeded.scalars(
            select(WeightAdjustment).where(WeightAdjustment.peer_set_id == ps.id)
        )
    }
    assert set(changes) == set(profiles.PILLARS)
    assert changes["MOMENTUM"].adjusted_weight == 0 and "no metric" in changes["MOMENTUM"].reason
    assert changes["GROWTH"].adjusted_weight > changes["GROWTH"].base_weight
    assert sum(w.adjusted_weight for w in changes.values()) == D(100)
    assert report.scorecard.coverage == D(90)  # momentum (10) has no data


def test_every_comparison_is_evidence_linked_to_the_values_it_used(seeded, it_sector):
    report = run(seeded, it_sector["A"])
    rank = next(o for o in report.outcomes if o.code == "MARGIN.net_margin").stats.rank
    links = list(seeded.scalars(select(EvidenceLink).where(EvidenceLink.evidence_id == rank.id)))
    assert {link.alias for link in links} == {"subject", "peer1", "peer2", "peer3"}
    inputs = {link.alias: seeded.get(EvidenceRecord, link.input_id) for link in links}
    assert inputs["subject"].company_id == it_sector["A"].id
    assert {r.company_id for a, r in inputs.items() if a != "subject"} == {
        it_sector[n].id for n in "BCD"
    }
    assert rank.details["method"] == "peer.v1" and rank.details["n"] == 3
    assert [p["company"] for p in rank.details["peers"]] == ["B", "C", "D"]
    overall_links = seeded.scalars(
        select(EvidenceLink).where(EvidenceLink.evidence_id == report.overall.id)
    ).all()
    assert {link.alias for link in overall_links} == set(report.pillar_records)


def test_the_run_is_recorded_with_peers_reasons_and_weights(seeded, it_sector):
    report = run(seeded, it_sector["A"])
    ps = report.peer_set
    assert (
        ps.sector_code == "it_services"
        and ps.candidates == 3
        and ps.criteria["basis"] == "CONSOLIDATED"
    )
    peers = seeded.scalars(select(Peer).where(Peer.peer_set_id == ps.id).order_by(Peer.rank)).all()
    assert [p.rank for p in peers] == [1, 2, 3] and all("same industry" in p.reason for p in peers)
    weights = seeded.get(SectorWeightSet, ps.weight_set_id)
    assert weights.sector_code == "it_services" and weights.version == profiles.VERSION
    assert weights.pillar_weights["GROWTH"] == 20 and weights.rationale["BALANCE_SHEET"]
    assert ps.summary["compared"] == 7 and ps.summary["overall"] == str(
        report.overall.value.quantize(D("0.01"))
    )
    assert len(ps.summary["gaps"]) == 13 - 7 + 4 - 4 + 6 - 6 or ps.summary["gaps"]


def test_weight_sets_are_stored_once_per_version(seeded):
    first = ensure_weight_sets(seeded)
    again = ensure_weight_sets(seeded)
    assert {k: v.id for k, v in first.items()} == {k: v.id for k, v in again.items()}
    assert "banking" in first and "_generic_nonfin" in first
    assert seeded.scalar(select(func.count()).select_from(SectorWeightSet)) == len(first)


def test_thin_peer_groups_are_marked_and_capped_at_low_quality(seeded):
    a, b = mk_company(seeded, "A"), mk_company(seeded, "B")
    for c, v in ((a, 10), (b, 20)):
        size(seeded, c, 1000)
        for code in (
            "MARGIN.net_margin",
            "RETURN.roe_closing",
            "GROWTH.revenue_yoy",
            "GROWTH.net_profit_yoy",
            "MARGIN.pbt_margin",
            "RETURN.roa_closing",
            "LEVERAGE.equity_multiplier",
            "VALUATION.pe_ttm",
        ):
            metric(seeded, c, code, v)
    report = run(seeded, a)
    assert report.peer_set.thin is True and report.peer_set.peer_count == 1
    rank = next(o for o in report.outcomes if o.status == "COMPARED").stats.rank
    assert rank.details["thin_peer_group"] is True
    resolution = seeded.scalar(
        select(EvidenceResolution).where(EvidenceResolution.selected_evidence_id == rank.id)
    )
    assert resolution.quality is Quality.LOW
    assert any("fewer peers than the minimum" in r for r in resolution.quality_reasons)


def test_only_momentum_data_gives_pillar_scores_but_no_overall(seeded):
    a, b, c = (mk_company(seeded, n) for n in "ABC")
    for i, co in enumerate((a, b, c)):
        size(seeded, co, 1000)
        for code in ("TECH.return_6m", "TECH.return_1y", "TECH.pct_from_52w_high"):
            metric(seeded, co, code, 10 * (i + 1))
    report = run(seeded, a)
    assert set(report.pillar_records) == {"MOMENTUM"} and report.overall is None
    assert report.scorecard.coverage == D(10)
    assert "no overall score" in report.peer_set.summary["overall_withheld"]
    assert report.peer_set.summary["overall"] is None
    assert (
        seeded.scalar(
            select(func.count())
            .select_from(EvidenceRecord)
            .where(EvidenceRecord.metric == "SCORE.overall")
        )
        == 0
    )


def test_banks_use_bank_metrics_and_are_never_compared_with_non_financials(seeded):
    bank = mk_company(seeded, "Bank1", "banking", "private_sector_bank")
    bank2 = mk_company(seeded, "Bank2", "banking", "private_sector_bank")
    other = mk_company(seeded, "Maker", "banking", "private_sector_bank")  # same sector, not a bank
    size(seeded, bank, 1000, bank=True)
    size(seeded, bank2, 1200, bank=True)
    size(seeded, other, 1000)
    for co, roe in ((bank, 15), (bank2, 12), (other, 99)):
        metric(seeded, co, "RETURN.roe_closing", roe)
    report = run(seeded, bank)
    assert report.candidate.profile == "BANK" and report.profile.sector_code == "banking"
    assert [p.candidate.name for p in report.picks] == ["Bank2"]
    assert any("different statement profile" in why for _, why in report.rejected)
    roe = next(o for o in report.outcomes if o.code == "RETURN.roe_closing")
    assert roe.stats.rank.value == D(100) and roe.peers == 1


def test_peers_are_aligned_on_the_period_most_of_them_share(seeded):
    fy25 = date(2025, 3, 31)
    subj = mk_company(seeded, "S")
    peers = [mk_company(seeded, n) for n in "XYZ"]
    for c in (subj, *peers):
        size(seeded, c, 1000)
    # the subject has FY26 and FY25; only FY25 is shared by three peers
    metric(seeded, subj, "RETURN.roe_closing", 20, end=FY26)
    metric(seeded, subj, "RETURN.roe_closing", 18, end=fy25)
    for i, p in enumerate(peers):
        metric(seeded, p, "RETURN.roe_closing", 10 + i, end=fy25)
    metric(seeded, peers[0], "RETURN.roe_closing", 99, end=FY26)  # one peer has FY26
    report = run(seeded, subj)
    outcome = next(o for o in report.outcomes if o.code == "RETURN.roe_closing")
    assert outcome.peers == 3
    assert outcome.stats.rank.period_end == fy25 and outcome.stats.rank.value == D(100)


def test_no_peer_with_a_comparable_value_is_a_gap_with_the_reason(seeded):
    a, b = mk_company(seeded, "A"), mk_company(seeded, "B")
    for c in (a, b):
        size(seeded, c, 1000)
    metric(seeded, a, "RETURN.roe_closing", 20)
    metric(seeded, b, "RETURN.roe_closing", 10, basis="STANDALONE")
    report = run(seeded, a)
    outcome = next(o for o in report.outcomes if o.code == "RETURN.roe_closing")
    assert outcome.status == "GAP" and "no peer has a value" in outcome.reason
    missing = next(o for o in report.outcomes if o.code == "GROWTH.revenue_yoy")
    assert missing.reason == "the company has no value for it"


def test_a_company_without_a_sector_has_no_peers(seeded):
    a = mk_company(seeded, "A", sector=None)
    b = mk_company(seeded, "B")
    for c in (a, b):
        size(seeded, c, 1000)
        metric(seeded, c, "RETURN.roe_closing", 10)
    report = run(seeded, a)
    assert report.picks == [] and report.peer_set.peer_count == 0 and report.peer_set.thin
    assert report.peer_set.sector_code is None
    assert all(o.status == "GAP" for o in report.outcomes)
    assert report.profile.sector_code == "_generic_nonfin"


def test_a_company_with_no_calculated_metrics_is_reported_not_crashed(seeded):
    lone = mk_company(seeded, "Lone")
    report = run(seeded, lone)
    assert report.candidate is None and report.peer_set is None


# ---- validation: the comparisons are recomputed and tampering is caught -----------------------------


def test_clean_run_validates_and_comparisons_recompute_exactly(seeded, it_sector):
    report = run(seeded, it_sector["A"])
    assert report.validation.run.errors == 0
    for record in seeded.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == it_sector["A"].id,
            EvidenceRecord.kind == "SECTOR",
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    ):
        value, problems = recompute(seeded, record)
        assert problems == [] and abs(value - record.value) < D("0.0001"), record.metric


def test_a_changed_peer_value_is_detected_and_blocks_the_comparison(seeded, it_sector):
    report = run(seeded, it_sector["A"])
    rank = next(o for o in report.outcomes if o.code == "GROWTH.revenue_yoy").stats.rank
    assert rank.value == D("33.333333")  # A (10) beats only C (5)
    link = seeded.scalar(
        select(EvidenceLink).where(
            EvidenceLink.evidence_id == rank.id, EvidenceLink.alias == "peer2"
        )
    )
    peer_value = seeded.get(EvidenceRecord, link.input_id)
    assert peer_value.value == D(5)
    peer_value.value = D(50)  # C now beats A: the stored rank (33.3) is no longer true (0)
    seeded.flush()
    validation = validate_company(seeded, it_sector["A"].id, today=TODAY, now=NOW, sync=False)
    errors = [f for f in validation.findings if f.severity is Severity.ERROR]
    mismatch = [
        f for f in errors if f.rule == "CALC_RECOMPUTE_MISMATCH" and f.metric == rank.metric
    ]
    assert mismatch and mismatch[0].details["recomputed"].startswith("0")
    resolution = seeded.scalar(
        select(EvidenceResolution).where(EvidenceResolution.selected_evidence_id == rank.id)
    )
    assert resolution.blocked
    # everything built on the blocked comparison is blocked too: the pillar score and the overall
    assert seeded.scalar(
        select(EvidenceResolution).where(
            EvidenceResolution.selected_evidence_id == report.overall.id
        )
    ).blocked


def test_a_superseded_peer_input_is_reported_as_not_current(seeded, it_sector):
    report = run(seeded, it_sector["A"])
    rank = next(o for o in report.outcomes if o.code == "MARGIN.net_margin").stats.rank
    link = seeded.scalar(
        select(EvidenceLink).where(
            EvidenceLink.evidence_id == rank.id, EvidenceLink.alias == "peer2"
        )
    )
    seeded.get(EvidenceRecord, link.input_id).status = EvidenceStatus.SUPERSEDED
    seeded.flush()
    validation = validate_company(seeded, it_sector["A"].id, today=TODAY, now=NOW, sync=False)
    assert any(
        f.rule == "CALC_INPUT_NOT_CURRENT" and f.metric == rank.metric
        for f in validation.findings
        if f.severity is Severity.ERROR
    )


def test_rerunning_replaces_old_comparisons_and_retires_stale_ones(seeded, it_sector):
    run(seeded, it_sector["A"])
    ids = {
        r.id
        for r in seeded.scalars(
            select(EvidenceRecord).where(
                EvidenceRecord.company_id == it_sector["A"].id, EvidenceRecord.kind == "SECTOR"
            )
        )
    }
    again = run(seeded, it_sector["A"])
    active = {
        r.id
        for r in seeded.scalars(
            select(EvidenceRecord).where(
                EvidenceRecord.company_id == it_sector["A"].id,
                EvidenceRecord.kind == "SECTOR",
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
            )
        )
    }
    assert active == ids  # identical inputs: the same records, nothing duplicated
    # now a peer disappears: the next run must not keep comparisons built on it
    d_metrics = seeded.scalars(
        select(EvidenceRecord).where(EvidenceRecord.company_id == it_sector["D"].id)
    ).all()
    for r in d_metrics:
        r.status = EvidenceStatus.WITHDRAWN
    seeded.flush()
    third = run(seeded, it_sector["A"])
    assert third.validation.run.errors == 0
    assert again.peer_set.id != third.peer_set.id


# ---- API -----------------------------------------------------------------------------------------


@pytest.fixture
def client(session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_api_serves_peers_scorecard_and_weights(seeded, it_sector, client):
    a = it_sector["A"]
    run(seeded, a)
    base = f"/api/companies/{a.id}"
    peers = client.get(f"{base}/peers").json()
    assert [p["name"] for p in peers["peers"]] == ["B", "C", "D"] and peers["thin"] is False
    assert peers["sector"] == "it_services" and peers["criteria"]["same_statement_profile"] is True
    card = client.get(f"{base}/scorecard").json()
    assert card["overall"] is not None and card["weight_coverage_pct"] == "90.0"
    assert "not a measure of quality" in card["meaning"]
    revenue = next(c for c in card["comparisons"] if c["metric"] == "GROWTH.revenue_yoy")
    assert revenue["percent_of_peers_beaten"].startswith("33.33") and revenue["peers_compared"] == 3
    assert revenue["peer_median"].startswith("15") and revenue["pillar"] == "GROWTH"
    assert {w["pillar"] for w in card["weights"]} == set(profiles.PILLARS)
    assert any(g["metric"].startswith("TECH.") for g in card["gaps"])
    weights = client.get("/api/sector-weights", params={"sector": "it_services"}).json()
    assert weights[0]["pillar_weights"]["PROFITABILITY"] == 25 and weights[0]["rationale"]["GROWTH"]
    assert client.get("/api/sector-weights", params={"sector": "nope"}).json() == []


def test_api_404s_and_read_only(seeded, client):
    ghost = uuid.uuid4()
    for path in ("peers", "scorecard"):
        assert client.get(f"/api/companies/{ghost}/{path}").status_code == 404
    lone = mk_company(seeded, "Lone")
    assert client.get(f"/api/companies/{lone.id}/peers").status_code == 404
    assert client.post(f"/api/companies/{lone.id}/peers").status_code == 405
