"""Corporate-action adjustment of prices and volumes (splits, bonuses, consolidations).

Why: stored prices are the exchanges' official end-of-day values and are UNADJUSTED. A 1:1 bonus
halves the price overnight; a return or moving average computed across it would be wrong. Old prices
must be divided by the event's factor (and old volumes multiplied) before any comparison over time.

Two sources of events, never mixed up:

* CORPORATE_ACTION: a stored `corporate_actions` row whose ratio text can be read unambiguously.
* IMPLIED: the exchange's bhavcopy has a `prev_close` column, its own reference price for the day.
  On
  an ex-date that reference is adjusted, so `prior stored close / prev_close` equals the event's
  factor. A ratio is accepted as an event only if it matches a standard split/bonus ratio within
  0.5%. Anything else (for example a missing trading session) is recorded as UNEXPLAINED and NOT
  applied.

A third case is REPORTED, never applied: the exchange file's `prev_close` for an ex-date is
sometimes the unadjusted prior close (it then shows no gap at all), yet the close itself falls by a
standard split/bonus factor overnight (Reliance, 28 Oct 2024: 2655.70 -> 1334.35). That is flagged
as PRICE_GAP_ONLY so it is visible; it is applied only once a corporate action confirms it.

Not adjusted, and why: cash dividends (a total-return question, and exchanges do not apply them
uniformly), and rights issues (the factor depends on the issue price, which we do not hold).
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    AdjustedPrice,
    AdjustmentKind,
    AdjustmentOrigin,
    AdjustmentStatus,
    CorporateAction,
    EodPrice,
    PriceAdjustment,
    Security,
)

MATCH_TOLERANCE = Decimal("0.005")  # 0.5% around a standard ratio
NOISE = Decimal("0.003")  # below this the reference price simply equals the prior close
REPORT_THRESHOLD = Decimal("0.01")  # smaller unexplained differences are not worth reporting
MIN_EVENT_RATIO = Decimal("1.2")  # the smallest standard factor is 1.25 (a 1:4 bonus)
# Only the factors companies really use are matched when nothing but a price gap is known: with
# every possible ratio (1.4, 1.6, ...) an ordinary 30% fall would be mistaken for a bonus.
COMMON_FACTORS = tuple(Decimal(str(x)) for x in ("1.5", 2, 3, 4, 5, 10, 20, 50))
EXPLAINED_NOTE = "; agrees with the stored corporate action"
PRICE_GAP_TOLERANCE = Decimal("0.015")  # a day's own move on top of a split/bonus factor
MAX_ROW_GAP_DAYS = 5  # more calendar days than this between two rows: a session may be missing


def _standard_factors() -> list[Decimal]:
    out = set()
    for new in range(1, 6):  # bonus: `new` shares for every `held`
        for held in range(1, 6):
            factor = Decimal(new + held) / Decimal(held)
            out.add(factor.quantize(Decimal("0.000001")))
    out.update(Decimal(n) for n in (2, 3, 4, 5, 6, 8, 10, 20, 25, 50, 100))  # face-value splits
    return sorted(f for f in out if f >= Decimal("1.25"))


STANDARD_FACTORS = _standard_factors()


@dataclass(frozen=True)
class PriceRow:
    trade_date: date
    close: Decimal | None
    prev_close: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: int | None = None
    turnover_inr: Decimal | None = None
    delivery_pct: Decimal | None = None


@dataclass(frozen=True)
class Event:
    ex_date: date
    factor: Decimal  # >1: old prices are divided by it (split/bonus); <1: consolidation
    kind: AdjustmentKind
    origin: AdjustmentOrigin
    applied: bool
    confidence: str  # EXPLICIT | SINGLE_EXCHANGE | CONFIRMED | NOT_CLASSIFIED
    detail: str
    prior_close: Decimal | None = None
    reference_close: Decimal | None = None
    corporate_action_id: int | None = None
    ratio_text: str | None = None
    source: dict | None = None


@dataclass
class Bar:
    """One day, adjusted to today's share structure."""

    trade_date: date
    close: Decimal
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume: Decimal | None
    delivery_pct: Decimal | None
    raw_close: Decimal
    cumulative_factor: Decimal


# ---- reading corporate-action rows ---------------------------------------------------------------

_BONUS = re.compile(r"(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)")
_FV = re.compile(
    r"(?i)(?:rs\.?|re\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:/-)?[^0-9]{0,30}?\bto\b[^0-9]{0,20}?"
    r"(\d+(?:\.\d+)?)"
)


def parse_action_factor(action_type: str, ratio_text: str) -> tuple[Decimal, AdjustmentKind] | None:
    """The price factor of a split/bonus from its ratio text, or None if it is not unambiguous.

    Bonus "a:b" means `a` new shares for every `b` held (factor (a+b)/b). A split is accepted only
    in words ("from Rs 10 to Re 1"), because a bare "10:1" could be read either way.
    """
    kind = (action_type or "").strip().lower()
    text = ratio_text or ""
    if "bonus" in kind:
        m = _BONUS.search(text)
        if not m:
            return None
        new, held = Decimal(m.group(1)), Decimal(m.group(2))
        if new <= 0 or held <= 0:
            return None
        return (new + held) / held, AdjustmentKind.BONUS
    if "split" in kind or "sub-division" in kind or "subdivision" in kind:
        m = _FV.search(text)
        if not m:
            return None
        old, new = Decimal(m.group(1)), Decimal(m.group(2))
        if new <= 0 or old <= 0 or old == new:
            return None
        return old / new, AdjustmentKind.SPLIT
    return None


def _verification(action) -> dict:
    """The verification block stored with an action, if its details are a mapping.

    Some providers store details as a list of name/value pairs (Upstox bonus and dividend events);
    there is no block then.
    """
    details = getattr(action, "details", None)
    block = details.get("verification") if isinstance(details, dict) else None
    return block if isinstance(block, dict) else {}


def events_from_actions(
    actions: list[CorporateAction],
) -> tuple[list[Event], list[str]]:
    """Events from corporate-action rows, plus a reason for every row that could not be used."""
    events, skipped = [], []
    for a in actions:
        label = (a.action_type or "").strip().lower()
        if not any(w in label for w in ("bonus", "split", "sub-division", "subdivision")):
            continue  # dividends, rights etc.: not adjusted
        if a.effective_date is None:
            skipped.append(f"{a.action_type} '{a.ratio_text}': no ex-date")
            continue
        parsed = parse_action_factor(a.action_type, a.ratio_text)
        if parsed is None:
            skipped.append(
                f"{a.action_type} on {a.effective_date} '{a.ratio_text}': ratio not unambiguous"
            )
            continue
        factor, kind = parsed
        events.append(
            Event(
                ex_date=a.effective_date,
                factor=factor,
                kind=kind,
                origin=AdjustmentOrigin.CORPORATE_ACTION,
                applied=True,
                confidence="EXPLICIT",
                detail=f"{a.action_type} {a.ratio_text} from {a.provider_code}",
                corporate_action_id=a.id,
                ratio_text=a.ratio_text or None,
                source={
                    "provider": a.provider_code,
                    "source_url": getattr(a, "source_url", None),
                    **_verification(a),
                },
            )
        )
    return events, skipped


# ---- implied events ------------------------------------------------------------------------------


def match_standard(ratio: Decimal) -> Decimal | None:
    """The standard factor `ratio` matches (or its inverse, for a consolidation), if any."""
    target = ratio if ratio >= 1 else 1 / ratio
    if target < MIN_EVENT_RATIO:
        return None
    best = min(STANDARD_FACTORS, key=lambda f: abs(target / f - 1))
    if abs(target / best - 1) > MATCH_TOLERANCE:
        return None
    return best if ratio >= 1 else 1 / best


def _price_gap_candidate(before: PriceRow, now: PriceRow) -> Event | None:
    """A close that fell by a standard split/bonus factor although the reference price shows no gap.

    Reported only (`applied=False`): without a corporate action the cause is unknown (a real crash
    of exactly that size is possible), so no price is adjusted on this evidence.
    """
    if (
        not now.close
        or now.close <= 0
        or (now.trade_date - before.trade_date).days > MAX_ROW_GAP_DAYS
    ):
        return None
    ratio = before.close / now.close
    if ratio < MIN_EVENT_RATIO:
        return None
    best = min(COMMON_FACTORS, key=lambda f: abs(ratio / f - 1))
    if abs(ratio / best - 1) > PRICE_GAP_TOLERANCE:
        return None
    volume = ""
    if before.volume and now.volume:
        volume = f"; volume {now.volume / before.volume:.2f}x the day before"
    return Event(
        ex_date=now.trade_date,
        factor=ratio.quantize(Decimal("0.00000001")),
        kind=AdjustmentKind.UNEXPLAINED,
        origin=AdjustmentOrigin.IMPLIED,
        applied=False,
        confidence="PRICE_GAP_ONLY",
        detail=(
            f"SUSPECTED unadjusted split/bonus: close fell from {before.close} to {now.close} "
            f"(factor {ratio:.4f}, near the standard factor {best:.4f}) while the exchange's "
            f"reference price equals the prior close{volume}. NOT adjusted: it needs a "
            "corporate action to confirm it"
        ),
        prior_close=before.close,
        reference_close=now.prev_close,
    )


def detect_implied(rows: list[PriceRow]) -> list[Event]:
    """Events implied by the exchange's reference price (see module docstring)."""
    rows = sorted((r for r in rows if r.close is not None), key=lambda r: r.trade_date)
    out: list[Event] = []
    for before, now in zip(rows, rows[1:], strict=False):
        if not now.prev_close or now.prev_close <= 0 or before.close <= 0:
            continue
        ratio = before.close / now.prev_close
        if abs(ratio - 1) <= NOISE:
            candidate = _price_gap_candidate(before, now)
            if candidate is not None:
                out.append(candidate)
            continue
        gap = (now.trade_date - before.trade_date).days
        note = (
            f" ({gap} calendar days between the two stored rows: a missing session is possible)"
            if gap > MAX_ROW_GAP_DAYS
            else ""
        )
        standard = match_standard(ratio)
        if standard is not None:
            kind = AdjustmentKind.SPLIT_OR_BONUS if standard > 1 else AdjustmentKind.CONSOLIDATION
            out.append(
                Event(
                    ex_date=now.trade_date,
                    factor=standard,
                    kind=kind,
                    origin=AdjustmentOrigin.IMPLIED,
                    applied=True,
                    confidence="SINGLE_EXCHANGE",
                    detail=(
                        f"prior close {before.close} vs reference price {now.prev_close} "
                        f"(ratio {ratio:.4f}) matches standard factor {standard:.4f}{note}"
                    ),
                    prior_close=before.close,
                    reference_close=now.prev_close,
                )
            )
        elif abs(ratio - 1) > REPORT_THRESHOLD:
            out.append(
                Event(
                    ex_date=now.trade_date,
                    factor=ratio.quantize(Decimal("0.00000001")),
                    kind=AdjustmentKind.UNEXPLAINED,
                    origin=AdjustmentOrigin.IMPLIED,
                    applied=False,
                    confidence="NOT_CLASSIFIED",
                    detail=(
                        f"prior close {before.close} vs reference price {now.prev_close} "
                        f"(ratio {ratio:.4f}) matches no split/bonus ratio; not adjusted{note}"
                    ),
                    prior_close=before.close,
                    reference_close=now.prev_close,
                )
            )
    return out


def reconcile(
    explicit: list[Event], implied: list[Event], other_exchange: list[Event] | None = None
) -> list[Event]:
    """Combine event lists for one security.

    * An implied event on the same date as an explicit one is kept for the record but not applied.
    * An implied event that the other exchange's reference price also shows becomes CONFIRMED.
    """
    explicit_dates = {e.ex_date: e for e in explicit}
    other = {(e.ex_date, e.factor) for e in (other_exchange or []) if e.applied}
    out = list(explicit)
    for e in implied:
        if e.ex_date in explicit_dates:
            ex = explicit_dates[e.ex_date]
            agree = abs(e.factor / ex.factor - 1) <= MATCH_TOLERANCE
            out.append(
                Event(
                    **{
                        **e.__dict__,
                        "applied": False,
                        "detail": e.detail
                        + (
                            EXPLAINED_NOTE
                            if agree
                            else f"; DISAGREES with the stored action (factor {ex.factor})"
                        ),
                    }
                )
            )
        elif e.applied and (e.ex_date, e.factor) in other:
            out.append(Event(**{**e.__dict__, "confidence": "CONFIRMED"}))
        else:
            out.append(e)
    return sorted(out, key=lambda e: (e.ex_date, e.origin.value))


# ---- applying events -----------------------------------------------------------------------------


def cumulative_factor(events: list[Event], on: date) -> Decimal:
    """Product of the factors of APPLIED events strictly after `on`."""
    factor = Decimal(1)
    for e in events:
        if e.applied and e.ex_date > on:
            factor *= e.factor
    return factor


def adjust(rows: list[PriceRow], events: list[Event]) -> list[Bar]:
    bars = []
    for r in sorted(rows, key=lambda r: r.trade_date):
        if r.close is None:
            continue
        f = cumulative_factor(events, r.trade_date)
        bars.append(
            Bar(
                trade_date=r.trade_date,
                close=r.close / f,
                open=r.open / f if r.open is not None else None,
                high=r.high / f if r.high is not None else None,
                low=r.low / f if r.low is not None else None,
                volume=Decimal(r.volume) * f if r.volume is not None else None,
                delivery_pct=r.delivery_pct,
                raw_close=r.close,
                cumulative_factor=f,
            )
        )
    return bars


# ---- database ------------------------------------------------------------------------------------


@dataclass
class AdjustmentReport:
    events: dict[int, list[Event]] = field(default_factory=dict)  # by security id
    skipped_actions: list[str] = field(default_factory=list)

    def applied(self) -> list[Event]:
        return [e for evs in self.events.values() for e in evs if e.applied]

    def unexplained(self) -> list[Event]:
        return [
            e
            for evs in self.events.values()
            for e in evs
            if e.kind is AdjustmentKind.UNEXPLAINED and not e.detail.endswith(EXPLAINED_NOTE)
        ]


def price_rows(session: Session, security_id: int, series: str = "EQ") -> list[PriceRow]:
    return [
        PriceRow(
            p.trade_date,
            p.close,
            p.prev_close,
            p.open,
            p.high,
            p.low,
            p.volume,
            p.turnover_inr,
            p.delivery_pct,
        )
        for p in session.scalars(
            select(EodPrice)
            .where(EodPrice.security_id == security_id, EodPrice.series == series)
            .order_by(EodPrice.trade_date)
        )
    ]


def rebuild_adjustments(
    session: Session, company_id: uuid.UUID, *, now: datetime | None = None
) -> AdjustmentReport:
    """Recompute and store the adjustment events of every listing of a company.

    `price_adjustments` is derived data (from prices and corporate actions), so it is replaced
    wholesale for the company's securities; nothing it is derived from is changed.
    """
    now = now or datetime.now(UTC)
    report = AdjustmentReport()
    securities = list(session.scalars(select(Security).where(Security.company_id == company_id)))
    actions = list(
        session.scalars(select(CorporateAction).where(CorporateAction.company_id == company_id))
    )
    explicit, report.skipped_actions = events_from_actions(actions)
    implied = {s.id: detect_implied(price_rows(session, s.id)) for s in securities}
    for s in securities:
        others = [e for o in securities if o.id != s.id for e in implied[o.id]]
        report.events[s.id] = reconcile(explicit, implied[s.id], others)
    ids = [s.id for s in securities]
    if ids:
        session.execute(delete(PriceAdjustment).where(PriceAdjustment.security_id.in_(ids)))
    for sid, events in report.events.items():
        for e in events:
            session.add(
                PriceAdjustment(
                    security_id=sid,
                    ex_date=e.ex_date,
                    factor=e.factor,
                    kind=e.kind,
                    origin=e.origin,
                    status=AdjustmentStatus.APPLIED
                    if e.applied
                    else AdjustmentStatus.REPORTED_ONLY,
                    confidence=e.confidence,
                    prior_close=e.prior_close,
                    reference_close=e.reference_close,
                    corporate_action_id=e.corporate_action_id,
                    detail=e.detail,
                    ratio_text=e.ratio_text,
                    source=e.source,
                    built_at=now,
                )
            )
    session.flush()
    store_adjusted_prices(session, securities, report, now)
    return report


def store_adjusted_prices(
    session: Session, securities: list[Security], report: "AdjustmentReport", now: datetime
) -> int:
    """Materialise the adjusted bars of each listing, separately from the official prices.

    Replaced wholesale per security (derived data). A bar stores the factor applied and the events
    behind it, so an adjusted figure can always be traced to its corporate actions.
    """
    ids = [s.id for s in securities]
    if not ids:
        return 0
    session.execute(delete(AdjustedPrice).where(AdjustedPrice.security_id.in_(ids)))
    stored = 0
    for sec in securities:
        events = [e for e in report.events.get(sec.id, []) if e.applied]
        for bar in adjust(price_rows(session, sec.id), events):
            behind = [
                {
                    "ex_date": e.ex_date.isoformat(),
                    "factor": str(e.factor),
                    "ratio_text": e.ratio_text,
                    "kind": e.kind.value,
                    "origin": e.origin.value,
                    "source": e.source,
                }
                for e in events
                if e.ex_date > bar.trade_date
            ]
            session.add(
                AdjustedPrice(
                    security_id=sec.id,
                    trade_date=bar.trade_date,
                    series="EQ",
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    raw_close=bar.raw_close,
                    cumulative_factor=bar.cumulative_factor,
                    events=behind,
                    built_at=now,
                )
            )
            stored += 1
    session.flush()
    return stored


def stored_events(session: Session, security_id: int) -> list[Event]:
    return [
        Event(
            ex_date=a.ex_date,
            factor=a.factor,
            kind=a.kind,
            origin=a.origin,
            applied=a.status is AdjustmentStatus.APPLIED,
            confidence=a.confidence,
            detail=a.detail,
            prior_close=a.prior_close,
            reference_close=a.reference_close,
            corporate_action_id=a.corporate_action_id,
        )
        for a in session.scalars(
            select(PriceAdjustment)
            .where(PriceAdjustment.security_id == security_id)
            .order_by(PriceAdjustment.ex_date)
        )
    ]


def adjusted_bars(session: Session, security_id: int, *, upto: date | None = None) -> list[Bar]:
    """The security's official EQ prices adjusted with the stored events."""
    rows = [r for r in price_rows(session, security_id) if upto is None or r.trade_date <= upto]
    return adjust(rows, stored_events(session, security_id))


def factor_between(events: list[Event], after: date, upto: date) -> Decimal:
    """Product of applied factors of events with `after` < ex_date <= `upto`."""
    factor = Decimal(1)
    for e in events:
        if e.applied and after < e.ex_date <= upto:
            factor *= e.factor
    return factor


__all__ = [
    "AdjustmentReport",
    "Bar",
    "Event",
    "PriceRow",
    "adjust",
    "adjusted_bars",
    "cumulative_factor",
    "detect_implied",
    "events_from_actions",
    "factor_between",
    "match_standard",
    "parse_action_factor",
    "price_rows",
    "rebuild_adjustments",
    "reconcile",
    "stored_events",
]
