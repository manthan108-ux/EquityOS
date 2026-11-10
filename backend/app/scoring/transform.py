"""Turning a financial value into a 0-100 score. Pure functions, no database.

Four transformations, chosen by the metric's economic meaning (recorded on its definition):

* PERCENTILE  read the value against a benchmark distribution built from real observations
* THRESHOLD   piecewise-linear curve through documented (value, score) points - for safety and risk
              measures and growth rates with a stated rationale
* RANGE       trapezoid for metrics with a genuine sweet spot (a current ratio can be too low OR too
              high)
* LOWER/HIGHER direction is part of PERCENTILE and THRESHOLD: a curve simply descends or ascends.

Rules shared by all: scores are always within 0..100 (an extreme outlier is clamped, it cannot score
more than 100 or less than 0); a missing value is not scored at all (never a zero); the original
value is returned next to the score by the caller.
"""

from dataclasses import dataclass
from decimal import Decimal

PERCENTILE = "PERCENTILE"
THRESHOLD = "THRESHOLD"
RANGE = "RANGE"
KINDS = (PERCENTILE, THRESHOLD, RANGE)


class NotScorable(Exception):
    """The value cannot be turned into a score (and that is not the same as a bad score)."""


@dataclass(frozen=True)
class Scored:
    score: Decimal
    detail: dict


def _q(x: float) -> Decimal:
    return Decimal(str(round(min(100.0, max(0.0, x)), 3)))


def interpolate(points: list[tuple[float, float]], x: float) -> float:
    """Piecewise-linear value at x; flat beyond the first and last point."""
    pts = sorted(points)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= x <= x1:
            return y1 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    raise NotScorable("value outside every segment")  # unreachable for sorted, finite points


def score_threshold(value: Decimal, curve: dict) -> Scored:
    points = [(float(x), float(y)) for x, y in curve["points"]]
    if len(points) < 2:
        raise NotScorable("a threshold curve needs at least two points")
    return Scored(_q(interpolate(points, float(value))), {"curve": curve["points"]})


def score_range(value: Decimal, curve: dict) -> Scored:
    zl, fl, fh, zh = (float(curve[k]) for k in ("zero_low", "full_low", "full_high", "zero_high"))
    if not zl < fl <= fh < zh:
        raise NotScorable("a range curve needs zero_low < full_low <= full_high < zero_high")
    points = [(zl, 0.0), (fl, 100.0), (fh, 100.0), (zh, 0.0)]
    return Scored(_q(interpolate(points, float(value))), {"range": curve})


def percentile_anchors(stats: dict) -> list[tuple[float, float]]:
    """(value, percentile) anchors from the stored distribution; duplicates keep the higher rank."""
    raw = [
        (stats["min"], 0.0),
        (stats["p25"], 25.0),
        (stats["p50"], 50.0),
        (stats["p75"], 75.0),
        (stats["p90"], 90.0),
        (stats["max"], 100.0),
    ]
    merged: dict[float, float] = {}
    for x, p in raw:
        merged[float(x)] = max(p, merged.get(float(x), 0.0))
    return sorted(merged.items())


def score_percentile(value: Decimal, stats: dict, direction: str) -> Scored:
    anchors = percentile_anchors(stats)
    if len(anchors) < 3:
        raise NotScorable("the benchmark distribution has no spread")
    rank = interpolate(anchors, float(value))
    score = 100.0 - rank if direction == "LOWER" else rank
    return Scored(_q(score), {"percentile": round(rank, 1), "direction": direction})


def not_meaningful(code: str, value: Decimal) -> str | None:
    """Reasons a ratio cannot be read at all (as opposed to being poor)."""
    if (
        code in ("VALUATION.pe_ttm", "VALUATION.pe_annual", "VALUATION.price_to_book")
        and value <= 0
    ):
        return "negative or zero earnings/book value: the multiple is not meaningful"
    if code == "LEVERAGE.debt_to_equity" and value < 0:
        return "negative equity: the ratio is not meaningful"
    return None
