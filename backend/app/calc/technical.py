"""Technical indicators on ADJUSTED prices. Pure functions, exact `Decimal` arithmetic.

Definitions (stated so a reader can reproduce any number):

* SMA(n): mean of the last n closes.
* EMA(n): seeded with the SMA of the first n closes, then e = e + 2/(n+1) * (close - e).
* RSI(n): Wilder. First average gain/loss = mean over the first n changes, then
  avg = (avg * (n-1) + current) / n. RSI = 100 - 100 / (1 + avg_gain / avg_loss); 100 if no losses.
* MACD: EMA(12) - EMA(26) of closes; signal = EMA(9) of the MACD line; histogram = MACD - signal.
* Bollinger(n, k): SMA(n) +- k * population standard deviation of the last n closes.
* ATR(n): Wilder smoothing of the true range max(high-low, |high-prev close|, |low-prev close|),
  seeded with the mean of the first n true ranges.
* Returns: reference bar = last bar on or before (as_of - period); a first bar up to 8 days AFTER
  that date is accepted for the longest windows and the actual date is recorded.
* Volatility(n): sample standard deviation of the last n daily simple returns x sqrt(252), in %.
* Max drawdown: largest peak-to-trough fall of the close within the window, in %.
* 52-week high/low: highest high / lowest low over the last 365 days (needs ~a year of data).

When there is not enough history the result is `InsufficientHistory` with the reason; nothing is
extrapolated or padded.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.calc.adjust import Bar

PLACES = Decimal("0.000001")
YEAR_DAYS = 365
TOLERANCE_DAYS = 8
D = Decimal


class InsufficientHistory(ValueError):
    """The stored price history cannot support this indicator."""


@dataclass(frozen=True)
class Reading:
    value: Decimal
    unit: str
    note: dict


def _q(x: Decimal) -> Decimal:
    return x.quantize(PLACES)


def _need(values: list, n: int, what: str) -> None:
    if len(values) < n:
        raise InsufficientHistory(f"{what} needs {n} prices; {len(values)} stored")


def sma(values: list[Decimal], n: int) -> Decimal:
    _need(values, n, f"SMA({n})")
    return sum(values[-n:], D(0)) / n


def ema_series(values: list[Decimal], n: int) -> list[Decimal]:
    """EMA from index n-1 on (the first value is the SMA of the first n closes)."""
    _need(values, n, f"EMA({n})")
    alpha = D(2) / (n + 1)
    e = sum(values[:n], D(0)) / n
    out = [e]
    for v in values[n:]:
        e = e + alpha * (v - e)
        out.append(e)
    return out


def ema(values: list[Decimal], n: int) -> Decimal:
    return ema_series(values, n)[-1]


def rsi(values: list[Decimal], n: int = 14) -> Decimal:
    _need(values, n + 1, f"RSI({n})")
    changes = [b - a for a, b in zip(values, values[1:], strict=False)]
    gains = [max(c, D(0)) for c in changes]
    losses = [max(-c, D(0)) for c in changes]
    avg_gain = sum(gains[:n], D(0)) / n
    avg_loss = sum(losses[:n], D(0)) / n
    for g, l_ in zip(gains[n:], losses[n:], strict=False):
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l_) / n
    if avg_loss == 0:
        return D(100)
    return D(100) - D(100) / (1 + avg_gain / avg_loss)


def macd(
    values: list[Decimal], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Decimal, Decimal, Decimal]:
    _need(values, slow + signal - 1, f"MACD({fast},{slow},{signal})")
    fast_s = ema_series(values, fast)
    slow_s = ema_series(values, slow)
    # align on the last len(slow_s) points
    line = [f - s for f, s in zip(fast_s[-len(slow_s) :], slow_s, strict=True)]
    sig = ema_series(line, signal)
    return line[-1], sig[-1], line[-1] - sig[-1]


def bollinger(values: list[Decimal], n: int = 20, k: int = 2) -> tuple[Decimal, Decimal, Decimal]:
    _need(values, n, f"Bollinger({n})")
    window = values[-n:]
    mean = sum(window, D(0)) / n
    variance = sum(((v - mean) ** 2 for v in window), D(0)) / n
    sd = variance.sqrt()
    return mean, mean + k * sd, mean - k * sd


def atr(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], n: int = 14) -> Decimal:
    _need(closes, n + 1, f"ATR({n})")
    trs = [
        max(h - l_, abs(h - pc), abs(l_ - pc))
        for h, l_, pc in zip(highs[1:], lows[1:], closes[:-1], strict=True)
    ]
    value = sum(trs[:n], D(0)) / n
    for tr in trs[n:]:
        value = (value * (n - 1) + tr) / n
    return value


def daily_returns(values: list[Decimal]) -> list[Decimal]:
    return [b / a - 1 for a, b in zip(values, values[1:], strict=False) if a != 0]


def volatility(values: list[Decimal], n: int) -> Decimal:
    returns = daily_returns(values)
    _need(returns, n, f"volatility({n})")
    window = returns[-n:]
    mean = sum(window, D(0)) / n
    variance = sum(((r - mean) ** 2 for r in window), D(0)) / (n - 1)
    return variance.sqrt() * D(252).sqrt() * 100


def max_drawdown(values: list[Decimal]) -> Decimal:
    peak, worst = values[0], D(0)
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst * 100


# ---- calendar helpers ----------------------------------------------------------------------------


def months_before(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    m += 1
    day = min(
        d.day,
        [
            31,
            29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ][m - 1],
    )
    return date(y, m, day)


def reference_bar(bars: list[Bar], target: date) -> Bar:
    """Last bar on or before `target`; else the first bar if it is within the tolerance after."""
    before = [b for b in bars if b.trade_date <= target]
    if before:
        return before[-1]
    if bars and bars[0].trade_date <= target + timedelta(days=TOLERANCE_DAYS):
        return bars[0]
    raise InsufficientHistory(
        f"history starts {bars[0].trade_date if bars else 'never'}, after the needed {target}"
    )


def window(bars: list[Bar], as_of: date, days: int) -> list[Bar]:
    """Bars in (as_of - days, as_of]; the history must reach back to the start of the window."""
    start = as_of - timedelta(days=days)
    if not bars or bars[0].trade_date > start + timedelta(days=TOLERANCE_DAYS):
        first = bars[0].trade_date if bars else "none"
        raise InsufficientHistory(f"needs prices from {start}; history starts {first}")
    return [b for b in bars if start < b.trade_date <= as_of]


# ---- catalogue -----------------------------------------------------------------------------------


def _closes(bars: list[Bar]) -> list[Decimal]:
    return [b.close for b in bars]


def _ret(months: int) -> Callable[[list[Bar], date], Reading]:
    def go(bars: list[Bar], as_of: date) -> Reading:
        last = bars[-1]
        ref = reference_bar(bars, months_before(last.trade_date, months))
        value = (last.close / ref.close - 1) * 100
        return Reading(value, "percent", {"from": str(ref.trade_date), "to": str(last.trade_date)})

    return go


def _single(fn: Callable[[list[Decimal]], Decimal], unit: str):
    def go(bars: list[Bar], as_of: date) -> Reading:
        return Reading(fn(_closes(bars)), unit, {})

    return go


def _macd_part(i: int):
    def go(bars: list[Bar], as_of: date) -> Reading:
        return Reading(macd(_closes(bars))[i], "inr_per_share", {})

    return go


def _bb(i: int):
    def go(bars: list[Bar], as_of: date) -> Reading:
        return Reading(bollinger(_closes(bars))[i], "inr_per_share", {})

    return go


def _atr(bars: list[Bar], as_of: date) -> Reading:
    if any(b.high is None or b.low is None for b in bars[-15:]):
        raise InsufficientHistory("ATR(14) needs high and low prices for the last 15 sessions")
    return Reading(
        atr([b.high for b in bars], [b.low for b in bars], _closes(bars), 14), "inr_per_share", {}
    )


def _high52(bars: list[Bar], as_of: date) -> Reading:
    w = window(bars, as_of, YEAR_DAYS)
    return Reading(
        max((b.high if b.high is not None else b.close) for b in w),
        "inr_per_share",
        {"sessions": len(w)},
    )


def _low52(bars: list[Bar], as_of: date) -> Reading:
    w = window(bars, as_of, YEAR_DAYS)
    return Reading(
        min((b.low if b.low is not None else b.close) for b in w),
        "inr_per_share",
        {"sessions": len(w)},
    )


def _from_high(bars: list[Bar], as_of: date) -> Reading:
    high = _high52(bars, as_of).value
    return Reading((bars[-1].close / high - 1) * 100, "percent", {})


def _mdd(bars: list[Bar], as_of: date) -> Reading:
    w = window(bars, as_of, YEAR_DAYS)
    return Reading(max_drawdown(_closes(w)), "percent", {"sessions": len(w)})


def _avg_volume(bars: list[Bar], as_of: date) -> Reading:
    last = bars[-20:]
    _need(last, 20, "average volume(20)")
    if any(b.volume is None for b in last):
        raise InsufficientHistory("volume missing for some of the last 20 sessions")
    return Reading(sum((b.volume for b in last), D(0)) / 20, "shares", {})


def _avg_delivery(bars: list[Bar], as_of: date) -> Reading:
    last = bars[-20:]
    _need(last, 20, "average delivery %(20)")
    if any(b.delivery_pct is None for b in last):
        raise InsufficientHistory("delivery % is not published for some of the last 20 sessions")
    return Reading(sum((b.delivery_pct for b in last), D(0)) / 20, "percent", {})


def _vol(n: int):
    def go(bars: list[Bar], as_of: date) -> Reading:
        return Reading(volatility(_closes(bars), n), "percent", {})

    return go


INDICATORS: dict[str, Callable[[list[Bar], date], Reading]] = {
    "close": lambda bars, as_of: Reading(
        bars[-1].close, "inr_per_share", {"date": str(bars[-1].trade_date)}
    ),
    "sma_20": _single(lambda v: sma(v, 20), "inr_per_share"),
    "sma_50": _single(lambda v: sma(v, 50), "inr_per_share"),
    "sma_200": _single(lambda v: sma(v, 200), "inr_per_share"),
    "ema_12": _single(lambda v: ema(v, 12), "inr_per_share"),
    "ema_26": _single(lambda v: ema(v, 26), "inr_per_share"),
    "rsi_14": _single(lambda v: rsi(v, 14), "index"),
    "macd": _macd_part(0),
    "macd_signal": _macd_part(1),
    "macd_histogram": _macd_part(2),
    "bollinger_mid_20": _bb(0),
    "bollinger_upper_20": _bb(1),
    "bollinger_lower_20": _bb(2),
    "atr_14": _atr,
    "return_1m": _ret(1),
    "return_3m": _ret(3),
    "return_6m": _ret(6),
    "return_1y": _ret(12),
    "volatility_60d": _vol(60),
    "volatility_126d": _vol(126),
    "high_52w": _high52,
    "low_52w": _low52,
    "pct_from_52w_high": _from_high,
    "max_drawdown_1y": _mdd,
    "avg_volume_20d": _avg_volume,
    "avg_delivery_pct_20d": _avg_delivery,
}


def compute(indicator: str, bars: list[Bar], as_of: date) -> Reading:
    """The indicator as of `as_of` from adjusted bars up to that date, or InsufficientHistory."""
    if indicator not in INDICATORS:
        raise KeyError(f"unknown indicator {indicator!r}")
    usable = [b for b in bars if b.trade_date <= as_of]
    if not usable:
        raise InsufficientHistory("no prices on or before the date")
    reading = INDICATORS[indicator](usable, as_of)
    return Reading(_q(reading.value), reading.unit, reading.note)


__all__ = [
    "INDICATORS",
    "InsufficientHistory",
    "Reading",
    "atr",
    "bollinger",
    "compute",
    "ema",
    "ema_series",
    "macd",
    "max_drawdown",
    "months_before",
    "reference_bar",
    "rsi",
    "sma",
    "volatility",
    "window",
]
