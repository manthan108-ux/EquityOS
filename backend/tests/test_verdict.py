"""The BUY / SELL / WATCH verdict, target, stop-loss and confidence (methodology v1)."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.research import verdict as vd
from app.research.pdf_pro import render
from tests.test_research_pdf import pdf_text
from tests.test_sector import seeded  # noqa: F401

pymupdf = pytest.importorskip("pymupdf")


def analysis(
    *, score=70, traj="improving", better=3, worse=0, ranked=3, price=100, base=130, bear=80,
    rel_base=None, intrinsic=True,
):  # fmt: skip
    def block(v, b):
        return {
            "completed": True,
            "scenarios": {
                "base": {"value_per_share": str(v)},
                "bear": {"value_per_share": str(b)},
                "bull": {"value_per_share": str(v * 1.2)},
            },
        }

    val = {
        "snapshot": {"price": {"value": str(price), "period_end": "2026-11-06"}},
        "intrinsic": block(base, bear) if intrinsic else {"completed": False},
        "relative": block(rel_base, bear) if rel_base is not None else {"completed": False},
    }
    return {
        "valuation": val,
        "thesis": {"trajectory": {"label": traj, "basis": "test"}},
        "peers": {"summary": {"ranked_metrics": ranked, "better": ["m"] * better, "worse": ["m"] * worse, "in_line": []}},
    }  # fmt: skip


def score(v=70, conf="MEDIUM"):
    return {"available": True, "overall": str(v), "methodology": "RELATIVE", "confidence": conf}


def run(monkeypatch, a, sc=None, *, ctx=None, readiness=90):
    ctx = ctx or {
        "available": True,
        "last_date": "2026-11-06",
        "last_close": "100",
        "sma200": "80",
        "atr14": "2",
    }
    monkeypatch.setattr(vd, "price_context", lambda session, cid: ctx)
    return vd.build(None, None, a, score=sc or score(), readiness_score=readiness)


def test_a_strong_set_of_components_is_a_buy_with_target_stop_and_reward_to_risk(monkeypatch):
    out = run(monkeypatch, analysis(rel_base=130))
    assert out["verdict"] == "BUY" and out["net_points"] == 6
    lv = out["levels"]
    assert lv["target"] == "130" and lv["stop_loss"] == "95"  # 2 x ATR 2 = 4% -> floored at 5%
    assert D(lv["reward_to_risk"]) == D("6.0") and D(lv["stop_pct"]) == D("-5")
    assert (
        out["confidence"]["band"] == "HIGH" and "not the probability" in out["confidence"]["note"]
    )


def test_a_weak_set_is_a_sell_with_a_downside_target_and_a_stop_above_the_price(monkeypatch):
    a = analysis(score=30, traj="deteriorating", better=0, worse=3, base=70, bear=60, rel_base=70)
    out = run(
        monkeypatch,
        a,
        score(30),
        ctx={"available": True, "last_close": "100", "sma200": "130", "atr14": "10"},
    )
    assert out["verdict"] == "SELL"
    lv = out["levels"]
    assert lv["target"] == "60" and D(lv["stop_loss"]) == D(
        "115"
    )  # ATR 10 x 2 = 20% -> capped at 15%
    assert D(lv["stop_pct"]) == D("15.0") and D(lv["reward_to_risk"]) > D("1.5")


def test_the_middle_is_a_watch_with_trigger_prices_and_no_trade_levels(monkeypatch):
    out = run(monkeypatch, analysis(score=50, traj="mixed", better=1, worse=1, base=108))
    assert (
        out["verdict"] == "WATCH"
        and "target" not in out["levels"]
        and "stop_loss" not in out["levels"]
    )
    assert out["levels"]["would_turn_buy_below"] == "94"  # 108 / 1.15
    assert out["levels"]["would_turn_sell_above"] == "127"  # 108 / 0.85
    assert any("net points" in r for r in out["reasons"])


def test_a_trade_with_poor_reward_to_risk_is_downgraded_to_watch(monkeypatch):
    out = run(monkeypatch, analysis(base=105, bear=95))  # +5% target against a 5% stop: 1.0
    assert out["verdict"] == "WATCH"
    assert any("reward-to-risk" in r and "below the minimum 1.5" in r for r in out["reasons"])
    assert "target" not in out["levels"]


def test_too_few_computable_components_cannot_be_a_trade(monkeypatch):
    out = run(monkeypatch, analysis(ranked=1), ctx={"available": False, "reason": "no history"})
    assert [c["available"] for c in out["components"]] == [True, True, False, True, False]
    assert out["verdict"] == "WATCH" and any("only 3 of 5" in r for r in out["reasons"])


def test_without_price_volatility_no_stop_can_be_set_so_no_trade(monkeypatch):
    ctx = {"available": True, "last_close": "100", "sma200": "80"}  # no ATR
    out = run(monkeypatch, analysis(), ctx=ctx)
    assert out["verdict"] == "WATCH" and any("volatility stop" in r for r in out["reasons"])


def test_valuation_counts_double_and_uses_the_mean_of_available_base_cases(monkeypatch):
    out = run(monkeypatch, analysis(base=140, rel_base=100))  # mean 120 -> +20%
    val = next(c for c in out["components"] if c["key"] == "valuation")
    assert val["points"] == 1 and val["weight"] == 2 and "Rs. 120" in val["reading"]
    assert out["levels"]["value_sources"] == [
        "residual-income (justified P/B)",
        "relative (peer P/E)",
    ]


def test_confidence_drops_when_valuation_methods_disagree_or_the_score_is_low(monkeypatch):
    agree = run(monkeypatch, analysis(base=130, rel_base=125))["confidence"]
    split = run(monkeypatch, analysis(base=130, rel_base=70))["confidence"]
    assert (
        int(split["points"]) < int(agree["points"])
        and "disagree" in split["factors"]["evidence_quality"]
    )
    low = run(monkeypatch, analysis(), score(70, "LOW"))["confidence"]
    assert "LOW-confidence financial score" in low["factors"]["evidence_quality"]
    thin = run(monkeypatch, analysis(ranked=0), readiness=40)["confidence"]
    assert thin["band"] in ("LOW", "MEDIUM") and int(thin["points"]) < int(agree["points"])


def test_missing_inputs_are_not_treated_as_zero(monkeypatch):
    out = run(monkeypatch, analysis(), {"available": False, "reason": "none"})
    first = out["components"][0]
    assert first["available"] is False and first["points"] is None  # not scored as 0


def test_the_disclosure_and_rules_travel_with_every_verdict(monkeypatch):
    out = run(monkeypatch, analysis())
    assert "investment advice" in out["disclosure"] and "back-tested" in out["disclosure"]
    assert "reward-to-risk at least 1.5" in out["rules"] and out["version"] == vd.VERSION


def test_the_volatility_and_trend_inputs_come_from_stored_prices(seeded):  # noqa: F811
    from sqlalchemy import select

    from app.models import Company, EodPrice, Security
    from app.seeds.loader import seed_reference_data, seed_test_universe

    seed_reference_data(seeded)
    seed_test_universe(seeded)
    c = seeded.scalar(select(Company).where(Company.common_name == "Reliance Industries"))
    sec = seeded.scalars(select(Security).where(Security.company_id == c.id)).first()
    seeded.query(EodPrice).filter(EodPrice.security_id == sec.id).delete()
    day = date(2026, 1, 1)
    for i in range(210):
        px = D(100) + D(i) / 10
        seeded.add(
            EodPrice(
                security_id=sec.id,
                trade_date=day + timedelta(days=i),
                series="EQ",
                open=px,
                high=px + 2,
                low=px - 2,
                close=px,
                last=px,
                source_url="test://",
                file_sha256="0" * 64,
                retrieved_at=datetime.now(UTC),
            )
        )
    seeded.flush()
    ctx = vd.price_context(seeded, c.id)
    assert (
        ctx["available"] and D(ctx["atr14"]) == D("4") and "sma200" in ctx
    )  # high-low = 4 every day
    assert D(ctx["last_close"]) > D(ctx["sma200"])  # a rising series ends above its average


def test_the_pdf_shows_the_verdict_block_with_trade_levels_for_a_buy(monkeypatch):
    path = Path(__file__).parent / "fixtures" / "axis_report_v2.json"
    c = json.loads(path.read_text(encoding="utf-8"))
    out = run(monkeypatch, analysis())
    c["analysis"]["verdict"] = out
    flat = pdf_text(
        render(c, version=2, mode="COMPLETE", report_id="0" * 36, content_hash="d" * 64)
    )[0].replace("\n", " ")
    assert "EquityOS verdict" in flat and "BUY" in flat and "Stop-loss" in flat and "Target" in flat
    assert "Reward-to-risk" in flat and "Evidence confidence: HIGH" in flat
    assert "investment advice" in flat and "has not been back-tested" in flat
    assert "EquityOS verdict: BUY" in flat  # in the executive summary too


def test_a_sell_without_a_downside_scenario_becomes_a_watch_instead_of_failing(monkeypatch):
    a = analysis(score=30, traj="deteriorating", better=0, worse=3, base=70, bear=60)
    a["valuation"]["intrinsic"]["scenarios"].pop("bear")  # no bear-case value stored
    out = run(
        monkeypatch,
        a,
        score(30),
        ctx={"available": True, "last_close": "100", "sma200": "130", "atr14": "2"},
    )
    assert (
        out["verdict"] == "WATCH"
        and "target" not in out["levels"]
        and "stop_loss" not in out["levels"]
    )
    assert any("target or a volatility stop" in r for r in out["reasons"])


def test_with_one_valuation_method_only_half_the_gap_counts_and_confidence_drops(monkeypatch):
    single = run(monkeypatch, analysis(base=130))  # residual-income only
    both = run(monkeypatch, analysis(base=130, rel_base=130))
    assert (
        single["verdict"] == "BUY" and single["levels"]["target"] == "115"
    )  # 100 + (130 - 100) / 2
    assert single["levels"]["single_method"] is True and both["levels"]["target"] == "130"
    assert int(single["confidence"]["points"]) == int(both["confidence"]["points"]) - 10
    assert "only one valuation method" in single["confidence"]["factors"]["evidence_quality"]
