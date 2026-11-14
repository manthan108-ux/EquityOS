# ruff: noqa: F811, E501
"""Sector-specific indicators: bank, IT, FMCG, automobile and energy panels."""

import json
from datetime import date
from decimal import Decimal as D

from app.research import readiness as rd
from app.research import sector_kpis as sk
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

Q = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30)]
YEARS = [date(2023, 3, 31), date(2024, 3, 31), date(2025, 3, 31), date(2026, 3, 31)]
CRORE = 10_000_000


def put(
    session,
    co,
    metric,
    values,
    *,
    years,
    unit="percent",
    ptype="QUARTERLY",
    basis=None,
    dimension="",
):
    for end, v in zip(years, values, strict=False):
        if v is not None:
            evidence(
                session,
                co,
                metric,
                v,
                end=end,
                basis=basis,
                unit=unit,
                ptype=ptype,
                dimension=dimension,
            )


def store(session, co):
    return rd.load_store(session, co.id, set(sk.NAMES))


def test_a_bank_panel_shows_reported_ratios_with_their_period_and_change(seeded):
    co = mk_company(seeded, "Bank Co")
    put(seeded, co, "RATIOS.gross_npa_ratio", [2.6, 2.5, 2.4, 2.3, 2.2], years=Q)
    put(seeded, co, "RATIOS.net_interest_margin", [3.9, 3.9, 3.8, 3.7, 3.7], years=Q)
    put(seeded, co, "RATIOS.casa_ratio", [40.0, 39.0], years=Q[3:])
    out = sk.build(store(seeded, co), [], "BANK", "CONSOLIDATED")
    g = out["indicators"]["gnpa_ratio"]
    assert g["available"] and g["source"] == "REPORTED" and g["group"] == "Asset quality"
    v = g["variants"][0]
    assert (
        v["period_type"] == "QUARTERLY"
        and D(v["latest"]["value"]) == D("2.2")
        and D(v["change"]) == D("-0.4")
    )
    assert v["latest"]["evidence_id"] and len(v["change_evidence_ids"]) == 2
    assert out["indicators"]["casa"]["available"] and D(
        out["indicators"]["casa"]["variants"][0]["change"]
    ) == D("-1.0")


def test_a_missing_reported_indicator_says_why_and_is_not_zero(seeded):
    out = sk.build(store(seeded, mk_company(seeded, "Bare Bank")), [], "BANK", "CONSOLIDATED")
    k = out["indicators"]["capital_adequacy"]
    assert not k["available"] and "not stated in the stored documents" in k["reason"]
    assert out["available"] is False


def test_calculated_bank_indicators_come_from_the_calculation_engine_on_the_basis(seeded):
    co = mk_company(seeded, "Calc Bank")
    put(
        seeded,
        co,
        "GROWTH.advances_yoy",
        [12.0, 14.0],
        years=YEARS[2:],
        ptype="ANNUAL",
        basis="CONSOLIDATED",
    )
    put(
        seeded,
        co,
        "GROWTH.advances_yoy",
        [99.0],
        years=YEARS[3:],
        ptype="ANNUAL",
        basis="STANDALONE",
    )
    k = sk.indicator(
        store(seeded, co), next(s for s in sk.BANKING if s.key == "credit_growth"), "CONSOLIDATED"
    )
    assert k["source"] == "CALCULATED" and D(k["variants"][0]["latest"]["value"]) == D(14)


def test_voluntary_and_total_attrition_are_never_blended(seeded):
    co = mk_company(seeded, "IT Co")
    put(
        seeded,
        co,
        "OPERATING.attrition",
        [13.7],
        years=YEARS[3:],
        ptype="ANNUAL",
        dimension="voluntary",
    )
    put(
        seeded,
        co,
        "OPERATING.attrition",
        [15.1],
        years=YEARS[3:],
        ptype="ANNUAL",
        dimension="total",
    )
    out = sk.build(store(seeded, co), [], "IT_SERVICES", "CONSOLIDATED")
    dims = {
        v["dimension"]: D(v["latest"]["value"]) for v in out["indicators"]["attrition"]["variants"]
    }
    assert dims == {"voluntary": D("13.7"), "total": D("15.1")}


def test_it_indicators_include_people_and_margin_measures(seeded):
    co = mk_company(seeded, "IT Co 2")
    put(
        seeded,
        co,
        "MARGIN.ebit_margin",
        [22.0, 23.0],
        years=YEARS[2:],
        ptype="ANNUAL",
        basis="CONSOLIDATED",
    )
    put(
        seeded,
        co,
        "MARGIN.employee_cost_ratio",
        [55.0, 54.0],
        years=YEARS[2:],
        ptype="ANNUAL",
        basis="CONSOLIDATED",
    )
    out = sk.build(store(seeded, co), [], "IT_SERVICES", "CONSOLIDATED")
    assert D(out["indicators"]["ebit_margin"]["variants"][0]["change"]) == D("1.0")
    assert out["indicators"]["employee_cost_ratio"]["group"] == "People"
    assert not out["indicators"]["utilisation"]["available"]


def test_fmcg_volume_growth_is_not_invented(seeded):
    out = sk.build(store(seeded, mk_company(seeded, "FMCG Co")), [], "FMCG", "CONSOLIDATED")
    k = out["indicators"]["volume_growth"]
    assert not k["available"] and "volume growth is not stated" in k["reason"]


def test_automobile_volume_growth_and_realisation_come_from_stated_volume_and_revenue(seeded):
    co = mk_company(seeded, "Auto Co")
    put(
        seeded,
        co,
        "OPERATING.vehicle_volumes",
        [1_000_000, 1_100_000],
        years=YEARS[2:],
        ptype="ANNUAL",
        unit="units",
    )
    put(
        seeded,
        co,
        "INCOME.revenue_from_operations",
        [100_000 * CRORE, 121_000 * CRORE],
        years=YEARS[2:],
        ptype="ANNUAL",
        unit="inr",
        basis="CONSOLIDATED",
    )
    out = sk.build(store(seeded, co), [], "AUTOMOBILES", "CONSOLIDATED")
    vm = out["volume_measures"]
    assert (
        vm["available"]
        and vm["kind"] == "DERIVED"
        and "average revenue per vehicle" in vm["formula"]
    )
    assert (
        D(vm["volume_growth"][0]["value"]) == D("10.0")
        and len(vm["volume_growth"][0]["evidence_ids"]) == 2
    )
    assert [D(r["value"]) for r in vm["realisation"]] == [D(1_000_000), D(1_100_000)]


def test_one_volume_period_alone_gives_no_growth_and_a_gap_reason(seeded):
    co = mk_company(seeded, "One Period Auto")
    put(seeded, co, "OPERATING.vehicle_volumes", [700_000], years=Q[4:], unit="units")
    vm = sk.build(store(seeded, co), [], "AUTOMOBILES", "CONSOLIDATED")["volume_measures"]
    assert vm["available"] is False and "one period only" in vm["reason"]


def test_domestic_and_export_volumes_are_kept_out_of_the_total_growth(seeded):
    co = mk_company(seeded, "Split Auto")
    put(
        seeded,
        co,
        "OPERATING.vehicle_volumes",
        [900_000, 1_000_000],
        years=YEARS[2:],
        ptype="ANNUAL",
        unit="units",
        dimension="domestic",
    )
    assert sk.build(store(seeded, co), [], "AUTOMOBILES", "CONSOLIDATED")["volume_measures"] is None


def test_energy_operating_metrics_are_listed_as_not_stated_when_absent(seeded):
    out = sk.build(store(seeded, mk_company(seeded, "Energy Co")), [], "ENERGY", "CONSOLIDATED")
    for key in ("production", "realisation", "refinery_throughput", "grm"):
        assert (
            not out["indicators"][key]["available"]
            and "not stated" in out["indicators"][key]["reason"]
        )


def test_reported_energy_operating_figures_are_shown_when_stored(seeded):
    co = mk_company(seeded, "Refiner Co")
    put(
        seeded, co, "OPERATING.grm", [8.5, 9.5], years=YEARS[2:], ptype="ANNUAL", unit="usd_per_bbl"
    )
    k = sk.build(store(seeded, co), [], "ENERGY", "CONSOLIDATED")["indicators"]["grm"]
    assert k["available"] and k["source"] == "REPORTED" and "change" not in k["variants"][0]


def test_peer_comparison_needs_three_peers_for_the_same_period(seeded):
    own = mk_company(seeded, "Bank A")
    put(seeded, own, "RATIOS.gross_npa_ratio", [2.0], years=Q[4:])
    peers = []
    for i, v in enumerate((1.0, 2.5, 3.0, 4.0)):
        p = mk_company(seeded, f"Bank P{i}")
        put(seeded, p, "RATIOS.gross_npa_ratio", [v], years=Q[4:])
        peers.append((p, store(seeded, p)))
    k = next(s for s in sk.BANKING if s.key == "gnpa_ratio")
    own_i = sk.indicator(store(seeded, own), k, "CONSOLIDATED")
    block = sk.peer_compare(store(seeded, own), peers, k, "CONSOLIDATED", own_i)
    assert block["available"] and block["benchmark"] == "PEER_GROUP" and block["lower_is_better"]
    assert D(block["peer_group_median"]) == D("2.8") and D(block["difference"]) == D("-0.8")
    few = sk.peer_compare(store(seeded, own), peers[:2], k, "CONSOLIDATED", own_i)
    assert not few["available"] and few["peers_with_value"] == 2


def test_an_unknown_sector_has_no_panel_and_says_so(seeded):
    out = sk.build(
        store(seeded, mk_company(seeded, "Other Co")), [], "GENERIC_NONFIN", "CONSOLIDATED"
    )
    assert out["available"] is False and "general financial sections apply" in out["reason"]


def test_the_pdf_shows_sector_indicators_with_reasons_for_the_missing(seeded):
    from tests.test_report_context import _axis, _flat

    co = mk_company(seeded, "PDF Bank")
    put(seeded, co, "RATIOS.gross_npa_ratio", [2.6, 2.2], years=Q[3:])
    put(seeded, co, "RATIOS.net_interest_margin", [3.9, 3.7], years=Q[3:])
    content = _axis()
    content["analysis"]["sector_kpis"] = json.loads(
        json.dumps(sk.build(store(seeded, co), [], "BANK", "CONSOLIDATED"))
    )
    flat = _flat(content)
    assert (
        "Sector-specific indicators" in flat
        and "Gross NPA ratio" in flat
        and "Net interest margin" in flat
    )
    assert "Not available" in flat and "capital adequacy" in flat.lower()


def test_an_empty_sector_panel_leaves_no_section():
    from tests.test_report_context import _axis, _flat

    content = _axis()
    content["analysis"]["sector_kpis"] = {"available": False, "reason": "none"}
    assert "Sector-specific indicators" not in _flat(content)
