# ruff: noqa: E501
"""Back-test of the verdict rules on the stored history, and validation of the verdict thresholds.

What can honestly be reconstructed at a past date
-------------------------------------------------
The production verdict uses five components. Three of them cannot be rebuilt as they stood on a past
date from what EquityOS stores: the financial score and the peer standing (peer sets and scores are stored as
of today only) and the valuation scenarios (their assumptions are entered today, with no history). The back-test
therefore runs the SAME rule on the components that can be rebuilt with data dated on or before each as-of date:

  trend       close against its 200-day average, beyond +/-5% (same rule as production)
  trajectory  direction of the latest annual changes in net profit, EPS and revenue that had been PUBLISHED by the
              as-of date (period end + PUBLICATION_LAG_DAYS), same stable band as production
  valuation   a PROXY: the company's own trailing P/E (price / latest published annual EPS) against the median of
              its own monthly P/Es since the stored price history began; value = price x median / current,
              +1 / -1 at +/-UPSIDE percent (the production threshold)

This is a test of the rule's structure and thresholds on a subset, not of the production verdict as published.
Every result carries its sample size and is labelled INSUFFICIENT when it is too small to say anything.

Forward return = close H trading days later / close at the as-of date - 1; the "excess" return subtracts the mean
forward return of all companies observed on the same as-of date, which removes the market-wide movement.

Known weaknesses (also printed in the report and docs/REPORT_ENGINE.md): about three years of price history; a
universe of today's pilot companies chosen with hindsight (survivorship); overlapping windows (monthly dates, 6-12 month
horizons) so observations are strongly dependent; one market regime; no transaction costs; publication lag is a
convention; corporate-action windows are excluded rather than adjusted.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean, median, pstdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EodPrice, PriceAdjustment, Security
from app.research import readiness as rd
from app.research.analysis import STABLE_PCT, _records
from app.research.valuation import _material

VERSION = "1.0"
RESULT_FILE = Path(__file__).with_name("backtest_latest.json")
PUBLICATION_LAG_DAYS = 90  # annual results are assumed public this long after the year end
SMA_DAYS = 200
MIN_PE_HISTORY = 9  # monthly P/E observations before the as-of date needed for the valuation proxy
HORIZONS = (126, 252)  # trading days (about 6 and 12 months)
MIN_BUCKET = 30  # observations a bucket needs before its result is read at all
MIN_MONTHS = 6  # distinct as-of months a bucket needs
TREND_BAND = Decimal("5")
UPSIDE_GRID = (Decimal(5), Decimal(10), Decimal(15), Decimal(20), Decimal(25))
NET_GRID = (1, 2, 3, 4)
EPS_NAMES = ("INCOME.eps_basic",)
PROFIT_NAMES = ("INCOME.net_profit", "INCOME.summary.net_profit")
REVENUE_NAMES = ("INCOME.revenue_from_operations", "INCOME.summary.revenue", "INCOME.total_income")


# ---------------------------------------------------------------- the rule, parameterised


def classify(points: dict, weights: dict, buy: int, sell: int) -> str | None:
    """BUY / SELL / WATCH from the component points available; None when fewer than 3 of 3 are computable."""
    if any(points.get(k) is None for k in weights):
        return None
    net = sum(points[k] * w for k, w in weights.items())
    return "BUY" if net >= buy else "SELL" if net <= -buy else "WATCH"


WEIGHTS = {"trajectory": 1, "valuation": 2, "trend": 1}


# ---------------------------------------------------------------- reconstruction at a date


def _annual(store, names, basis):
    recs = _records(store, names, "ANNUAL", basis)
    return {d: r for d, r in recs.items() if r.value is not None}


def _published(series: dict, as_of: date) -> list:
    cut = as_of - timedelta(days=PUBLICATION_LAG_DAYS)
    return [series[d] for d in sorted(series) if d <= cut]


def trajectory_points(series_list: list[dict], as_of: date) -> int | None:
    ups = downs = used = 0
    for series in series_list:
        pub = _published(series, as_of)
        if len(pub) < 2 or pub[-2].value is None or pub[-2].value <= 0:
            continue
        ch = (pub[-1].value / pub[-2].value - 1) * 100
        used += 1
        if abs(ch) >= STABLE_PCT:
            ups += ch > 0
            downs += ch < 0
    if not used:
        return None
    return 1 if ups >= 2 and downs == 0 else -1 if downs >= 2 and ups == 0 else 0


def valuation_proxy(history: list[tuple[date, Decimal]], upside: Decimal) -> tuple[int | None, Decimal | None]:
    """history: monthly (date, P/E) up to and including the as-of date; returns (points, implied upside %)."""
    if len(history) < MIN_PE_HISTORY + 1:
        return None, None
    *prior, (_, cur) = history
    if cur <= 0:
        return None, None
    med = Decimal(str(median(pe for _, pe in prior)))
    up = (med / cur - 1) * 100
    return (1 if up >= upside else -1 if up <= -upside else 0), up


def trend_points(closes: list[Decimal]) -> int | None:
    if len(closes) < SMA_DAYS:
        return None
    window = closes[-SMA_DAYS:]
    gap = (closes[-1] / (sum(window) / SMA_DAYS) - 1) * 100
    return 1 if gap > TREND_BAND else -1 if gap < -TREND_BAND else 0


# ---------------------------------------------------------------- data


def _price_series(session: Session, company_id: uuid.UUID):
    secs = sorted(
        session.scalars(select(Security).where(Security.company_id == company_id)),
        key=lambda s: (s.security_code is not None, s.id),
    )
    for s in secs:
        rows = list(
            session.scalars(
                select(EodPrice)
                .where(EodPrice.security_id == s.id, EodPrice.close.is_not(None))
                .order_by(EodPrice.trade_date)
            )
        )
        by_date = {}
        for r in rows:
            if r.trade_date not in by_date or r.series == "EQ":
                by_date[r.trade_date] = r.close
        if len(by_date) >= SMA_DAYS + 60:
            adj = [
                a.ex_date
                for a in session.scalars(
                    select(PriceAdjustment).where(PriceAdjustment.security_id == s.id)
                )
                if _material(a)
            ]
            dates = sorted(by_date)
            return dates, [by_date[d] for d in dates], adj
    return None


def company_observations(session: Session, company_id: uuid.UUID, prefer: str, symbol: str, horizons=HORIZONS) -> tuple[list[dict], list[str]]:
    """One observation per month-end as-of date: component points and forward returns."""
    notes: list[str] = []
    ps = _price_series(session, company_id)
    if ps is None:
        return [], [f"{symbol}: fewer than {SMA_DAYS + 60} stored trading days"]
    dates, closes, adj = ps
    store = rd._Store(session, company_id, set(EPS_NAMES + PROFIT_NAMES + REVENUE_NAMES))
    other = "STANDALONE" if prefer == "CONSOLIDATED" else "CONSOLIDATED"
    series = {}
    for key, names in (("eps", EPS_NAMES), ("profit", PROFIT_NAMES), ("revenue", REVENUE_NAMES)):
        s = _annual(store, names, prefer)
        series[key] = s if len(s) >= 2 else (_annual(store, names, other) or s)
    if len(series["eps"]) < 2:
        return [], [f"{symbol}: fewer than two annual EPS values"]
    # last trading day of every month
    month_ends = [d for i, d in enumerate(dates) if i + 1 == len(dates) or dates[i + 1].month != d.month]
    pe_hist: list[tuple[date, Decimal]] = []
    out = []
    for d in month_ends:
        i = dates.index(d)
        eps_pub = _published(series["eps"], d)
        eps = eps_pub[-1].value if eps_pub else None
        if eps is not None and eps > 0:
            pe_hist.append((d, closes[i] / eps))
        elif pe_hist:
            pe_hist.append((d, Decimal(0)))  # keeps the monthly spacing; a non-positive P/E is excluded below
        window_start = dates[max(0, i - SMA_DAYS + 1)]
        usable_pe = [(x, p) for x, p in pe_hist if p > 0]
        obs = {"symbol": symbol, "date": d}
        for h in horizons:
            if i + h >= len(dates):
                continue
            end = dates[i + h]
            if any(window_start <= a <= end for a in adj):
                continue
            obs[f"fwd{h}"] = closes[i + h] / closes[i] - 1
            obs[f"dd{h}"] = min(closes[i : i + h + 1]) / closes[i] - 1  # worst point from entry (<= 0)
        if not any(k.startswith("fwd") for k in obs):
            continue
        if any(window_start <= a <= d for a in adj):
            continue
        obs["trend"] = trend_points(closes[: i + 1])
        obs["trajectory"] = trajectory_points([series["profit"], series["eps"], series["revenue"]], d)
        out.append((obs, usable_pe))
    final = []
    for obs, usable in out:
        final.append({**obs, "_pe": usable})
    return final, notes


def _score(obs: dict, upside: Decimal) -> dict:
    pe = obs["_pe"]
    val, up = valuation_proxy(pe, upside) if pe and pe[-1][0] == obs["date"] else (None, None)
    return {"trajectory": obs["trajectory"], "valuation": val, "trend": obs["trend"], "implied_upside": up}


# ---------------------------------------------------------------- statistics


def add_excess(rows: list[dict], horizon: int) -> None:
    key = f"fwd{horizon}"
    by_date: dict[date, list[dict]] = {}
    for r in rows:
        if r.get(key) is not None:
            by_date.setdefault(r["date"], []).append(r)
    for rs in by_date.values():
        m = mean(float(r[key]) for r in rs) if len(rs) >= 3 else None
        for r in rs:
            r[f"ex{horizon}"] = float(r[key]) - m if m is not None else None


def bucket_stats(items: list[dict], horizon: int) -> dict:
    ex = [r[f"ex{horizon}"] for r in items if r.get(f"ex{horizon}") is not None]
    months = len({(r["date"].year, r["date"].month) for r in items if r.get(f"ex{horizon}") is not None})
    n = len(ex)
    out = {"n": n, "months": months, "companies": len({r["symbol"] for r in items if r.get(f"ex{horizon}") is not None})}
    if n:
        out["mean_excess_pct"] = round(mean(ex) * 100, 2)
        out["median_excess_pct"] = round(median(ex) * 100, 2)
        out["hit_rate_pct"] = round(sum(1 for x in ex if x > 0) / n * 100, 1)
        # risk, cost and independence (all from the same observations; nothing is assumed)
        dd = [r[f"dd{horizon}"] for r in items if r.get(f"ex{horizon}") is not None and r.get(f"dd{horizon}") is not None]  # fmt: skip
        if dd:
            out["mean_worst_drawdown_pct"] = round(float(mean(dd)) * 100, 1)
            out["worst_drawdown_pct"] = round(float(min(dd)) * 100, 1)
        sd = pstdev(ex) if n > 1 else 0
        out["excess_per_risk"] = round(mean(ex) / sd, 2) if sd else None  # mean excess / its standard deviation, per observation
        # the round-trip cost (percent of position) that would remove the average excess return; a measured cost
        # below this leaves the mean positive. No cost is assumed: the data holds none.
        out["breakeven_round_trip_cost_pct"] = round(mean(ex) * 100, 2) if mean(ex) > 0 else 0.0
        out["independent_observations"] = max(1, round(months / max(1, round(horizon / 21))))  # non-overlapping windows
    out["sufficient"] = n >= MIN_BUCKET and months >= MIN_MONTHS
    return out


def evaluate(rows: list[dict], upside: Decimal, buy: int, horizon: int) -> dict:
    groups: dict[str, list[dict]] = {"BUY": [], "WATCH": [], "SELL": []}
    for r in rows:
        if r.get(f"ex{horizon}") is None:
            continue
        v = classify(_score(r, upside), WEIGHTS, buy, buy)
        if v:
            groups[v].append(r)
    stats = {k: bucket_stats(v, horizon) for k, v in groups.items()}
    return {"buckets": stats, "reading": _reading(stats)}


def _reading(stats: dict) -> str:
    b, s = stats["BUY"], stats["SELL"]
    if not (b["sufficient"] and s["sufficient"]):
        return "INSUFFICIENT"
    spread = b["mean_excess_pct"] - s["mean_excess_pct"]
    if b["mean_excess_pct"] > 0 > s["mean_excess_pct"] and spread > 0:
        return "CONSISTENT"
    if b["mean_excess_pct"] <= s["mean_excess_pct"]:
        return "NOT SUPPORTED"
    return "MIXED"


# ---------------------------------------------------------------- static validation of the production thresholds


def validate_thresholds() -> dict:
    """Checks that follow from the rule itself, with no market data."""
    from app.research import verdict as v

    maxpts = 1 + 1 + 1 + 2 + 1
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("symmetric thresholds", v.NET_BUY == -v.NET_SELL, f"BUY at +{v.NET_BUY}, SELL at {v.NET_SELL}")
    add("a verdict is reachable", v.NET_BUY <= maxpts - 1, f"the maximum is {maxpts} points; BUY needs {v.NET_BUY} with at least {v.MIN_COMPONENTS} of 5 components")
    add("valuation alone cannot decide", 2 < v.NET_BUY, f"valuation counts at most 2 points; BUY needs {v.NET_BUY}, so at least one other component must agree")
    add("score thresholds bracket neutral", v.SCORE_SELL < v.SCORE_BUY, f"score +1 at {v.SCORE_BUY}, -1 at {v.SCORE_SELL}")
    min_target = v.MIN_REWARD_RISK * v.STOP_MAX * 100
    add(
        "valuation threshold reachable at the widest stop",
        v.UPSIDE >= min_target,
        f"a stop at the {int(v.STOP_MAX * 100)}% cap needs a target {min_target}% away for reward-to-risk {v.MIN_REWARD_RISK}; a valuation gap of exactly {v.UPSIDE}% passes only when the stop is {(v.UPSIDE / v.MIN_REWARD_RISK).quantize(Decimal('0.1'))}% or less",
    )
    add("trend band below the stop floor", v.TREND_BAND <= v.STOP_MIN * 100, f"trend band {v.TREND_BAND}% against stop floor {int(v.STOP_MIN * 100)}%")
    return {"checks": checks, "all_ok": all(c["ok"] for c in checks), "note": "internal consistency of the rule, independent of any market data; a check that is not OK is a design finding, not a failure of the program"}


# ---------------------------------------------------------------- run


LIMITATIONS = [
    "About three years of daily prices are stored, so only about fifteen month-end dates can be tested at a 12-month horizon and about twenty-one at 6 months.",
    "The universe is today's 24 pilot companies, chosen with hindsight (survivorship and selection bias); companies that fell out of favour or delisted are absent.",
    "Windows overlap (monthly as-of dates, 6-12 month horizons), so observations are strongly dependent and a hit rate or mean looks more certain than it is; the number of independent observations is far below the count shown.",
    "One market regime is covered; the result says nothing about other periods.",
    "Excess return is measured against the mean of the same pilot companies on the same date, not against an index (no index history is stored).",
    "Financial score, peer standing and the valuation scenarios cannot be rebuilt as of a past date (peer sets, scores and entered assumptions are stored as of today only), so the production verdict itself is not back-tested; a three-component subset with a valuation proxy is.",
    f"Annual results are assumed public {PUBLICATION_LAG_DAYS} days after the year end; the real filing dates are not used.",
    "Windows containing a split, bonus or flagged price gap are excluded rather than adjusted; no transaction costs, taxes or slippage are included (each bucket instead reports the round-trip cost that would remove its average excess return).",
    "The target price, stop-loss and reward-to-risk rules are not back-tested: they depend on valuation scenarios that cannot be reconstructed.",
]


def run(session: Session, companies: list[tuple[uuid.UUID, str]], prefer: str = "CONSOLIDATED") -> dict:
    rows: list[dict] = []
    notes: list[str] = []
    for cid, sym in companies:
        obs, n = company_observations(session, cid, prefer, sym)
        rows += obs
        notes += n
    for h in HORIZONS:
        add_excess(rows, h)
    results = {}
    for h in HORIZONS:
        results[str(h)] = {
            "horizon_trading_days": h,
            "default_rule": evaluate(rows, Decimal(15), 3, h),
            "threshold_grid": [
                {"upside_pct": int(u), "net_threshold": b, **evaluate(rows, u, b, h)}
                for u in UPSIDE_GRID
                for b in NET_GRID
            ],
        }
    # single-component diagnostics: does each component alone line up with the forward excess return?
    diag = {}
    for h in HORIZONS:
        diag[str(h)] = {}
        for comp in ("trend", "trajectory", "valuation"):
            groups = {1: [], 0: [], -1: []}
            for r in rows:
                if r.get(f"ex{h}") is None:
                    continue
                p = _score(r, Decimal(15))[comp]
                if p is not None:
                    groups[p].append(r)
            diag[str(h)][comp] = {("+1" if k == 1 else str(k)): bucket_stats(v, h) for k, v in groups.items()}
    dates = sorted({r["date"] for r in rows})
    return {
        "version": VERSION,
        "generated_on": date.today().isoformat(),
        "universe": [s for _, s in companies],
        "observations": len(rows),
        "first_as_of": dates[0].isoformat() if dates else None,
        "last_as_of": dates[-1].isoformat() if dates else None,
        "weights": WEIGHTS,
        "results": results,
        "component_diagnostics": diag,
        "threshold_validation": validate_thresholds(),
        "limitations": LIMITATIONS,
        "notes": notes,
        "min_bucket": MIN_BUCKET,
    }


def save(result: dict) -> Path:
    RESULT_FILE.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return RESULT_FILE


def load() -> dict | None:
    try:
        return json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summary() -> dict:
    """What the report says about the back-test: the 12-month default-rule reading and the sample behind it."""
    r = load()
    if not r:
        return {"available": False, "reason": "the back-test has not been run (python -m app.research.backtest_cli run)", "limitations": LIMITATIONS}
    h = r["results"].get("252") or next(iter(r["results"].values()))
    d = h["default_rule"]
    return {
        "available": True,
        "generated_on": r["generated_on"],
        "observations": r["observations"],
        "first_as_of": r["first_as_of"],
        "last_as_of": r["last_as_of"],
        "horizon_trading_days": h["horizon_trading_days"],
        "reading": d["reading"],
        "buckets": d["buckets"],
        "limitations": r["limitations"],
    }
