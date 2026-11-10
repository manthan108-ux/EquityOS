"""Company identity, restructuring histories and industry-level peer groups (Phase 10)."""

from datetime import date
from decimal import Decimal

from app.sector.core import Candidate, peer_group, select_peers
from app.sector.store import starts_before
from app.seeds.identity import IDENTITIES, NOT_THE_SAME_ENTITY, comparable_from
from app.seeds.sample_universe import BY_SYMBOL, NEW_INDUSTRIES, RECLASSIFIED


def cand(name, industry, sector="automobiles", profile="NONFIN", size=100):
    return Candidate(name.lower(), name, sector, industry, profile, Decimal(size))


def test_every_new_company_has_a_checked_identity():
    new = {
        "NESTLEIND",
        "BRITANNIA",
        "GODREJCP",
        "TMPV",
        "HYUNDAI",
        "TVSMOTOR",
        "ONGC",
        "IOC",
        "BPCL",
    }
    assert new <= set(IDENTITIES) and new <= set(BY_SYMBOL)
    for symbol in new:
        ident = IDENTITIES[symbol]
        assert ident.isin.startswith("INE") and len(ident.isin) == 12 and ident.legal_name
    assert "TMCV" in NOT_THE_SAME_ENTITY and "TMCV" not in BY_SYMBOL


def test_the_tata_motors_demerger_is_documented_and_blocks_pre_demerger_growth():
    ident = IDENTITIES["TMPV"]
    assert ident.relationship == "RENAMED_AFTER_DEMERGER"
    assert comparable_from("TMPV") == date(2025, 7, 1)
    assert "TMCV" in ident.note and "not" in ident.note.lower()
    # FY26 growth compares with FY25, which still contained commercial vehicles: excluded
    assert starts_before("GROWTH.revenue_yoy", date(2026, 3, 31), date(2025, 7, 1))
    # a comparison whose base is after the cut-off stays
    assert not starts_before("GROWTH.revenue_yoy", date(2026, 9, 30), date(2025, 7, 1))
    # quarter-on-quarter growth within the new structure stays too
    assert not starts_before("GROWTH.revenue_qoq", date(2026, 3, 31), date(2025, 7, 1))


def test_price_history_across_the_demerger_is_excluded_but_non_growth_metrics_are_not():
    cutoff = date(2025, 7, 1)
    assert starts_before("TECH.return_1y", date(2026, 3, 31), cutoff)
    assert not starts_before("TECH.return_6m", date(2026, 3, 31), cutoff)
    assert not starts_before("MARGIN.net_margin", date(2026, 3, 31), cutoff)


def test_nestle_year_end_change_blocks_growth_against_the_transition_period():
    cutoff = comparable_from("NESTLEIND")
    assert cutoff == date(2024, 4, 1)
    assert starts_before("GROWTH.revenue_yoy", date(2025, 3, 31), cutoff)  # vs the long year
    assert not starts_before("GROWTH.revenue_yoy", date(2026, 3, 31), cutoff)
    assert comparable_from("BRITANNIA") is None


def test_peers_must_share_an_industry_not_just_a_sector():
    subject = cand("Maruti", "passenger_vehicles")
    pool = [
        subject,
        cand("TMPV", "passenger_vehicles"),
        cand("Hyundai", "passenger_vehicles"),
        cand("TVS", "two_wheelers"),
    ]
    picks, rejected = select_peers(subject, pool)
    assert {p.candidate.name for p in picks} == {"TMPV", "Hyundai"}
    assert any(c.name == "TVS" and "different industry" in why for c, why in rejected)


def test_private_and_public_banks_are_one_peer_group():
    assert peer_group("private_sector_bank") == peer_group("public_sector_bank") == "banks"
    sbi = cand("SBI", "public_sector_bank", "banking", "BANK")
    hdfc = cand("HDFC", "private_sector_bank", "banking", "BANK")
    assert [p.candidate.name for p in select_peers(sbi, [sbi, hdfc])[0]] == ["HDFC"]


def test_reliance_is_a_conglomerate_with_no_industry_peers_and_the_reclassifications_are_listed():
    reliance = cand("Reliance", "diversified_conglomerate", "oil_and_gas")
    ioc = cand("IOC", "refineries_and_marketing", "oil_and_gas")
    picks, _ = select_peers(reliance, [reliance, ioc])
    assert picks == []
    assert RECLASSIFIED["RELIANCE"][2] == "diversified_conglomerate"
    assert ("oil_and_gas", "diversified_conglomerate") in NEW_INDUSTRIES
    assert RECLASSIFIED["MARUTI"][2] == RECLASSIFIED["M&M"][2] == "passenger_vehicles"
