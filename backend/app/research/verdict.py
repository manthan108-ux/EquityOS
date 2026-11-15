"""EquityOS verdict: BUY / HOLD / WATCH / SELL with target price, stop-loss and confidence (methodology v2).

This is a transparent rule set, not a prediction and not investment advice. It exists so that a verdict
is reproducible from stored evidence: every input, point and threshold is printed in the report.

Components (each scored -1, 0 or +1; valuation counts double):
  1 financial score        >= 60 -> +1, <= 40 -> -1
  2 financial trajectory   improving -> +1, deteriorating -> -1
  3 peer standing          (better - worse) / ranked metrics >= +1/3 -> +1, <= -1/3 -> -1
  4 valuation (weight 2)   upside of the base-case value to the price >= +15% -> +1, <= -15% -> -1
  5 price trend            close vs its 200-day average, beyond +/-5%

Net points >= +3 -> BUY, <= -3 -> SELL, otherwise HOLD or WATCH. A BUY or SELL also needs at least four of
the five components to be computable, and a reward-to-risk of at least 1.5; otherwise the verdict is not a trade
and the reason is stated.

HOLD (v2): the evidence is adequate (at least four components computable, including a valuation) and the price
lies within +/-15% of the base-case value, with net points inside the BUY and SELL thresholds: the evidence
supports owning the shares if held, without a case to add or to exit. WATCH is everything unresolved: too little
evidence, no valuation, or components that conflict (for example a valuation gap that the other evidence does not
confirm, or a BUY that fails the reward-to-risk test).

Target: the mean of the available base-case scenario values (BUY), or of the bear-case values (SELL);
with only one valuation method, half of the gap to that value is counted and confidence drops.
The target price is the valuation-linked scenario value (base case; bear case for a SELL) from the completed
valuation methods, shown with the bear-to-bull range and a stated review horizon (a convention, not a forecast of
when the market will agree). Stop-loss: 2 x the 14-day average true range from the stored price history, kept between
5% and 15% of the price. Confidence is EVIDENCE confidence (coverage, agreement, evidence quality), not
the probability that the trade works. The rule is back-tested only as a three-component subset (see backtest.py and its limitations).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EodPrice, PriceAdjustment, Security
from app.research.analysis import _s, dec
from app.research.valuation import _material

VERSION = "2.0"
HORIZON_MONTHS = (
    12  # review horizon: a convention for stating the target, not a prediction of timing
)
SCORE_BUY, SCORE_SELL = Decimal("60"), Decimal("40")
PEER_SHARE = Decimal("0.3334")
UPSIDE = Decimal("15")
TREND_BAND = Decimal("5")
NET_BUY, NET_SELL = 3, -3
MIN_COMPONENTS = 4
MIN_REWARD_RISK = Decimal("1.5")
ATR_DAYS, ATR_MULT = 14, Decimal("2")
STOP_MIN, STOP_MAX = Decimal("0.05"), Decimal("0.15")
SMA_DAYS = 200
CONF_BANDS = (("HIGH", 85), ("MEDIUM", 55), ("LOW", 0))
DISAGREE_PENALTY = 10
SINGLE_PENALTY = 10
DISCLOSURE = (
    "Methodology v2 is an EquityOS rule set, not a forecast or investment advice. Only a three-component "
    "subset of it can be back-tested on the stored history (see the back-test summary and its limitations). "
    "Confidence describes the evidence behind the verdict, not the probability that a trade succeeds. Target "
    "prices are scenario-implied values under the stated assumptions; the review horizon is a convention. "
    "Do not trade on this report alone."
)


def _component(key, label, points, reading, weight=1):
    return {
        "key": key,
        "label": label,
        "points": points,
        "weight": weight,
        "available": points is not None,
        "reading": reading,
    }


def _comp_score(score: dict):
    if not score.get("available"):
        return _component("score", "Financial score", None, "no financial score is available")
    v = dec(score["overall"])
    pts = 1 if v >= SCORE_BUY else -1 if v <= SCORE_SELL else 0
    return _component(
        "score",
        "Financial score",
        pts,
        f"{v.quantize(Decimal('0.1'))} / 100 ({str(score['methodology']).lower()}); +1 at {SCORE_BUY} or more, "
        f"-1 at {SCORE_SELL} or less",
    )


def _comp_trajectory(thesis: dict):
    t = thesis["trajectory"]
    if t["label"] == "cannot be established":
        return _component("trajectory", "Financial trajectory", None, "no annual change is stored")
    pts = {"improving": 1, "deteriorating": -1}.get(t["label"], 0)
    return _component("trajectory", "Financial trajectory", pts, f"{t['label']} ({t['basis']})")


def _comp_peers(peers: dict):
    s = peers["summary"]
    n = s["ranked_metrics"]
    if n < 3:
        return _component(
            "peers", "Peer standing", None, f"{n} rankable metric(s); at least 3 are needed"
        )
    net = Decimal(len(s["better"]) - len(s["worse"])) / Decimal(n)
    pts = 1 if net >= PEER_SHARE else -1 if net <= -PEER_SHARE else 0
    return _component(
        "peers",
        "Peer standing",
        pts,
        f"better than the peer median on {len(s['better'])}, worse on {len(s['worse'])} of {n} rankable "
        "metrics; +1 / -1 when the net share is at least a third",
    )


def _pb(valuation: dict) -> dict:
    blk = valuation.get("relative_pb") or {}
    return blk if blk.get("in_verdict") else {}


def _blocks(valuation: dict) -> list[dict]:
    """The valuation methods whose scenario values the verdict may use."""
    return [
        valuation.get("intrinsic", {}),
        valuation.get("relative", {}),
        valuation.get("dcf") or {},
        _pb(valuation),
        valuation.get("relative_ev_ebitda") or {},
        valuation.get("sotp") or {},
    ]


def _values(valuation: dict, case: str) -> list[Decimal]:
    out = []
    for block in _blocks(valuation):
        if block.get("completed"):
            v = dec(block["scenarios"].get(case, {}).get("value_per_share"))
            if v is not None:
                out.append(v)
    return out


def _mean(vals: list[Decimal]) -> Decimal | None:
    return sum(vals) / len(vals) if vals else None


def _comp_valuation(price: Decimal | None, base: Decimal | None):
    if price is None or base is None:
        return _component("valuation", "Valuation", None, "no scenario value or no price", 2)
    up = (base / price - 1) * 100
    pts = 1 if up >= UPSIDE else -1 if up <= -UPSIDE else 0
    return _component(
        "valuation",
        "Valuation",
        pts,
        f"base-case value Rs. {base.quantize(Decimal('1'))} against price Rs. {price.quantize(Decimal('0.01'))} "
        f"({'+' if up >= 0 else ''}{up.quantize(Decimal('0.1'))}%); +1 at +{UPSIDE}% or more, -1 at -{UPSIDE}% or less",
        2,
    )


def _history(session: Session, company_id: uuid.UUID):
    secs = list(session.scalars(select(Security).where(Security.company_id == company_id)))
    for s in sorted(secs, key=lambda s: (s.security_code is not None, s.id)):
        rows = list(
            session.scalars(
                select(EodPrice)
                .where(EodPrice.security_id == s.id, EodPrice.close.is_not(None))
                .order_by(EodPrice.trade_date.desc())
                .limit(400)
            )
        )
        by_date = {}
        for r in rows:  # one row per date; the equity series first
            if r.trade_date not in by_date or r.series == "EQ":
                by_date[r.trade_date] = r
        if len(by_date) >= 20:
            return s, [by_date[d] for d in sorted(by_date)]
    return None, []


def _atr(rows) -> Decimal | None:
    trs = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        if cur.high is None or cur.low is None:
            continue
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    last = trs[-ATR_DAYS:]
    return sum(last) / len(last) if len(last) == ATR_DAYS else None


def price_context(session: Session, company_id: uuid.UUID) -> dict:
    sec, rows = _history(session, company_id)
    if not rows:
        return {"available": False, "reason": "no stored price history"}
    last = rows[-1]
    start = rows[max(0, len(rows) - SMA_DAYS)].trade_date
    adj = list(
        session.scalars(select(PriceAdjustment).where(PriceAdjustment.security_id == sec.id))
    )
    if any(a.ex_date >= start and _material(a) for a in adj):
        return {
            "available": False,
            "reason": "a corporate-action adjustment or flagged price gap falls inside the price window, so average and volatility are not comparable",
        }
    out: dict = {
        "available": True,
        "last_date": last.trade_date.isoformat(),
        "last_close": _s(last.close),
    }
    if len(rows) >= SMA_DAYS:
        window = rows[-SMA_DAYS:]
        out["sma200"] = _s(sum(r.close for r in window) / SMA_DAYS)
    atr = _atr(rows[-(ATR_DAYS + 1) :])
    if atr is not None:
        out["atr14"] = _s(atr)
    return out


def _comp_trend(ctx: dict):
    if not ctx.get("available") or "sma200" not in ctx:
        return _component(
            "trend",
            "Price trend",
            None,
            ctx.get("reason") or f"fewer than {SMA_DAYS} stored trading days",
        )
    close, sma = dec(ctx["last_close"]), dec(ctx["sma200"])
    gap = (close / sma - 1) * 100
    pts = 1 if gap > TREND_BAND else -1 if gap < -TREND_BAND else 0
    return _component(
        "trend",
        "Price trend",
        pts,
        f"close Rs. {close.quantize(Decimal('0.01'))} is {gap.quantize(Decimal('0.1'))}% from its {SMA_DAYS}-day "
        f"average of Rs. {sma.quantize(Decimal('0.01'))}; +1 above +{TREND_BAND}%, -1 below -{TREND_BAND}%",
    )


def _methods_disagree(valuation: dict, price) -> bool:
    """True when the residual-income and relative base cases lie on opposite sides of the price."""
    sides = []
    for blk in _blocks(valuation):
        if blk.get("completed") and price:
            v = dec(blk["scenarios"].get("base", {}).get("value_per_share"))
            if v is not None:
                sides.append(v > price)
    return len(sides) >= 2 and len(set(sides)) == 2


def _haircut(price, value, single):
    """With one valuation method only half of the gap to its value counts toward the target."""
    if value is None or not single:
        return value
    return price + (value - price) / 2


def _confidence(comps, verdict, readiness_score, score, disagree=False, single=False) -> dict:
    avail = [c for c in comps if c["available"]]
    coverage = Decimal(len(avail)) / Decimal(len(comps))
    if verdict == "WATCH":
        aligned = sum(1 for c in avail if c["points"] == 0)
    else:
        sign = 1 if verdict == "BUY" else -1
        aligned = sum(1 for c in avail if c["points"] == sign)
    agree = Decimal(aligned) / Decimal(len(avail)) if avail else Decimal(0)
    quality = Decimal(str(readiness_score or 0)) / 100
    pts = coverage * 40 + agree * 30 + quality * 30
    if score.get("available") and str(score.get("confidence")).upper() == "LOW":
        pts -= 10
    if disagree:
        pts -= DISAGREE_PENALTY
    if single:
        pts -= SINGLE_PENALTY
    pts = max(Decimal(0), min(Decimal(100), pts))
    band = next(name for name, floor in CONF_BANDS if pts >= floor)
    return {
        "band": band,
        "points": _s(pts.quantize(Decimal("1"))),
        "factors": {
            "coverage": f"{len(avail)} of {len(comps)} components computable (40 points)",
            "agreement": f"{aligned} of {len(avail)} computable components point the same way as the verdict (30 points)",
            "evidence_quality": f"research readiness {readiness_score} / 100 (30 points)"
            + (
                "; a LOW-confidence financial score subtracts 10"
                if score.get("available") and str(score.get("confidence")).upper() == "LOW"
                else ""
            )
            + (
                f"; the valuation methods disagree on direction, which subtracts {DISAGREE_PENALTY}"
                if disagree
                else ""
            )
            + (
                f"; only one valuation method was available, which subtracts {SINGLE_PENALTY}"
                if single
                else ""
            ),
        },
        "note": "evidence confidence, not the probability that the trade works",
    }


THRESHOLDS = {
    "net_buy": NET_BUY,
    "net_sell": NET_SELL,
    "score_buy": str(SCORE_BUY),
    "score_sell": str(SCORE_SELL),
    "valuation_upside_pct": str(UPSIDE),
    "trend_band_pct": str(TREND_BAND),
    "min_reward_risk": str(MIN_REWARD_RISK),
    "min_components": MIN_COMPONENTS,
}


def _backtest() -> dict:
    from app.research import backtest

    s = backtest.summary()
    try:
        s["threshold_validation"] = backtest.validate_thresholds()
    except Exception:  # noqa: BLE001 - never block a report on the validation notes
        s["threshold_validation"] = None
    return s


def _target_price(verdict, levels, price, base, bear, bull, single) -> dict:
    """The valuation-linked price: the trade target for BUY / SELL, the fair value (base case) otherwise."""
    if base is None or price is None:
        return {
            "available": False,
            "reason": "no completed valuation method gives a scenario value, so no valuation-linked target price can be stated",
        }
    if verdict == "SELL":
        value, kind = (bear if bear is not None else base), "downside value (mean bear case)"
    else:
        value, kind = base, "base-case value" if verdict != "BUY" else "target (mean base case)"
    value = _haircut(price, value, single)
    return {
        "available": True,
        "kind": kind,
        "value": _s(value.quantize(Decimal("1"))),
        "vs_price_pct": _s(((value / price - 1) * 100).quantize(Decimal("0.1"))),
        "range": {
            "bear": _s(bear.quantize(Decimal("1"))) if bear is not None else None,
            "bull": _s(bull.quantize(Decimal("1"))) if bull is not None else None,
        },
        "sources": levels.get("value_sources", []),
        "single_method": single,
        "review_horizon_months": HORIZON_MONTHS,
        "note": "a scenario-implied value under the entered assumptions, not a forecast of the share price; the horizon is the review period for restating it",
    }


def _scenarios(valuation: dict, price) -> dict:
    """Upside and downside from the bear / base / bull values of every completed method; no probabilities."""
    rows = []
    for case in ("bear", "base", "bull"):
        vals = _values(valuation, case)
        m = _mean(vals)
        rows.append(
            {
                "case": case,
                "value_per_share": _s(m.quantize(Decimal("1"))) if m is not None else None,
                "vs_price_pct": _s(((m / price - 1) * 100).quantize(Decimal("0.1")))
                if m is not None and price
                else None,
                "methods": len(vals),
                "low": _s(min(vals).quantize(Decimal("1"))) if vals else None,
                "high": _s(max(vals).quantize(Decimal("1"))) if vals else None,
            }
        )
    ok = all(r["value_per_share"] is not None for r in rows)
    return {
        "available": any(r["value_per_share"] is not None for r in rows),
        "complete": ok,
        "rows": rows,
        "note": "the mean of each completed method's scenario value; no probability is attached to any case because none is evidenced. Low and high are the extremes across methods.",
    }


def _horizon(valuation: dict) -> dict:
    parts = []
    if (valuation.get("dcf") or {}).get("completed"):
        parts.append(
            "the DCF explicit forecast spans 5 years, so its value is a medium- to long-term (3 to 5 year) view"
        )
    if (valuation.get("intrinsic") or {}).get("completed"):
        parts.append(
            "the residual-income value rests on a long-run ROE, growth and cost of equity, a multi-year view"
        )
    if (valuation.get("relative") or {}).get("completed") or (
        valuation.get("relative_ev_ebitda") or {}
    ).get("completed"):
        parts.append(
            "relative values apply today's peer multiples to the latest reported figures and carry no horizon of their own"
        )
    return {
        "review_months": HORIZON_MONTHS,
        "kind": "CONVENTION",
        "statement": f"The target and levels are restated every {HORIZON_MONTHS} months or on a new annual result, whichever comes first. This is a review convention, not a prediction of when the price reaches the value.",
        "method_horizons": parts,
    }


def _triggers(comps, levels, verdict) -> list[dict]:
    """What would change the verdict: the thresholds each component and the price must cross."""
    out = []
    flips = {
        "score": f"the financial score crosses {SCORE_BUY} (to +1) or {SCORE_SELL} (to -1)",
        "trajectory": "the latest annual change in profit, EPS or revenue turns clearly up or down",
        "peers": "the net share of peer metrics better or worse than the median crosses one third",
        "valuation": f"the price moves {UPSIDE}% or more away from the base-case value, or an entered valuation input changes",
        "trend": f"the close moves beyond {TREND_BAND}% either side of its {SMA_DAYS}-day average",
    }
    for c in comps:
        out.append(
            {
                "component": c["label"],
                "now": "n/a" if not c["available"] else f"{c['points']:+d}",
                "changes_when": flips[c["key"]],
            }
        )
    if levels.get("would_turn_buy_below"):
        out.append({"component": "Price", "now": levels.get("price"), "changes_when": f"the price falls to Rs. {levels['would_turn_buy_below']} or below (valuation turns positive) or rises to Rs. {levels['would_turn_sell_above']} or above (valuation turns negative)"})  # fmt: skip
    if levels.get("stop_loss"):
        out.append({"component": "Invalidation", "now": levels.get("price"), "changes_when": f"the price reaches Rs. {levels['stop_loss']} (the stop level): the {verdict} case is abandoned"})  # fmt: skip
    out.append({"component": "Reports", "now": "-", "changes_when": f"a new annual or quarterly result is published, or {HORIZON_MONTHS} months pass without a restatement"})  # fmt: skip
    return out


def _risk(levels, ctx, price, analysis, score, scen) -> dict:
    """Risk assessment from stated factors: HIGH / MODERATE / LOW, or not established when too few can be measured."""
    factors = []

    def add(key, label, pts, reading, available=True):
        factors.append(
            {"key": key, "label": label, "points": pts, "available": available, "reading": reading}
        )

    bear = next((r for r in scen["rows"] if r["case"] == "bear"), None)
    if bear and bear["vs_price_pct"] is not None:
        d = Decimal(bear["vs_price_pct"])
        add(
            "downside",
            "Downside to the bear-case value",
            2 if d <= -30 else 1 if d <= -15 else 0,
            f"bear-case value is {d}% from the price; 2 points at -30% or worse, 1 at -15% or worse",
        )
    else:
        add("downside", "Downside to the bear-case value", None, "no bear-case value", False)
    rows = {r["case"]: r for r in scen["rows"]}
    if scen["complete"] and rows["base"]["value_per_share"]:
        spread = (
            (Decimal(rows["bull"]["value_per_share"]) - Decimal(rows["bear"]["value_per_share"]))
            / Decimal(rows["base"]["value_per_share"])
            * 100
        )
        add(
            "dispersion",
            "Dispersion of scenario values",
            1 if spread > 60 else 0,
            f"bull minus bear is {spread.quantize(Decimal('1'))}% of the base value; 1 point above 60%",
        )
    else:
        add(
            "dispersion",
            "Dispersion of scenario values",
            None,
            "scenario values are incomplete",
            False,
        )
    atr = dec(ctx.get("atr14")) if ctx.get("available") else None
    if atr is not None and price:
        f = atr / price * 100
        add(
            "volatility",
            "Price volatility (14-day average true range)",
            2 if f >= 3 else 1 if f >= 1.5 else 0,
            f"average daily range is {f.quantize(Decimal('0.1'))}% of the price; 2 points at 3% or more, 1 at 1.5% or more",
        )
    else:
        add(
            "volatility",
            "Price volatility (14-day average true range)",
            None,
            ctx.get("reason") or "no price range is stored",
            False,
        )
    bear_pts = len((analysis.get("thesis") or {}).get("bear") or [])
    add(
        "adverse",
        "Adverse evidence-based points",
        1 if bear_pts >= 4 else 0,
        f"{bear_pts} adverse evidence-based point(s) in the bear case; 1 point at four or more",
    )
    gov_block = analysis.get("governance") or {}
    if gov_block.get("available"):
        gov = [
            i for i in (gov_block.get("checklist") or {}).get("items", []) if i["status"] == "FLAG"
        ]
        add(
            "governance",
            "Governance checklist flags",
            1 if gov else 0,
            f"{len(gov)} flagged item(s)"
            + (": " + ", ".join(i["label"] for i in gov) if gov else "")
            + "; 1 point for any flag",
        )
    else:
        add(
            "governance",
            "Governance checklist flags",
            None,
            "governance analysis is not available",
            False,
        )
    low_conf = bool(score.get("available") and str(score.get("confidence")).upper() == "LOW")
    add(
        "evidence",
        "Evidence confidence of the financial score",
        1 if low_conf else 0,
        "the financial score has LOW confidence" if low_conf else "no low-confidence flag",
    )
    avail = [f for f in factors if f["available"]]
    pts = sum(f["points"] for f in avail)
    if len(avail) < 4 or not factors[0]["available"]:
        level = "NOT ESTABLISHED"  # without a bear-case value the largest risk, the downside, is unmeasured
    else:
        level = "HIGH" if pts >= 5 else "MODERATE" if pts >= 3 else "LOW"
    return {
        "level": level,
        "points": pts,
        "measurable": f"{len(avail)} of {len(factors)} factors measurable",
        "factors": factors,
        "rule": "HIGH at 5 or more points, MODERATE at 3 or 4, LOW at 2 or fewer; not established when fewer than four factors can be measured or when there is no bear-case value to measure the downside against. The points and bands are EquityOS conventions.",
    }


def _price_freshness(price_date: str | None, today: date | None = None) -> dict:
    """How many trading days the reference price lags the latest expected close (0 = current)."""
    from app.pipeline import prices

    if not price_date:
        return {"price_stale": None, "price_lag_days": None}
    today = today or date.today()
    expected = prices.last_expected_trading_day(today)
    lag = len(prices.weekdays_between(date.fromisoformat(price_date), expected)) if date.fromisoformat(price_date) < expected else 0  # fmt: skip
    return {
        "price_stale": lag > 0,
        "price_lag_days": lag,
        "price_expected_date": expected.isoformat(),
    }


def build(
    session: Session,
    company_id: uuid.UUID,
    analysis: dict,
    *,
    score: dict,
    readiness_score,
) -> dict:
    snap = analysis["valuation"]["snapshot"]
    price = dec(snap["price"]["value"]) if snap.get("price") else None
    ctx = price_context(session, company_id)
    comps = [
        _comp_score(score),
        _comp_trajectory(analysis["thesis"]),
        _comp_peers(analysis["peers"]),
        _comp_valuation(price, _mean(_values(analysis["valuation"], "base"))),
        _comp_trend(ctx),
    ]
    net = sum(c["points"] * c["weight"] for c in comps if c["available"])
    n_avail = sum(c["available"] for c in comps)
    raw = "BUY" if net >= NET_BUY else "SELL" if net <= NET_SELL else "WATCH"
    verdict, reasons = raw, []
    if raw != "WATCH" and n_avail < MIN_COMPONENTS:
        verdict = "WATCH"
        reasons.append(
            f"only {n_avail} of 5 components are computable; {MIN_COMPONENTS} are needed for a BUY or SELL"
        )

    base = _mean(_values(analysis["valuation"], "base"))
    bear = _mean(_values(analysis["valuation"], "bear"))
    bull = _mean(_values(analysis["valuation"], "bull"))
    levels: dict = {
        "price": _s(price) if price else None,
        "price_date": snap["price"]["period_end"] if snap.get("price") else None,
    }
    levels |= _price_freshness(levels["price_date"])
    levels |= {
        "base_value": _s(base.quantize(Decimal("1"))) if base is not None else None,
        "bear_value": _s(bear.quantize(Decimal("1"))) if bear is not None else None,
        "bull_value": _s(bull.quantize(Decimal("1"))) if bull is not None else None,
        "value_sources": [
            name
            for name, blk in (
                ("residual-income (justified P/B)", analysis["valuation"].get("intrinsic", {})),
                ("relative (peer P/E)", analysis["valuation"].get("relative", {})),
                ("discounted cash flow (FCFF)", analysis["valuation"].get("dcf") or {}),
                ("relative (peer P/B)", _pb(analysis["valuation"])),
                (
                    "relative (peer EV/EBITDA)",
                    analysis["valuation"].get("relative_ev_ebitda") or {},
                ),
                ("sum of the parts", analysis["valuation"].get("sotp") or {}),
            )
            if blk.get("completed")
        ],
    }
    stop = rr = target = None
    single = len(levels["value_sources"]) == 1
    levels["single_method"] = single
    atr = dec(ctx.get("atr14")) if ctx.get("available") else None
    if price is not None and atr is not None:
        frac = max(STOP_MIN, min(STOP_MAX, ATR_MULT * atr / price))
        levels["atr14"] = ctx["atr14"]
        levels["stop_distance_pct"] = _s((frac * 100).quantize(Decimal("0.1")))
        if verdict == "BUY":
            stop, target = price * (1 - frac), _haircut(price, base, single)
        elif verdict == "SELL":
            stop, target = price * (1 + frac), _haircut(price, bear, single)
    if verdict in ("BUY", "SELL"):
        if stop is None or target is None:
            verdict, stop, target, rr = "WATCH", None, None, None
            reasons.append("a target or a volatility stop cannot be computed from the stored data")
        else:
            reward = (target - price) if verdict == "BUY" else (price - target)
            risk = (price - stop) if verdict == "BUY" else (stop - price)
            rr = reward / risk if risk > 0 else None
            if rr is None or rr < MIN_REWARD_RISK:
                reasons.append(
                    f"reward-to-risk {rr.quantize(Decimal('0.1')) if rr is not None else 'n/a'} is below the minimum {MIN_REWARD_RISK}"
                )
                verdict, stop, target, rr = "WATCH", None, None, None
    if verdict in ("WATCH", "HOLD") and base is not None and price is not None:
        levels["would_turn_buy_below"] = _s((base / (1 + UPSIDE / 100)).quantize(Decimal("1")))
        levels["would_turn_sell_above"] = _s((base / (1 - UPSIDE / 100)).quantize(Decimal("1")))
    val_comp = next(c for c in comps if c["key"] == "valuation")
    if verdict == "WATCH" and raw == "WATCH":
        if n_avail >= MIN_COMPONENTS and val_comp["available"] and val_comp["points"] == 0:
            verdict = "HOLD"
            reasons.append(
                f"net points {net} lie between {NET_SELL} and {NET_BUY} and the price is within {UPSIDE}% of the base-case value, so there is no case to add or to exit"
            )
        else:
            if not val_comp["available"]:
                reasons.append(
                    "no valuation could be computed, so the evidence cannot say whether the price is fair"
                )
            elif val_comp["points"] != 0:
                reasons.append(
                    "the valuation gap is not confirmed by the other evidence (net points inside the thresholds)"
                )
            if n_avail < MIN_COMPONENTS:
                reasons.append(
                    f"only {n_avail} of 5 components are computable; {MIN_COMPONENTS} are needed for a HOLD"
                )
            reasons.append(f"net points {net} lie between {NET_SELL} and {NET_BUY}")
    elif verdict == "WATCH" and raw != "WATCH":
        reasons.append(
            "a BUY or SELL case was found but did not pass its guards, so the evidence is conflicting"
        )
    if stop is not None:
        levels |= {
            "target": _s(target.quantize(Decimal("1"))),
            "stop_loss": _s(stop.quantize(Decimal("1"))),
            "reward_to_risk": _s(rr.quantize(Decimal("0.1"))),
            "target_pct": _s(((target / price - 1) * 100).quantize(Decimal("0.1"))),
            "stop_pct": _s(((stop / price - 1) * 100).quantize(Decimal("0.1"))),
        }
    levels["target_price"] = _target_price(verdict, levels, price, base, bear, bull, single)
    scen = _scenarios(analysis["valuation"], price)
    horizon = _horizon(analysis["valuation"])
    risk = _risk(levels, ctx, price, analysis, score, scen)
    conf = _confidence(
        comps,
        verdict,
        readiness_score,
        score,
        _methods_disagree(analysis["valuation"], price),
        single,
    )
    return {
        "version": VERSION,
        "verdict": verdict,
        "net_points": net,
        "components": comps,
        "levels": levels,
        "scenarios": scen,
        "horizon": horizon,
        "risk": risk,
        "triggers": _triggers(comps, levels, verdict),
        "backtest": _backtest(),
        "thresholds": THRESHOLDS,
        "confidence": conf,
        "reasons": reasons,
        "rules": (
            f"net points >= {NET_BUY} BUY, <= {NET_SELL} SELL, otherwise WATCH; at least {MIN_COMPONENTS} of 5 "
            f"components computable; reward-to-risk at least {MIN_REWARD_RISK}; stop = {ATR_MULT} x {ATR_DAYS}-day "
            f"average true range, between {int(STOP_MIN * 100)}% and {int(STOP_MAX * 100)}% of the price"
        ),
        "disclosure": DISCLOSURE,
    }
