# ruff: noqa: F811, E501
"""Historical and peer multiples, relative scenarios, sum of the parts, sensitivity and the method plan."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models import EodPrice, Security
from app.research import assumptions as asm
from app.research import dcf, relative, sotp
from app.research import profile as pr
from app.research import readiness as rd
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

TODAY = date(2026, 11, 14)
CRORE = 10_000_000
FY = [date(2024, 3, 31), date(2025, 3, 31), date(2026, 3, 31)]


def store(session, co, names=None):
    return rd.load_store(
        session, co.id, set(names or relative.NAMES | dcf.NAMES | pr.SEGMENT_NAMES)
    )


def priced_company(session):
    from app.models import Company
    from app.seeds.loader import seed_reference_data, seed_test_universe

    seed_reference_data(session)
    seed_test_universe(session)
    co = session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))
    sec = session.scalars(select(Security).where(Security.company_id == co.id)).first()
    session.query(EodPrice).filter(EodPrice.security_id == sec.id).delete()
    return co


def add_price(session, co, on, close):
    sec = session.scalars(select(Security).where(Security.company_id == co.id)).first()
    session.add(EodPrice(security_id=sec.id, trade_date=on, series="EQ", open=close, high=close, low=close, close=close, last=close, source_url="test://", file_sha256="0" * 64, retrieved_at=datetime.now(UTC)))  # fmt: skip
    session.flush()


def put(session, co, metric, values, *, unit="inr", years=FY, basis="CONSOLIDATED", ptype="ANNUAL"):
    for end, v in zip(years, values, strict=False):
        if v is not None:
            evidence(session, co, metric, v, end=end, basis=basis, unit=unit, ptype=ptype)


# ---- historical P/B and EV/EBITDA -------------------------------------------------------------------------------


def test_historical_pb_is_price_times_shares_over_equity_at_each_year_end(seeded):
    co = priced_company(seeded)
    put(seeded, co, "BALANCE.owners_equity", [1000 * CRORE, 1200 * CRORE, 1500 * CRORE])
    put(seeded, co, "BALANCE.shares_outstanding", [100 * CRORE] * 3, unit="ratio")
    for end, px in zip(FY, (20, 18, 30), strict=True):
        add_price(seeded, co, end, D(px))
    h = relative.historical_pb(seeded, co.id, store(seeded, co), "CONSOLIDATED")
    assert h["available"] and [r["pb"] for r in h["rows"]] == [
        "2",
        "1.5",
        "2",
    ]  # 2000/1000, 1800/1200, 3000/1500
    assert all(len(r["evidence_ids"]) == 2 for r in h["rows"]) and "closing price" in h["formula"]


def test_a_year_without_a_price_or_with_negative_equity_is_skipped_with_the_reason(seeded):
    co = priced_company(seeded)
    put(seeded, co, "BALANCE.owners_equity", [1000 * CRORE, -50 * CRORE, 1500 * CRORE])
    put(seeded, co, "BALANCE.shares_outstanding", [100 * CRORE] * 3, unit="ratio")
    add_price(seeded, co, FY[1], D(18))
    h = relative.historical_pb(seeded, co.id, store(seeded, co), "CONSOLIDATED")
    reasons = {s["fy"]: s["reason"] for s in h["skipped"]}
    assert (
        h["available"] is False
        and "no stored price" in reasons["FY2024"]
        and "not positive" in reasons["FY2025"]
    )


def test_historical_pb_without_the_inputs_says_so_and_never_mixes_bases(seeded):
    co = mk_company(seeded, "Basis Co")
    put(seeded, co, "BALANCE.owners_equity", [1000 * CRORE], years=FY[:1], basis="STANDALONE")
    put(
        seeded,
        co,
        "BALANCE.shares_outstanding",
        [100 * CRORE],
        unit="ratio",
        years=FY[:1],
        basis="CONSOLIDATED",
    )
    h = relative.historical_pb(seeded, co.id, store(seeded, co), "CONSOLIDATED")
    assert h["available"] is False and "not both stored" in h["reason"]


def test_historical_ev_ebitda_adds_net_debt_and_nci_to_the_market_cap(seeded):
    co = priced_company(seeded)
    put(seeded, co, "BALANCE.shares_outstanding", [100 * CRORE] * 3, unit="ratio")
    put(seeded, co, "LEVERAGE.net_debt", [500 * CRORE, 400 * CRORE, 300 * CRORE])
    put(seeded, co, "INCOME.ebitda", [400 * CRORE, 500 * CRORE, 600 * CRORE])
    put(seeded, co, "BALANCE_SHEET.non_controlling_interests", [None, None, 100 * CRORE])
    for end, px in zip(FY, (20, 20, 30), strict=True):
        add_price(seeded, co, end, D(px))
    h = relative.historical_ev_ebitda(seeded, co.id, store(seeded, co), "CONSOLIDATED")
    # FY2026: 3000 + 300 + 100 = 3400 / 600; FY2024: 2000 + 500 = 2500 / 400
    assert [r["ev_ebitda"] for r in h["rows"]] == ["6.25", "4.8", "5.67"]  # fmt: skip
    assert h["rows"][-1]["nci_included"] is True and h["rows"][0]["nci_included"] is False


def test_no_ebitda_means_no_historical_ev_ebitda(seeded):
    co = mk_company(seeded, "No Ebitda Co")
    put(seeded, co, "BALANCE.shares_outstanding", [100 * CRORE], unit="ratio", years=FY[:1])
    h = relative.historical_ev_ebitda(seeded, co.id, store(seeded, co), "CONSOLIDATED")
    assert h["available"] is False and "EBITDA needs finance costs and depreciation" in h["reason"]


# ---- the historical comparison ---------------------------------------------------------------------------------


def hist(values, key="pb"):
    return {
        "available": True,
        "rows": [{key: str(v), "evidence_ids": [i]} for i, v in enumerate(values, 1)],
    }


def test_the_current_multiple_is_positioned_in_the_companys_own_range_without_a_verdict():
    c = relative.context(
        hist([10, 12, 14, 16]),
        {"value": "13", "period_end": "2026-11-06", "evidence_id": 99},
        "pb",
        "P/B",
    )
    assert c["available"] and c["position"] == "WITHIN_RANGE" and c["share_at_or_below_pct"] == "50"
    assert (
        c["min"] == "10"
        and c["max"] == "16"
        and D(c["median"]) == D(13)
        and "not a verdict" in c["note"]
    )
    assert (
        relative.context(hist([10, 12, 14]), {"value": "20"}, "pb", "P/B")["position"]
        == "ABOVE_RANGE"
    )
    assert (
        relative.context(hist([10, 12, 14]), {"value": "5"}, "pb", "P/B")["position"]
        == "BELOW_RANGE"
    )


def test_two_observations_are_too_few_to_position_a_multiple():
    c = relative.context(hist([10, 12]), {"value": "11"}, "pb", "P/B")
    assert c["available"] is False and "3 are needed" in c["reason"]
    assert relative.context({"available": False}, {"value": "11"}, "pb", "P/B") is None
    assert relative.context(hist([1, 2, 3]), None, "pb", "P/B") is None


# ---- relative scenarios -----------------------------------------------------------------------------------------


class Rec:
    def __init__(self, v, i=1, end=date(2026, 3, 31)):
        self.value, self.id, self.period_end = D(str(v)), i, end


def peer_row(values):
    return {"peers": [{"comparable": True, "value": str(v)} for v in values]}


def test_peer_pb_scenarios_scale_the_price_by_the_peer_to_own_ratio():
    out = relative.relative_on_price(
        Rec(1000, 1), Rec(2, 2), peer_row([1, 2, 3]), "P/B", "book value"
    )
    assert (
        out["completed"] and out["scenarios"]["base"]["value_per_share"] == "1000"
    )  # 1000 x 2 / 2
    assert (
        out["scenarios"]["bear"]["value_per_share"] == "500"
        and out["scenarios"]["bull"]["value_per_share"] == "1500"
    )
    assert (
        out["scenarios"]["base"]["evidence_ids"] == [1, 2] and "fewer than 4 peers" in out["spread"]
    )


def test_four_peers_use_quartiles_and_fewer_than_three_give_nothing():
    four = relative.relative_on_price(
        Rec(100), Rec(10), peer_row([8, 10, 12, 14]), "P/E", "earnings"
    )
    assert "quartile" in four["spread"] and four["peer_count"] == 4
    two = relative.relative_on_price(Rec(100), Rec(10), peer_row([8, 10]), "P/E", "earnings")
    assert two["completed"] is False and "at least 3" in two["reason"]
    assert (
        relative.relative_on_price(None, Rec(10), peer_row([1, 2, 3]), "P/E", "earnings")[
            "completed"
        ]
        is False
    )
    assert (
        relative.relative_on_price(Rec(100), Rec(-5), peer_row([1, 2, 3]), "P/E", "earnings")[
            "completed"
        ]
        is False
    )


def test_the_relative_sensitivity_grid_centres_on_the_base_value():
    out = relative.relative_on_price(Rec(1000), Rec(2), peer_row([1, 2, 3]), "P/B", "book value")
    grid = out["sensitivity"]["grid"]
    centre = next(r for r in grid if r["multiple"] == "2")
    assert centre["cells"][2]["value_per_share"] == out["scenarios"]["base"]["value_per_share"]
    above = next(r for r in grid if r["multiple"] == "3")
    assert D(above["cells"][2]["value_per_share"]) > D(centre["cells"][2]["value_per_share"])


def actuals(net_debt=500, shares=100, nci=None, nd_end="2026-03-31"):
    def ref(v, i, end="2026-03-31"):
        return {"value": str(v), "evidence_id": i, "period_end": end}

    return {"net_debt": ref(net_debt * CRORE, 10, nd_end), "shares": ref(shares * CRORE, 11), "price": ref(100, 12), "non_controlling_interests": ref(nci * CRORE, 13) if nci else None, "revenue": ref(1000 * CRORE, 14)}  # fmt: skip


def test_ev_ebitda_scenarios_apply_the_peer_multiple_to_ebitda_then_bridge_to_equity():
    out = relative.relative_ev_ebitda(actuals(), Rec(400 * CRORE, 20), peer_row([6, 8, 10]))
    # base: 8 x 400 = 3200 EV - 500 net debt = 2700 over 100 shares = 27 per share
    assert out["completed"] and out["scenarios"]["base"]["value_per_share"] == "27"
    assert (
        out["scenarios"]["bear"]["value_per_share"] == "19"
        and out["scenarios"]["bull"]["value_per_share"] == "35"
    )
    assert (
        20 in out["scenarios"]["base"]["evidence_ids"]
        and 10 in out["scenarios"]["base"]["evidence_ids"]
    )
    assert any("non-controlling interests are not stored" in f for f in out["flags"])
    assert (
        next(r for r in out["sensitivity"]["grid"] if r["multiple"] == "8")["cells"][2][
            "value_per_share"
        ]
        == "27"
    )


def test_nci_is_deducted_and_a_date_mismatch_is_flagged():
    out = relative.relative_ev_ebitda(
        actuals(nci=100, nd_end="2025-03-31"), Rec(400 * CRORE, 20), peer_row([6, 8, 10])
    )
    assert out["scenarios"]["base"]["value_per_share"] == "26"  # (3200 - 500 - 100) / 100
    assert any("net debt is as at 2025-03-31" in f for f in out["flags"]) and not any(
        "non-controlling" in f for f in out["flags"]
    )


def test_missing_actuals_or_negative_ebitda_are_named_not_assumed():
    a = actuals()
    a["net_debt"] = None
    out = relative.relative_ev_ebitda(a, Rec(400 * CRORE), peer_row([6, 8, 10]))
    assert (
        out["completed"] is False
        and "net debt" in out["reason"]
        and "none is assumed" in out["reason"]
    )
    assert relative.relative_ev_ebitda(actuals(), None, peer_row([6, 8, 10]))["completed"] is False
    assert (
        relative.relative_ev_ebitda(actuals(), Rec(-5 * CRORE), peer_row([6, 8, 10]))["completed"]
        is False
    )


# ---- sum of the parts ------------------------------------------------------------------------------------------


def seg_company(session, name="Sotp Co", results=(600, 300, -50)):
    co = mk_company(session, name)
    for seg, res, rev in zip(
        ("Passenger Vehicles", "Farm Equipment", "Other"), results, (4000, 2000, 500), strict=True
    ):
        evidence(
            session,
            co,
            "SEGMENT.segment_result",
            res * CRORE,
            end=FY[2],
            basis="CONSOLIDATED",
            unit="inr",
            ptype="ANNUAL",
            dimension=seg,
        )
        evidence(
            session,
            co,
            "SEGMENT.segment_revenue",
            rev * CRORE,
            end=FY[2],
            basis="CONSOLIDATED",
            unit="inr",
            ptype="ANNUAL",
            dimension=seg,
        )
    return co


def enter_multiples(session, co, scenario, multiples):
    for seg, m in multiples.items():
        asm.set_input(session, co.id, "sotp_multiple", m, scenario=scenario, segment=seg, author="A. Analyst", rationale="own view of the EV/EBIT multiple that suits this segment", as_of=date(2026, 11, 12), today=TODAY)  # fmt: skip


def run_sotp(session, co, sector="AUTOMOBILES"):
    inputs = asm.current_inputs(session, co.id, TODAY)
    st = store(session, co)
    return sotp.build(st, "CONSOLIDATED", sector, actuals(), inputs, D(100))


def test_a_segment_multiple_is_a_ledger_input_per_segment_and_scenario(seeded):
    co = seg_company(seeded)
    enter_multiples(seeded, co, "base", {"Passenger Vehicles": 10, "Farm Equipment": 12})
    items = asm.current_inputs(seeded, co.id, TODAY)
    key = "VALUATION_INPUT.sotp_multiple_passenger_vehicles_base"
    assert (
        items[key]["segment"] == "Passenger Vehicles"
        and items[key]["scenario"] == "base"
        and D(items[key]["value"]) == D(10)
    )
    with pytest.raises(asm.AssumptionError, match="per segment"):
        asm.set_input(seeded, co.id, "sotp_multiple", 10, scenario="base", author="A", rationale="a sufficiently long rationale here", as_of=date(2026, 11, 12), today=TODAY)  # fmt: skip
    with pytest.raises(asm.AssumptionError, match="not set per segment"):
        asm.set_input(seeded, co.id, "beta", 1, segment="X", author="A", rationale="a sufficiently long rationale here", as_of=date(2026, 11, 12), today=TODAY)  # fmt: skip
    hist = asm.history(seeded, co.id, "sotp_multiple", "base", TODAY, segment="Passenger Vehicles")
    assert len(hist) == 1


def test_sotp_sums_the_segment_values_and_bridges_to_a_per_share_value(seeded):
    co = seg_company(seeded)
    enter_multiples(seeded, co, "base", {"Passenger Vehicles": 10, "Farm Equipment": 12})
    s = run_sotp(seeded, co)
    base = s["scenarios"]["base"]
    # 600 x 10 + 300 x 12 = 9600 EV; less net debt 500 = 9100 over 100 shares = 91; the loss-making segment adds nothing
    assert s["applicable"] and base["completed"] and base["enterprise_value"] == str(9600 * CRORE)
    assert base["value_per_share"] == "91" and base["bridge"]["equity_value"] == str(9100 * CRORE)
    other = next(p for p in base["parts"] if p["segment"] == "Other")
    assert other["multiple"] is None and "not valued by a profit multiple" in other["note"]
    assert any("central or unallocated costs" in f for f in base["flags"]) and base["evidence_ids"]
    assert s["scenarios"]["bear"]["completed"] is False and "Passenger Vehicles segment in the bear scenario" in " ".join(s["scenarios"]["bear"]["missing"])  # fmt: skip


def test_sotp_sensitivity_offsets_every_multiple_by_the_same_turns(seeded):
    co = seg_company(seeded)
    enter_multiples(seeded, co, "base", {"Passenger Vehicles": 10, "Farm Equipment": 12})
    rows = run_sotp(seeded, co)["sensitivity"]["rows"]
    by = {r["multiple_offset"]: D(r["value_per_share"]) for r in rows}
    assert (
        by["0"] == D(91)
        and by["1"] == D(91) + D(900) / D(100)
        and by["-1"] == D(91) - D(900) / D(100)
    )


def test_sotp_is_not_used_for_single_business_sectors_or_with_one_segment(seeded):
    co = seg_company(seeded)
    assert run_sotp(seeded, co, sector="IT_SERVICES")["applicable"] is False
    single = mk_company(seeded, "One Segment Co")
    evidence(
        seeded,
        single,
        "SEGMENT.segment_result",
        100 * CRORE,
        end=FY[2],
        basis="CONSOLIDATED",
        unit="inr",
        ptype="ANNUAL",
        dimension="Only",
    )
    s = sotp.build(store(seeded, single), "CONSOLIDATED", "ENERGY", actuals(), {}, None)
    assert s["applicable"] is False and "fewer than 2 segments" in s["reason"]


def test_without_multiples_nothing_is_valued(seeded):
    co = seg_company(seeded)
    s = run_sotp(seeded, co)
    assert s["completed"] is False and s["scenarios_complete"] == [] and s["sensitivity"] is None
    assert "assumption_cli set" in s["how_to_enter"] and "--segment" in s["how_to_enter"]


# ---- the method plan ---------------------------------------------------------------------------------------------


def plan(sector, kind, nbfc=False, **blocks):
    return {m["method"]: m for m in relative.method_plan(sector, kind, nbfc, blocks)}


def test_a_bank_gets_residual_income_and_pb_methods_and_no_enterprise_value_methods():
    p = plan("BANK", "BANK")
    assert p["Residual income (justified P/B)"]["applies"] and p["P/B against ROE"]["applies"]
    for m in ("EV/EBITDA", "Discounted cash flow (FCFF)", "Sum of the parts"):
        assert p[m]["applies"] is False and p[m]["status"] == "NOT_APPLICABLE"
    assert (
        "raw material" in p["Discounted cash flow (FCFF)"]["reason"]
        and "a bank" in p["EV/EBITDA"]["reason"]
    )


def test_an_nbfc_is_named_as_such_and_treated_like_a_lender():
    p = plan("BANK", "BANK", nbfc=True)
    assert (
        "an NBFC" in p["Discounted cash flow (FCFF)"]["reason"]
        and p["Residual income (justified P/B)"]["applies"]
    )


@pytest.mark.parametrize(
    ("sector", "sotp_applies"),
    [("IT_SERVICES", False), ("FMCG", False), ("AUTOMOBILES", True), ("ENERGY", True)],
)
def test_non_financial_sectors_get_dcf_ev_ebitda_and_pe_and_sotp_only_where_appropriate(
    sector, sotp_applies
):
    p = plan(sector, "EARNINGS")
    assert p["Discounted cash flow (FCFF)"]["applies"] and p["EV/EBITDA (current, historical, peers)"]["applies"] and p["Historical P/E"]["applies"]  # fmt: skip
    assert (
        p["Sum of the parts"]["applies"] is sotp_applies
        and p["Residual income (justified P/B)"]["applies"] is False
    )
    if not sotp_applies:
        assert "essentially one business" in p["Sum of the parts"]["reason"]


def test_the_plan_reports_completion_from_the_blocks():
    p = plan("IT_SERVICES", "EARNINGS", dcf={"completed": True}, relative_ev_ebitda={"completed": False, "reason": "needs stored net debt"}, historical={"available": True})  # fmt: skip
    assert p["Discounted cash flow (FCFF)"]["status"] == "COMPLETED"
    assert p["Relative scenarios on EV/EBITDA"]["status"] == "NOT_COMPLETED" and p["Relative scenarios on EV/EBITDA"]["reason"] == "needs stored net debt"  # fmt: skip
    assert p["Historical P/E"]["status"] == "COMPLETED"


# ---- the calculation of EV and EV/EBITDA --------------------------------------------------------------------------


def test_the_report_valuation_carries_the_new_blocks(seeded, monkeypatch):
    from app.research import clock, valuation
    from app.research.sectors import sector_for

    monkeypatch.setattr(clock, "today", lambda: TODAY)
    co = priced_company(seeded)
    put(seeded, co, "BALANCE.owners_equity", [1000 * CRORE, 1200 * CRORE, 1500 * CRORE])
    put(seeded, co, "BALANCE.shares_outstanding", [100 * CRORE] * 3, unit="ratio")
    for end, px in zip(FY, (20, 18, 30), strict=True):
        add_price(seeded, co, end, D(px))
    put(
        seeded,
        co,
        "VALUATION.price_to_book",
        [2],
        unit="x",
        years=[date(2026, 11, 6)],
        ptype="QUARTERLY",
    )
    st = store(seeded, co, relative.NAMES | dcf.NAMES | {"PRICE.close", "VALUATION.price_to_book", "VALUATION.pe_annual", "VALUATION.pe_ttm", "INCOME.eps_basic"} | {"INCOME.revenue_from_operations"})  # fmt: skip
    v = valuation.build(seeded, co.id, st, sector_for("AUTOMOBILES"), "CONSOLIDATED", [], {})
    assert v["historical_pb"]["available"] and len(v["historical_pb"]["rows"]) == 3
    assert "pb" in v["historical_context"] and v["historical_context"]["pb"]["available"]
    assert v["relative_pb"]["completed"] is False and v["relative_ev_ebitda"]["completed"] is False
    assert v["sotp"]["applicable"] is False
    methods = {m["method"]: m["status"] for m in v["method_plan"]}
    assert (
        methods["Historical P/B"] == "COMPLETED" and methods["Sum of the parts"] == "NOT_COMPLETED"
    )
    bank = valuation.build(seeded, co.id, st, sector_for("BANK"), "CONSOLIDATED", [], {})
    assert (
        bank["historical_ev_ebitda"]["available"] is False and bank["sotp"]["applicable"] is False
    )
    assert bank["relative_pb"]["in_verdict"] is True and v["relative_pb"]["in_verdict"] is False


def test_the_verdict_counts_the_new_methods_but_not_pb_for_a_non_financial():
    from app.research import verdict

    def blk(v, **kw):
        return {"completed": True, "scenarios": {"base": {"value_per_share": v}}, **kw}

    val = {"intrinsic": {}, "relative": blk("100"), "dcf": blk("120"), "relative_pb": blk("90", in_verdict=False), "relative_ev_ebitda": blk("110"), "sotp": blk("130")}  # fmt: skip
    assert sorted(verdict._values(val, "base")) == [D(100), D(110), D(120), D(130)]
    val["relative_pb"]["in_verdict"] = True
    assert D(90) in verdict._values(val, "base")


def test_the_pdf_shows_the_plan_historical_comparison_relative_scenarios_and_sotp(seeded):
    from tests.test_report_context import _axis, _flat

    co = seg_company(seeded, "Pdf Sotp Co")
    enter_multiples(seeded, co, "base", {"Passenger Vehicles": 10, "Farm Equipment": 12})
    content = _axis()
    val = content["analysis"]["valuation"]
    val["method_plan"] = relative.method_plan("AUTOMOBILES", "EARNINGS", False, {})
    val["historical_pb"] = {"available": True, "rows": [{"fy": f"FY{y}", "price_date": f"{y}-03-30", "pb": str(1 + i), "evidence_ids": [i]} for i, y in enumerate((2024, 2025, 2026), 1)], "formula": "P/B at year end = x"}  # fmt: skip
    val["historical_context"] = {
        "pb": relative.context(val["historical_pb"], {"value": "2.5"}, "pb", "P/B")
    }
    val["relative_ev_ebitda"] = relative.relative_ev_ebitda(
        actuals(), Rec(400 * CRORE, 20), peer_row([6, 8, 10])
    )
    val["sotp"] = json.loads(json.dumps(run_sotp(seeded, co)))
    flat = _flat(content)
    assert (
        "Valuation methods for this sector" in flat
        and "Historical P/B" in flat
        and "Historical valuation comparison" in flat
    )
    assert (
        "Relative valuation on EV/EBITDA" in flat
        and "Sum of the parts" in flat
        and "Every multiple offset by" in flat
    )
