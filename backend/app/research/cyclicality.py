"""Business cyclicality: what the stored history shows, set beside the sector's known sensitivities.

Three layers, always kept apart:

* SECTOR CHARACTERISATION: how businesses of this sector generally behave (cyclical, defensive ...) and
  which macro factors matter. A template, labelled as such, not evidence about this company.
* OBSERVED HISTORY: arithmetic on the company's own stored annual and quarterly figures (growth volatility,
  declines and the year after them, earnings against revenue). Only what the evidence holds.
* LIMITS: a handful of annual points cannot show a full cycle. The report says so; it does not extrapolate.

Thresholds (disclosed in the report): growth volatility is the population standard deviation of year-on-year
growth in percentage points; under 8 = low, 8-20 = moderate, over 20 = high. These are presentation rules.
"""

from __future__ import annotations

from decimal import Decimal
from statistics import pstdev

from app.research.analysis import dec
from app.research.industry import template

VERSION = "1.0"
LOW, HIGH = Decimal("8"), Decimal("20")
FULL_CYCLE_YEARS = 10


def _row(rows: list[dict], *keys: str) -> dict | None:
    for k in keys:
        r = next((x for x in rows if x["key"] == k), None)
        if r:
            return r
    return None


def _growths(row: dict) -> list[dict]:
    """Year-on-year growth for consecutive fiscal years (percent), skipping undefined steps."""
    out = []
    pts = row["points"]
    for a, b in zip(pts, pts[1:], strict=False):
        x, y = dec(a["value"]), dec(b["value"])
        if x and y is not None and x > 0:
            out.append(
                {
                    "fy": b["fy"],
                    "growth": (y / x - 1) * 100,
                    "from": a["evidence_id"],
                    "to": b["evidence_id"],
                }
            )
    return out


def _bucket(sd: Decimal | None) -> str:
    if sd is None:
        return "not measurable"
    return "low" if sd < LOW else "moderate" if sd <= HIGH else "high"


def _series_block(row: dict | None, label: str) -> dict | None:
    if not row:
        return None
    g = _growths(row)
    if not g:
        return None
    vals = [x["growth"] for x in g]
    sd = Decimal(str(pstdev([float(v) for v in vals]))) if len(vals) >= 2 else None
    downs = [x for x in g if x["growth"] < 0]
    recoveries = []
    for d in downs:
        nxt = next((x for x in g if x["fy"] > d["fy"]), None)
        recoveries.append(
            {
                "fall_fy": d["fy"],
                "fall_pct": str(d["growth"].quantize(Decimal("0.1"))),
                "next_fy": nxt["fy"] if nxt else None,
                "next_pct": str(nxt["growth"].quantize(Decimal("0.1"))) if nxt else None,
                "evidence_ids": [d["from"], d["to"]],
            }
        )
    peak_to_trough = None
    pts = [dec(p["value"]) for p in row["points"] if dec(p["value"]) is not None]
    if len(pts) >= 2:
        worst, peak = Decimal(0), pts[0]
        for v in pts:
            peak = max(peak, v)
            if peak > 0:
                worst = min(worst, (v / peak - 1) * 100)
        peak_to_trough = str(worst.quantize(Decimal("0.1")))
    return {
        "label": label,
        "years": [x["fy"] for x in g],
        "growth": [str(x["growth"].quantize(Decimal("0.1"))) for x in g],
        "n_changes": len(g),
        "volatility_pp": str(sd.quantize(Decimal("0.1"))) if sd is not None else None,
        "volatility": _bucket(sd),
        "range_pp": str((max(vals) - min(vals)).quantize(Decimal("0.1"))),
        "down_years": len(downs),
        "recoveries": recoveries,
        "max_peak_to_trough_pct": peak_to_trough,
        "evidence_ids": sorted({i for x in g for i in (x["from"], x["to"])}),
    }


def build(trend: dict, sector_code: str, growth: dict, monitoring: list[dict]) -> dict:
    t = template(sector_code)
    prior, prior_text = t["cyclical_prior"]
    rows = trend["annual"]
    rev = _series_block(
        _row(rows, "revenue", "total_income"),
        "Revenue" if sector_code != "BANK" else "Total income",
    )
    prof = _series_block(_row(rows, "net_profit"), "Net profit")
    n_years = max((len(r["points"]) for r in rows), default=0)

    observed = None
    if prof and prof["volatility"] != "not measurable":
        observed = prof["volatility"]
    elif prof:
        observed = "not measurable"
    consistent = None
    if observed in ("low", "moderate", "high"):
        expected = {
            "defensive": "low",
            "moderately cyclical": "moderate",
            "cyclical": "moderate",
            "highly cyclical": "high",
        }.get(prior)
        if expected:
            order = ["low", "moderate", "high"]
            consistent = (
                "consistent with"
                if abs(order.index(observed) - order.index(expected)) == 0
                else "milder than"
                if order.index(observed) < order.index(expected)
                else "more volatile than"
            )

    leverage = None
    if (
        rev
        and prof
        and rev["volatility_pp"]
        and prof["volatility_pp"]
        and Decimal(rev["volatility_pp"]) > 0
    ):
        leverage = str(
            (Decimal(prof["volatility_pp"]) / Decimal(rev["volatility_pp"])).quantize(
                Decimal("0.1")
            )
        )

    cagr_rev = (growth.get("revenue") or {}).get("company")
    structural = None
    if cagr_rev:
        structural = {
            "cagr": cagr_rev["cagr"],
            "years": cagr_rev["years"],
            "down_years_revenue": rev["down_years"] if rev else None,
            "text": (
                f"Revenue compounded at {cagr_rev['cagr']}% a year over {cagr_rev['years']} years "
                + (
                    f"with {rev['down_years']} year(s) of decline out of {rev['n_changes']}. "
                    if rev
                    else ". "
                )
                + "A steady positive trend with few declines is consistent with structural growth; a "
                "trend that tracks the economy or commodity prices is consistent with cyclical growth."
            ),
        }

    limits = [
        f"Only {n_years} annual data point(s) are stored; a full cycle (several expansions and "
        f"downturns) needs about {FULL_CYCLE_YEARS} years, so cyclicality is indicated, not established.",
        "Volatility is measured on year-on-year changes, with at most a handful of observations; one unusual "
        "year (an acquisition, a one-off tax item, a restatement) can dominate it.",
        "Quarterly seasonality is not separated from the cycle.",
    ]
    if not (rev or prof):
        limits.insert(
            0, "No annual revenue or profit history is stored, so no observed measure is given."
        )
    return {
        "version": VERSION,
        "sector": {
            "classification": prior,
            "text": prior_text,
            "note": "Sector characterisation: a general template, not evidence about this company.",
        },
        "observed": {
            "revenue": rev,
            "profit": prof,
            "volatility_class": observed,
            "versus_sector": consistent,
            "profit_to_revenue_volatility": leverage,
            "n_years": n_years,
            "thresholds": f"growth volatility (population standard deviation of year-on-year growth): low under {LOW} points, moderate {LOW} to {HIGH}, high over {HIGH}",
        },
        "structural": structural,
        "sensitivities": [
            {"factor": f, "relevance": r, "mechanism": m} for f, r, m in t["sensitivity"]
        ],
        "sensitivity_note": "Relevance and mechanisms are sector templates; they say what usually matters for this kind of business, not how much it matters for this company.",
        "indicators": t["leading_indicators"],
        "monitored": [m for m in monitoring if m.get("available")][:6],
        "limits": limits,
    }
