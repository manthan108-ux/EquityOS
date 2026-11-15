"""CP13: HOLD / WATCH rules, target price, scenarios, horizon, risk assessment, triggers, thesis risk
families, and the back-test and threshold validation."""

from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

from app.research import backtest as bt
from app.research import thesis as th
from tests.test_sector import seeded  # noqa: F401,E402
from tests.test_verdict import analysis, run, score


def test_a_fair_price_with_enough_evidence_is_a_hold(monkeypatch):
    out = run(monkeypatch, analysis(score=50, traj="mixed", better=1, worse=1, base=105))
    assert out["verdict"] == "HOLD"
    assert out["levels"]["target_price"]["kind"] == "base-case value"
    assert out["levels"]["target_price"]["value"] == "102"  # one method: half of the gap to 105 counts
    assert out["levels"]["target_price"]["single_method"] is True
    assert out["confidence"]["band"] in ("HIGH", "MEDIUM", "LOW")


def test_no_valuation_is_a_watch_not_a_hold(monkeypatch):
    a = analysis(score=50, traj="mixed", better=1, worse=1, intrinsic=False)
    out = run(monkeypatch, a)
    assert out["verdict"] == "WATCH"
    assert any("no valuation could be computed" in r for r in out["reasons"])
    assert out["levels"]["target_price"]["available"] is False


def test_a_valuation_gap_the_other_evidence_does_not_confirm_is_a_watch(monkeypatch):
    flat = {"available": True, "last_close": "100", "sma200": "100", "atr14": "2"}
    out = run(monkeypatch, analysis(score=50, traj="mixed", better=1, worse=1, base=130), score(50), ctx=flat)
    assert out["verdict"] == "WATCH"
    assert any("not confirmed by the other evidence" in r for r in out["reasons"])


def test_buy_target_price_equals_the_trade_target_and_carries_the_range(monkeypatch):
    out = run(monkeypatch, analysis(rel_base=130))
    tp = out["levels"]["target_price"]
    assert out["verdict"] == "BUY" and tp["value"] == out["levels"]["target"] == "130"
    assert tp["range"]["bear"] == "80" and tp["review_horizon_months"] == 12
    assert "convention" in out["horizon"]["statement"] or out["horizon"]["kind"] == "CONVENTION"


def test_a_sell_target_price_is_the_downside_value(monkeypatch):
    a = analysis(score=30, traj="deteriorating", better=0, worse=3, base=70, bear=60, rel_base=70)
    out = run(monkeypatch, a, score(30), ctx={"available": True, "last_close": "100", "sma200": "130", "atr14": "10"})  # fmt: skip
    assert out["verdict"] == "SELL"
    assert out["levels"]["target_price"]["kind"].startswith("downside")
    assert out["levels"]["target_price"]["value"] == "60"


def test_scenarios_carry_values_and_no_probabilities(monkeypatch):
    out = run(monkeypatch, analysis(rel_base=130))
    rows = {r["case"]: r for r in out["scenarios"]["rows"]}
    assert rows["base"]["value_per_share"] == "130" and rows["bear"]["vs_price_pct"] == "-20"
    assert out["scenarios"]["complete"] is True
    assert "probab" in out["scenarios"]["note"] and "no probability" in out["scenarios"]["note"]


def test_risk_assessment_is_points_from_stated_factors(monkeypatch):
    out = run(monkeypatch, analysis(rel_base=130, bear=60))
    risk = out["risk"]
    keys = {f["key"] for f in risk["factors"]}
    assert {"downside", "dispersion", "volatility", "adverse", "governance", "evidence"} <= keys
    down = next(f for f in risk["factors"] if f["key"] == "downside")
    assert down["points"] == 2  # bear 60 is 40% below 100
    gov = next(f for f in risk["factors"] if f["key"] == "governance")
    assert gov["available"] is False  # no governance block in the fixture
    assert risk["level"] in ("HIGH", "MODERATE", "LOW", "NOT ESTABLISHED")


def test_risk_is_not_established_when_too_little_can_be_measured(monkeypatch):
    out = run(monkeypatch, analysis(intrinsic=False), ctx={"available": False, "reason": "no history"})  # fmt: skip
    assert out["risk"]["level"] == "NOT ESTABLISHED"


def test_triggers_name_what_would_change_the_verdict(monkeypatch):
    out = run(monkeypatch, analysis(score=50, traj="mixed", better=1, worse=1, base=105))
    items = {t["component"]: t for t in out["triggers"]}
    assert "Financial score" in items and "60" in items["Financial score"]["changes_when"]
    assert any(t["component"] == "Price" for t in out["triggers"])
    run_buy = run(monkeypatch, analysis(rel_base=130))
    assert any(t["component"] == "Invalidation" for t in run_buy["triggers"])


def test_the_verdict_carries_the_backtest_summary_and_thresholds(monkeypatch):
    out = run(monkeypatch, analysis(rel_base=130))
    assert "available" in out["backtest"] and out["backtest"]["limitations"]
    assert out["thresholds"]["net_buy"] == 3 and out["thresholds"]["valuation_upside_pct"] == "15"


# ---------------------------------------------------------------- back-test


def test_classify_uses_the_same_thresholds_and_needs_every_component():
    w = bt.WEIGHTS
    assert bt.classify({"trajectory": 1, "valuation": 1, "trend": 1}, w, 3, 3) == "BUY"
    assert bt.classify({"trajectory": -1, "valuation": -1, "trend": 0}, w, 3, 3) == "SELL"
    assert bt.classify({"trajectory": 1, "valuation": 0, "trend": 1}, w, 3, 3) == "WATCH"
    assert bt.classify({"trajectory": 1, "valuation": None, "trend": 1}, w, 3, 3) is None


def test_trend_points_follow_the_200_day_average_band():
    flat = [D(100)] * 199
    assert bt.trend_points(flat + [D(110)]) == 1
    assert bt.trend_points(flat + [D(90)]) == -1
    assert bt.trend_points(flat + [D(100)]) == 0
    assert bt.trend_points([D(100)] * 50) is None


def rec(value, end):
    return SimpleNamespace(value=D(value), period_end=end)


def test_trajectory_uses_only_results_published_by_the_as_of_date():
    prof = {date(2023, 3, 31): rec(100, date(2023, 3, 31)), date(2024, 3, 31): rec(130, date(2024, 3, 31)), date(2025, 3, 31): rec(60, date(2025, 3, 31))}  # fmt: skip
    eps = {d: rec(r.value / 10, d) for d, r in prof.items()}
    rev = {d: rec(r.value * 5, d) for d, r in prof.items()}
    # 2024-04-30 is before March 2024 results are public (31 Mar + 90 days): only one year known
    assert bt.trajectory_points([prof, eps, rev], date(2024, 4, 30)) is None
    assert bt.trajectory_points([prof, eps, rev], date(2024, 9, 30)) == 1  # +30% on all three
    assert bt.trajectory_points([prof, eps, rev], date(2025, 9, 30)) == -1  # the 2025 fall is public


def test_valuation_proxy_compares_the_current_pe_with_its_own_history():
    d0 = date(2024, 1, 31)
    hist = [(d0 + timedelta(days=30 * i), D(20)) for i in range(10)]
    cheap = [*hist, (d0 + timedelta(days=330), D(15))]  # median 20 vs 15 -> +33%
    rich = [*hist, (d0 + timedelta(days=330), D(25))]  # median 20 vs 25 -> -20%
    fair = [*hist, (d0 + timedelta(days=330), D(19))]
    assert bt.valuation_proxy(cheap, D(15))[0] == 1
    assert bt.valuation_proxy(rich, D(15))[0] == -1
    assert bt.valuation_proxy(fair, D(15))[0] == 0
    assert bt.valuation_proxy(hist[:5], D(15)) == (None, None)  # too little history


def test_excess_return_subtracts_the_same_date_mean_and_needs_three_companies():
    d = date(2025, 1, 31)
    rows = [{"symbol": s, "date": d, "fwd126": D(x)} for s, x in (("A", "0.10"), ("B", "0.00"), ("C", "-0.10"))]  # fmt: skip
    bt.add_excess(rows, 126)
    assert [round(r["ex126"], 4) for r in rows] == [0.1, 0.0, -0.1]
    two = [{"symbol": "A", "date": d, "fwd126": D("0.1")}, {"symbol": "B", "date": d, "fwd126": D("0.0")}]  # fmt: skip
    bt.add_excess(two, 126)
    assert all(r["ex126"] is None for r in two)


def test_a_small_bucket_is_insufficient_and_never_read():
    rows = [{"symbol": "A", "date": date(2025, 1, 31), "ex126": 0.2}] * 5
    s = bt.bucket_stats(rows, 126)
    assert s["n"] == 5 and s["sufficient"] is False
    stats = {"BUY": s, "WATCH": s, "SELL": s}
    assert bt._reading(stats) == "INSUFFICIENT"


def test_reading_distinguishes_consistent_from_not_supported():
    ok = {"sufficient": True}
    assert bt._reading({"BUY": {**ok, "mean_excess_pct": 4.0}, "SELL": {**ok, "mean_excess_pct": -3.0}, "WATCH": ok}) == "CONSISTENT"  # fmt: skip
    assert bt._reading({"BUY": {**ok, "mean_excess_pct": -1.0}, "SELL": {**ok, "mean_excess_pct": 2.0}, "WATCH": ok}) == "NOT SUPPORTED"  # fmt: skip


def test_threshold_validation_reports_the_reward_to_risk_interaction():
    v = bt.validate_thresholds()
    by = {c["check"]: c for c in v["checks"]}
    assert by["symmetric thresholds"]["ok"] and by["a verdict is reachable"]["ok"]
    gap = by["valuation threshold reachable at the widest stop"]
    assert gap["ok"] is False and "10.0%" in gap["detail"]  # 15% / 1.5
    assert v["all_ok"] is False


def test_the_saved_result_states_its_limitations():
    s = bt.summary()
    assert s["limitations"]
    assert any("survivorship" in x for x in s["limitations"])
    assert any("three years" in x for x in s["limitations"])


# ---------------------------------------------------------------- thesis risk families


def test_the_risk_register_has_the_four_families_and_labels_sector_context():
    from app.research.sectors import sector_for

    sector = sector_for("BANK") if callable(sector_for) else None
    a = {
        "valuation": {"intrinsic": {"completed": False}, "relative": {"completed": False}},
        "governance": {"available": True, "checklist": {"items": [{"key": "independence", "label": "Independent directors", "status": "FLAG", "detail": "only 20% are independent"}, {"key": "auditor", "label": "Auditor's report", "status": "UNKNOWN", "detail": "n/a"}]}},  # fmt: skip
        "business": {"weaknesses": {"evidence": [{"text": "Net margin contracted.", "evidence_ids": [7]}]}},
        "industry_view": {"environment": {"regulation": ["RBI capital rules apply."]}, "opportunities_threats": {"checked_against_company": [{"kind": "THREAT", "theme": "credit quality", "company_evidence": {"label": "Gross NPA", "direction": "UNFAVOURABLE", "change_from": "2", "change_to": "3", "evidence_ids": [9]}}]}},  # fmt: skip
    }
    risks = {"company": [], "sector": [{"title": "Rates", "text": "Margins move with rates.", "kind": "SECTOR TEMPLATE (not evidence about this company)"}], "valuation": [], "data": [], "governance": []}  # fmt: skip
    reg = th._risk_register(a, sector, risks, {"available": False})
    assert set(reg) == {"business_financial", "management_governance", "industry_regulatory", "valuation", "data"}  # fmt: skip
    assert any("Net margin contracted" in p["text"] for p in reg["business_financial"])
    assert any("Independent directors" in p["text"] for p in reg["management_governance"])
    assert any("gross npa" in p["text"] for p in reg["industry_regulatory"])
    assert any("SECTOR" in p["kind"] for p in reg["industry_regulatory"])
    assert any("No valuation method is completed" in p["text"] for p in reg["valuation"])


def test_bucket_statistics_report_drawdown_risk_and_the_break_even_cost():
    rows = [
        {"symbol": "A", "date": date(2025, m, 28), "ex126": e, "dd126": d}
        for m, e, d in ((1, 0.10, -0.05), (2, 0.04, -0.20), (3, -0.02, -0.10), (4, 0.08, 0.0))
    ]
    s = bt.bucket_stats(rows, 126)
    assert s["mean_excess_pct"] == 5.0
    assert s["worst_drawdown_pct"] == -20.0 and s["mean_worst_drawdown_pct"] == -8.8
    assert s["breakeven_round_trip_cost_pct"] == 5.0  # a cost above this removes the average excess
    assert s["excess_per_risk"] > 0 and s["independent_observations"] == 1
    losing = [{**r, "ex126": -0.01} for r in rows]
    assert bt.bucket_stats(losing, 126)["breakeven_round_trip_cost_pct"] == 0.0
