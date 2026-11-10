"""Robust benchmark statistics. Pure functions, no database.

A benchmark is only worth building from a real, representative sample, so this module refuses small
ones (`classify` returns INSUFFICIENT) instead of producing a distribution that looks authoritative.
Outliers are removed with the median absolute deviation (robust to the very outliers it looks for);
every removed value is kept by the caller with the reason, never silently dropped.
"""

import math
from decimal import Decimal

MIN_COMPANIES = 4  # distinct companies
MIN_OBSERVATIONS = 12  # company-years
HIGH_COMPANIES = 8
HIGH_OBSERVATIONS = 30
MAD_CUTOFF = 5.0  # modified z-score beyond which a value is an outlier
MAD_MIN_SAMPLE = 8  # below this, MAD is too unstable to exclude anything


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile (q in 0..100) of an ascending list."""
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def split_outliers(values: list[Decimal]) -> tuple[list[int], list[int]]:
    """Indexes of (kept, excluded) values by the modified z-score (0.6745 * |x - median| / MAD)."""
    xs = [float(v) for v in values]
    if len(xs) < MAD_MIN_SAMPLE:
        return list(range(len(xs))), []
    ordered = sorted(xs)
    med = percentile(ordered, 50)
    mad = percentile(sorted(abs(x - med) for x in xs), 50)
    if mad == 0:
        return list(range(len(xs))), []
    kept, dropped = [], []
    for i, x in enumerate(xs):
        (dropped if 0.6745 * abs(x - med) / mad > MAD_CUTOFF else kept).append(i)
    return kept, dropped


def summarize(values: list[Decimal]) -> dict:
    xs = sorted(float(v) for v in values)
    n = len(xs)
    mean = sum(xs) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {
        "n": n,
        "min": xs[0],
        "max": xs[-1],
        "mean": round(mean, 6),
        "median": percentile(xs, 50),
        "std": round(std, 6),
        "p25": percentile(xs, 25),
        "p50": percentile(xs, 50),
        "p75": percentile(xs, 75),
        "p90": percentile(xs, 90),
    }


def classify(n_observations: int, n_companies: int) -> str:
    if n_companies >= HIGH_COMPANIES and n_observations >= HIGH_OBSERVATIONS:
        return "HIGH"
    if n_companies >= MIN_COMPANIES and n_observations >= MIN_OBSERVATIONS:
        return "MEDIUM"
    return "INSUFFICIENT"
