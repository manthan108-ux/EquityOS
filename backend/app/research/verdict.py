"""EquityOS verdict: BUY / SELL / WATCH with target, stop-loss and confidence (methodology v1).

This is a transparent rule set, not a prediction and not investment advice. It exists so that a verdict
is reproducible from stored evidence: every input, point and threshold is printed in the report.

Components (each scored -1, 0 or +1; valuation counts double):
  1 financial score        >= 60 -> +1, <= 40 -> -1
  2 financial trajectory   improving -> +1, deteriorating -> -1
  3 peer standing          (better - worse) / ranked metrics >= +1/3 -> +1, <= -1/3 -> -1
  4 valuation (weight 2)   upside of the base-case value to the price >= +15% -> +1, <= -15% -> -1
  5 price trend            close vs its 200-day average, beyond +/-5%

Net points >= +3 -> BUY, <= -3 -> SELL, otherwise WATCH. A BUY or SELL also needs at least four of the
five components to be computable, and a reward-to-risk of at least 1.5; otherwise the verdict is WATCH
and the reason is stated.

Target: the mean of the available base-case scenario values (BUY), or of the bear-case values (SELL);
with only one valuation method, half of the gap to that value is counted and confidence drops.
These are scenario-implied values under the illustrative assumptions in the valuation section, with no
time horizon. Stop-loss: 2 x the 14-day average true range from the stored price history, kept between
5% and 15% of the price. Confidence is EVIDENCE confidence (coverage, agreement, evidence quality), not
the probability that the trade works. The method has not been back-tested.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EodPrice, PriceAdjustment, Security
from app.research.analysis import _s, dec
from app.research.valuation import _material

VERSION = "1.0"
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
    "Methodology v1 is an EquityOS rule set, not a forecast or investment advice, and has not been "
    "back-tested. Confidence describes the evidence behind the verdict, not the probability that a "
    "trade succeeds. Targets are scenario-implied values with no time horizon. Do not trade on this "
    "report alone."
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


def _values(valuation: dict, case: str) -> list[Decimal]:
    out = []
    intr, rel = valuation.get("intrinsic", {}), valuation.get("relative", {})
    for block in (intr, rel):
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
    for blk in (valuation.get("intrinsic", {}), valuation.get("relative", {})):
        if blk.get("completed") and price:
            v = dec(blk["scenarios"].get("base", {}).get("value_per_share"))
            if v is not None:
                sides.append(v > price)
    return len(sides) == 2 and sides[0] != sides[1]


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
    levels |= {
        "base_value": _s(base.quantize(Decimal("1"))) if base is not None else None,
        "bear_value": _s(bear.quantize(Decimal("1"))) if bear is not None else None,
        "bull_value": _s(bull.quantize(Decimal("1"))) if bull is not None else None,
        "value_sources": [
            name
            for name, blk in (
                ("residual-income (justified P/B)", analysis["valuation"].get("intrinsic", {})),
                ("relative (peer P/E)", analysis["valuation"].get("relative", {})),
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
    if verdict == "WATCH" and base is not None and price is not None:
        levels["would_turn_buy_below"] = _s((base / (1 + UPSIDE / 100)).quantize(Decimal("1")))
        levels["would_turn_sell_above"] = _s((base / (1 - UPSIDE / 100)).quantize(Decimal("1")))
    if verdict == "WATCH" and raw == "WATCH":
        reasons.append(f"net points {net} lie between {NET_SELL} and {NET_BUY}")
    if stop is not None:
        levels |= {
            "target": _s(target.quantize(Decimal("1"))),
            "stop_loss": _s(stop.quantize(Decimal("1"))),
            "reward_to_risk": _s(rr.quantize(Decimal("0.1"))),
            "target_pct": _s(((target / price - 1) * 100).quantize(Decimal("0.1"))),
            "stop_pct": _s(((stop / price - 1) * 100).quantize(Decimal("0.1"))),
        }
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
        "confidence": conf,
        "reasons": reasons,
        "rules": (
            f"net points >= {NET_BUY} BUY, <= {NET_SELL} SELL, otherwise WATCH; at least {MIN_COMPONENTS} of 5 "
            f"components computable; reward-to-risk at least {MIN_REWARD_RISK}; stop = {ATR_MULT} x {ATR_DAYS}-day "
            f"average true range, between {int(STOP_MIN * 100)}% and {int(STOP_MAX * 100)}% of the price"
        ),
        "disclosure": DISCLOSURE,
    }
