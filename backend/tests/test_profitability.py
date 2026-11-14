# ruff: noqa: F811
"""Growth, CAGR, margins, margin change, drivers and the peer-group comparison."""

import json
from datetime import date
from decimal import Decimal as D

from app.research import profitability as pf
from app.research import readiness as rd
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

YEARS = [date(2023, 3, 31), date(2024, 3, 31), date(2025, 3, 31), date(2026, 3, 31)]
CRORE = 10_000_000


def add(session, co, metric, values, basis="CONSOLIDATED", unit="inr", years=YEARS):
    for end, v in zip(years, values, strict=False):
        if v is not None:
            evidence(session, co, metric, v, end=end, basis=basis, unit=unit)


def store(session, co):
    return rd.load_store(session, co.id, set(pf.NAMES))


def company(session, name="Subject Co", **series):
    co = mk_company(session, name)
    for metric, values in series.items():
        add(
            session,
            co,
            metric.replace("__", "."),
            values,
            unit="percent" if "MARGIN" in metric else "inr",
        )
    return co


def revenue(*vals):
    return [v * CRORE if v is not None else None for v in vals]


# ---- growth and CAGR ------------------------------------------------------------------------------------------


def test_revenue_growth_and_cagr_are_computed_from_the_stored_figures(seeded):
    co = company(seeded, INCOME__revenue_from_operations=revenue(1000, 1100, 1250, 1400))
    g = pf.growth_metric(store(seeded, co), pf.REVENUE, "CONSOLIDATED")
    assert [None if r["yoy_pct"] is None else D(r["yoy_pct"]) for r in g["series"]] == [
        None,
        D(10),
        D("13.6"),
        D(12),
    ]
    assert D(g["cagr"]["pct"]) == D("11.9") and g["cagr"]["years"] == 3
    assert (g["cagr"]["start_fy"], g["cagr"]["end_fy"]) == ("FY2023", "FY2026")
    assert D(g["cagr_3y"]["pct"]) == D("11.9")
    assert len(g["cagr"]["evidence_ids"]) == 2 and all(r["evidence_id"] for r in g["series"])


def test_a_short_history_says_so_and_a_missing_line_says_why(seeded):
    one = company(seeded, "One Year Co", INCOME__revenue_from_operations=revenue(1000))
    g = pf.growth_metric(store(seeded, one), pf.REVENUE, "CONSOLIDATED")
    assert g["available"] and "only one annual value" in g["reason"] and "cagr" not in g
    none = mk_company(seeded, "No Lines Co")
    e = pf.growth_metric(store(seeded, none), pf.EBITDA, "CONSOLIDATED")
    assert not e["available"] and "does not print the lines" in e["reason"]


def test_a_cagr_needs_a_positive_start_and_end(seeded):
    co = company(
        seeded, "Loss Co", INCOME__net_profit=[-50 * CRORE, 10 * CRORE, 40 * CRORE, 90 * CRORE]
    )
    g = pf.growth_metric(store(seeded, co), pf.PAT, "CONSOLIDATED")
    assert "cagr" in g and g["cagr"] is None and "zero or negative" in g["reason"]
    assert g["series"][1]["yoy_pct"] is None, "growth from a loss is not a percentage"
    assert D(g["series"][3]["yoy_pct"]) == D(125)


def test_a_gap_year_does_not_produce_a_fake_year_on_year_figure(seeded):
    co = company(seeded, "Gap Co", INCOME__revenue_from_operations=revenue(1000, None, 1250, 1400))
    g = pf.growth_metric(store(seeded, co), pf.REVENUE, "CONSOLIDATED")
    by_fy = {r["fy"]: r["yoy_pct"] for r in g["series"]}
    assert by_fy["FY2025"] is None and D(by_fy["FY2026"]) == D(12)
    assert g["cagr"]["years"] == 3 and "cagr_3y" not in g


def test_the_other_accounting_basis_is_never_mixed_in(seeded):
    co = mk_company(seeded, "Mixed Co")
    add(
        seeded,
        co,
        "INCOME.revenue_from_operations",
        revenue(1000, 1100),
        basis="CONSOLIDATED",
        years=YEARS[2:],
    )
    add(seeded, co, "INCOME.revenue_from_operations", revenue(10, 20, 30, 40), basis="STANDALONE")
    g = pf.growth_metric(store(seeded, co), pf.REVENUE, "CONSOLIDATED")
    assert [r["value"] for r in g["series"]] == [str(1000 * CRORE), str(1100 * CRORE)]
    assert {r["unit"] for r in g["series"]} == {"inr"} and g["basis"] == "CONSOLIDATED"
    s = pf.growth_metric(store(seeded, co), pf.REVENUE, "STANDALONE")
    assert len(s["series"]) == 4 and s["cagr"]["pct"] == "58.7"


# ---- margins and their change ------------------------------------------------------------------------------------


def margin(session, values, name="Margin Co"):
    co = company(session, name, MARGIN__net_margin=values)
    return pf.margin_metric(store(session, co), pf.M_NET, "CONSOLIDATED")


def test_a_rising_margin_is_expanding_with_the_change_in_basis_points(seeded):
    m = margin(seeded, [10.0, 11.5, 12.0, 13.5])
    assert m["direction"] == "EXPANDING" and D(m["change_pp"]) == D("3.5")
    assert [r["change_bps"] for r in m["series"]] == [None, 150, 50, 150]
    assert (m["years_up"], m["years_down"], m["years_compared"]) == (3, 0, 3)
    assert m["peak"]["fy"] == "FY2026" and m["trough"]["fy"] == "FY2023"


def test_the_thresholds_for_expansion_contraction_and_stable(seeded):
    assert margin(seeded, [10.0, 10.2, 10.3, 10.4], "A")["direction"] == "STABLE"  # +0.4
    assert margin(seeded, [10.0, 10.2, 10.3, 10.5], "B")["direction"] == "EXPANDING"  # +0.5
    assert margin(seeded, [10.0, 9.8, 9.7, 9.6], "C")["direction"] == "STABLE"  # -0.4
    assert margin(seeded, [10.0, 9.8, 9.7, 9.5], "D")["direction"] == "CONTRACTING"  # -0.5


def test_a_margin_can_end_flat_after_a_swing(seeded):
    m = margin(seeded, [12.0, 9.0, 14.0, 12.2], "Swing")
    assert m["direction"] == "STABLE" and (m["years_up"], m["years_down"]) == (1, 2)
    assert m["peak"]["fy"] == "FY2025" and m["trough"]["fy"] == "FY2024"


def test_a_company_without_the_margin_says_why_and_a_single_year_has_no_change(seeded):
    none = mk_company(seeded, "No Margin Co")
    m = pf.margin_metric(store(seeded, none), pf.M_GROSS, "CONSOLIDATED")
    assert not m["available"] and "does not print" in m["reason"]
    one = margin(seeded, [12.0], "One Co")
    assert one["available"] and "change_pp" not in one and "only one annual value" in one["reason"]


def test_observations_state_the_reading_with_the_evidence_behind_it(seeded):
    co = company(seeded, "Obs Co", MARGIN__net_margin=[10.0, 11.0, 12.5, 13.0])
    margins = {"net_margin": pf.margin_metric(store(seeded, co), pf.M_NET, "CONSOLIDATED")}
    (obs,) = pf.observations(margins)
    assert (
        "expanded by 3.0 percentage points between FY2023 and FY2026 (10.0% to 13.0%)"
        in obs["text"]
    )
    assert "rose in 3 and fell in 0 of 3 years" in obs["text"]
    assert (
        obs["kind"] == "INTERPRETATION" and len(obs["evidence_ids"]) == 2 and "0.5" in obs["rule"]
    )


# ---- what moved the margin ------------------------------------------------------------------------------------


def bridge_company(session, name="Bridge Co"):
    co = mk_company(session, name)
    years = [YEARS[0], YEARS[3]]
    add(session, co, "INCOME.revenue_from_operations", revenue(1000, 1400), years=years)
    add(session, co, "INCOME.net_profit", revenue(100, 154), years=years)  # 10.0% -> 11.0%
    add(
        session, co, "INCOME.employee_benefits_expense", revenue(300, 392), years=years
    )  # 30% -> 28%
    add(session, co, "INCOME.other_expenses", revenue(200, 294), years=years)  # 20% -> 21%
    add(session, co, "INCOME.finance_costs", revenue(50, 42), years=years)  # 5% -> 3%
    add(session, co, "INCOME.tax_expense", revenue(80, 100), years=years)  # 8% -> 7.14%
    return co


def test_the_margin_bridge_itemises_the_change_and_shows_what_is_left(seeded):
    co = bridge_company(seeded)
    d = pf.drivers(store(seeded, co), "CONSOLIDATED")
    assert d["available"] and (d["start_fy"], d["end_fy"]) == ("FY2023", "FY2026")
    assert (D(d["net_margin_start"]), D(d["net_margin_end"]), D(d["change_pp"])) == (
        D(10),
        D(11),
        D(1),
    )
    items = {i["line"]: D(i["contribution_pp"]) for i in d["items"]}
    assert items["Employee benefits"] == D("2.00") and items["Other expenses"] == D("-1.00")
    assert items["Finance costs"] == D("2.00") and items["Tax"].quantize(D("0.01")) == D("0.86")
    total = sum(items.values())
    assert abs(total + D(d["not_itemised_pp"]) - D(d["change_pp"])) <= D("0.011")
    assert d["items"][0]["evidence_ids"], "every line points at its evidence"
    assert list(items)[0] == "Employee benefits" or abs(list(items.values())[0]) >= abs(
        list(items.values())[-1]
    )


def test_too_few_cost_lines_means_no_bridge_not_a_guess(seeded):
    co = mk_company(seeded, "Thin Bridge Co")
    years = [YEARS[0], YEARS[3]]
    add(seeded, co, "INCOME.revenue_from_operations", revenue(1000, 1400), years=years)
    add(seeded, co, "INCOME.net_profit", revenue(100, 154), years=years)
    add(seeded, co, "INCOME.employee_benefits_expense", revenue(300, 392), years=years)
    d = pf.drivers(store(seeded, co), "CONSOLIDATED")
    assert not d["available"] and "fewer than three cost lines" in d["reason"]


# ---- the peer group ------------------------------------------------------------------------------------------


def peers(session, series_by_peer, **kw):
    out = []
    for i, values in enumerate(series_by_peer):
        co = company(
            session, f"Peer {i}", **{k: v(values) if callable(v) else v for k, v in kw.items()}
        )
        out.append((co, store(session, co)))
    return out


def test_growth_is_compared_with_the_peer_group_and_never_called_industry(seeded):
    own = company(seeded, INCOME__revenue_from_operations=revenue(1000, 1100, 1250, 1400))
    group = [
        company(
            seeded,
            f"P{i}",
            INCOME__revenue_from_operations=revenue(*[1000 * (1 + g) ** n for n in range(4)]),
        )
        for i, g in enumerate((0.05, 0.07, 0.09, 0.11))
    ]
    peer_stores = [(p, store(seeded, p)) for p in group]
    block = pf.peer_growth(store(seeded, own), peer_stores, pf.REVENUE, "CONSOLIDATED")
    assert (
        block["peer_benchmark"] and block["benchmark"] == "PEER_GROUP" and block["peer_count"] == 4
    )
    assert D(block["peer_group_median_cagr"]) == D(8)
    assert D(block["difference_vs_peer_median_pp"]) == D("3.9")  # 11.9 - 8.0
    assert {r["name"] for r in block["peers"]} == {f"P{i}" for i in range(4)}
    text = json.dumps(block).lower()
    assert "alpha" not in text and "industry" not in text


def test_fewer_than_three_peers_give_no_benchmark_and_say_why(seeded):
    own = company(seeded, INCOME__revenue_from_operations=revenue(1000, 1100, 1250, 1400))
    group = [
        company(seeded, f"P{i}", INCOME__revenue_from_operations=revenue(1000, 1100, 1200, 1300))
        for i in range(2)
    ]
    block = pf.peer_growth(
        store(seeded, own), [(p, store(seeded, p)) for p in group], pf.REVENUE, "CONSOLIDATED"
    )
    assert (
        block["available"]
        and not block["peer_benchmark"]
        and "fewer than 3 peers" in block["reason"]
    )
    assert block["company"]["cagr"] == "11.9", "the company's own figure is still shown"


def test_peers_on_the_other_basis_do_not_count(seeded):
    own = company(seeded, INCOME__revenue_from_operations=revenue(1000, 1100, 1250, 1400))
    group = []
    for i in range(4):
        p = mk_company(seeded, f"Standalone Peer {i}")
        add(
            seeded,
            p,
            "INCOME.revenue_from_operations",
            revenue(1000, 1100, 1200, 1300),
            basis="STANDALONE",
        )
        group.append(p)
    block = pf.peer_growth(
        store(seeded, own), [(p, store(seeded, p)) for p in group], pf.REVENUE, "CONSOLIDATED"
    )
    assert not block["peer_benchmark"], (
        "four standalone peers cannot benchmark a consolidated company"
    )


def test_margin_is_compared_with_the_peer_median_for_the_same_year_and_basis(seeded):
    own = company(seeded, MARGIN__net_margin=[10.0, 11.0, 12.0, 14.0])
    group = [
        company(seeded, f"M{i}", MARGIN__net_margin=[9.0 + i, 9.5 + i, 10.0 + i, 10.0 + i])
        for i in range(4)
    ]  # FY2026 margins 10, 11, 12, 13: median 11.5
    peer_stores = [(p, store(seeded, p)) for p in group]
    own_margin = pf.margin_metric(store(seeded, own), pf.M_NET, "CONSOLIDATED")
    block = pf.peer_margin(store(seeded, own), peer_stores, pf.M_NET, "CONSOLIDATED", own_margin)
    assert block["fy"] == "FY2026" and D(block["peer_group_median"]) == D("11.5")
    assert D(block["difference_pp"]) == D("2.5") and (
        block["peers_above"],
        block["peers_below"],
    ) == (
        0,
        4,
    )
    assert D(block["window"]["company_change_pp"]) == D(4)
    assert D(block["window"]["peer_group_median_change_pp"]) == D(1)
    assert D(block["window"]["difference_pp"]) == D(3)


def test_a_margin_comparison_needs_three_peers_with_that_year(seeded):
    own = company(seeded, MARGIN__net_margin=[10.0, 11.0, 12.0, 14.0])
    group = [company(seeded, f"M{i}", MARGIN__net_margin=[9.0, 9.5, 10.0, 10.0]) for i in range(2)]
    own_margin = pf.margin_metric(store(seeded, own), pf.M_NET, "CONSOLIDATED")
    block = pf.peer_margin(
        store(seeded, own),
        [(p, store(seeded, p)) for p in group],
        pf.M_NET,
        "CONSOLIDATED",
        own_margin,
    )
    assert (
        not block["available"]
        and block["peers_with_value"] == 2
        and "fewer than 3" in block["reason"]
    )


# ---- the whole -------------------------------------------------------------------------------------------------


def test_the_whole_analysis_labels_the_benchmark_and_withholds_alpha(seeded):
    co = company(
        seeded,
        INCOME__revenue_from_operations=revenue(1000, 1100, 1250, 1400),
        INCOME__net_profit=revenue(100, 110, 130, 150),
        MARGIN__net_margin=[10.0, 10.0, 10.4, 10.7],
    )
    out = pf.build(store(seeded, co), [], "IT_SERVICES", "CONSOLIDATED")
    assert out["available"] and out["kind"] == "NON_FINANCIAL" and out["basis"] == "CONSOLIDATED"
    assert out["benchmark"]["kind"] == "PEER_GROUP" and out["benchmark"]["industry"] is None
    assert "no growth alpha is calculated" in out["benchmark"]["industry_reason"]
    assert set(out["growth"]) == {"revenue", "gross_profit", "ebitda", "ebit", "net_profit"}
    assert set(out["margins"]) == {"gross_margin", "ebitda_margin", "ebit_margin", "net_margin"}
    assert out["growth"]["gross_profit"]["available"] is False
    assert out["peer_growth"]["revenue"]["peer_benchmark"] is False
    assert any("percentage points" in d for d in out["definitions"])
    assert "industry cagr" not in json.dumps(out).lower().replace("no industry cagr", "")


def test_a_bank_is_measured_on_bank_lines_and_has_no_industrial_bridge(seeded):
    co = company(
        seeded,
        INCOME__total_income=revenue(1000, 1200, 1500, 1800),
        INCOME__net_interest_income=revenue(400, 470, 560, 650),
        INCOME__net_profit=revenue(150, 190, 240, 290),
        MARGIN__net_profit_to_income=[15.0, 15.8, 16.0, 16.1],
    )
    out = pf.build(store(seeded, co), [], "BANK", "CONSOLIDATED")
    assert out["kind"] == "BANK" and out["drivers"] is None
    assert set(out["growth"]) == {"revenue", "net_interest_income", "ppop", "net_profit"}
    assert out["growth"]["revenue"]["label"] == "Total income"
    assert out["growth"]["net_interest_income"]["cagr"]["pct"] == "17.6"
    assert "ebitda" not in out["growth"] and "ebitda_margin" not in out["margins"]


def test_a_company_with_no_stored_data_reports_not_available_rather_than_zeros(seeded):
    co = mk_company(seeded, "Empty Co")
    out = pf.build(store(seeded, co), [], "IT_SERVICES", "CONSOLIDATED")
    assert out["available"] is False
    assert all(not g["available"] for g in out["growth"].values())
    assert all(not m["available"] for m in out["margins"].values())


# ---- the report section ---------------------------------------------------------------------------------------


def test_the_pdf_shows_growth_and_profitability_with_the_peer_group_label(seeded):
    from tests.test_report_context import _axis, _flat

    co = company(
        seeded,
        INCOME__revenue_from_operations=revenue(1000, 1100, 1250, 1400),
        INCOME__net_profit=revenue(100, 110, 130, 150),
        MARGIN__net_margin=[10.0, 10.0, 10.4, 10.7],
    )
    content = _axis()
    content["analysis"]["profitability"] = json.loads(
        json.dumps(pf.build(store(seeded, co), [], "IT_SERVICES", "CONSOLIDATED"))
    )
    flat = _flat(content)
    assert "Growth and profitability" in flat and "Net profit margin" in flat
    assert "Revenue from operations" in flat and "peer group" in flat
    assert "no growth alpha is calculated" in flat.replace("  ", " ")


def test_a_report_without_profitability_data_has_no_empty_section():
    from tests.test_report_context import _axis, _flat

    content = _axis()
    content["analysis"]["profitability"] = {"available": False, "reason": "none stored"}
    assert "Growth and profitability" not in _flat(content)
