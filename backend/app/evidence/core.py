"""Plain-data view of evidence plus source-authority resolution and quality grading.

Everything in this module is pure (no database), so the rules can be tested exhaustively with
small hand-written cases. `sync.py` fills `Fact` objects from the database; `rules.py` and
`engine.py` use them.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.models.evidence import Quality, ResolutionStatus
from app.normalization import PER_SHARE_UNITS, UNIT_TO_INR

CONFLICT_TOLERANCE = Decimal("0.01")  # 1% relative difference between sources (methodology 3)
USER_SUPPLIED_PROVIDERS = {"MANUAL_IMPORT", "MANUAL_UPLOAD"}

# Used when the `sources` table has no row for a provider (e.g. a fresh test database).
DEFAULT_TIERS = {
    "DOCUMENT_EXTRACTION": 2,
    "DERIVED_QUARTER": 2,
    "COMPANY_IR": 2,
    "UPSTOX_FUNDAMENTALS": 5,
    "MANUAL_IMPORT": 6,
    "MANUAL_UPLOAD": 6,
}
WEAKEST_TIER = 6

Key = tuple[str, str, date, str, str]  # metric, dimension, period_end, period_type, basis


def canon(value: Decimal | None) -> str | None:
    """A number's text that depends only on its value ('100', '100.0000' and '1E+2' are equal).

    Used wherever a hash or identity is built from a number: the same figure read from Python and
    from the database must be the same evidence.
    """
    if value is None:
        return None
    text = format(Decimal(value).normalize(), "f")
    return "0" if text in ("-0", "") else text


@dataclass
class Fact:
    """One FACT or CALCULATION record as the rules see it."""

    id: int
    cls: str  # FACT | CALCULATION
    metric: str
    dimension: str
    period_end: date
    period_type: str  # "" when the figure is a point-in-time balance
    basis: str  # "" when not applicable
    value: Decimal
    unit: str
    provider: str
    tier: int
    original_unit: str | None = None
    document_id: uuid.UUID | None = None
    page: int | None = None
    source_url: str | None = None
    retrieved_at: datetime | None = None
    details: dict = field(default_factory=dict)
    # Set when this record may not be used (document not the company's, unit unknown ...).
    problem: str | None = None

    @property
    def key(self) -> Key:
        return (self.metric, self.dimension, self.period_end, self.period_type, self.basis)

    @property
    def statement(self) -> str:
        return self.metric.split(".", 1)[0]

    @property
    def item(self) -> str:
        return self.metric.split(".", 1)[1] if "." in self.metric else self.metric

    @property
    def rounding(self) -> Decimal:
        """Half of the last printed digit, in the fact's normalized unit.

        A figure printed as 1,234 crore may really be anything from 1,233.5 to 1,234.5 crore, so two
        correct figures can differ by that much without either being wrong.
        """
        stored = self.details.get("rounding")
        if stored is not None:
            return Decimal(str(stored))
        decimals = int(self.details.get("decimals", 0))
        step = Decimal(10) ** -decimals
        original = (self.original_unit or "").lower()
        scale = (
            Decimal(1)
            if original in PER_SHARE_UNITS or self.unit != "inr"
            else UNIT_TO_INR.get(original, Decimal(1))
        )
        return step * scale / 2


def relative_difference(a: Decimal, b: Decimal) -> Decimal:
    """|a-b| relative to the larger magnitude (at least 1, as in the coverage engine)."""
    return abs(a - b) / max(abs(a), abs(b), Decimal(1))


def agree(a: Fact, b: Fact) -> bool:
    """Same figure for our purposes: within 1%, or within the rounding of the two printed values."""
    if a.unit != b.unit:
        return False
    return relative_difference(a.value, b.value) <= CONFLICT_TOLERANCE or (
        abs(a.value - b.value) <= a.rounding + b.rounding
    )


# ---- source authority and resolution ------------------------------------------------------------


@dataclass
class Resolved:
    key: Key
    status: ResolutionStatus
    selected: Fact | None
    alternatives: list[dict]
    max_difference_pct: Decimal | None
    note: str
    quality: Quality = Quality.LOW
    quality_reasons: list[str] = field(default_factory=list)


def _order(fact: Fact) -> tuple:
    # strongest authority first; among equals the most recently retrieved, then the lowest id
    stamp = fact.retrieved_at.timestamp() if fact.retrieved_at else 0.0
    return (fact.tier, -stamp, fact.id)


def _alt(fact: Fact, best: Fact | None, excluded: str | None = None) -> dict:
    difference = None
    if best is not None and fact is not best and fact.unit == best.unit:
        difference = str(
            (relative_difference(best.value, fact.value) * 100).quantize(Decimal("0.0001"))
        )
    return {
        "evidence_id": fact.id,
        "class": fact.cls,
        "provider": fact.provider,
        "tier": fact.tier,
        "value": str(fact.value),
        "unit": fact.unit,
        "difference_pct": difference,
        "document_id": str(fact.document_id) if fact.document_id else None,
        "page": fact.page,
        "excluded": excluded,
    }


def resolve_group(facts: list[Fact]) -> Resolved:
    """Pick the value a report may use for one key, or explain why none can be picked.

    Rules (methodology section 2): a stronger source beats a weaker one; sources of EQUAL authority
    that disagree beyond tolerance are a CONFLICT and nothing is chosen; a reported figure beats a
    calculated one; a record that failed a provenance check is listed but never chosen.
    """
    key = facts[0].key
    usable = [f for f in facts if f.problem is None]
    excluded = [_alt(f, None, f.problem) for f in facts if f.problem is not None]
    if not usable:
        return Resolved(
            key,
            ResolutionStatus.UNUSABLE,
            None,
            excluded,
            None,
            "every candidate failed a provenance check: "
            + "; ".join(sorted({f.problem for f in facts if f.problem})),
        )
    reported = [f for f in usable if f.cls == "FACT"]
    calculated = [f for f in usable if f.cls == "CALCULATION"]
    pool = sorted(reported or calculated, key=_order)
    best = pool[0]
    alternatives = [_alt(f, best) for f in pool[1:]] + [
        _alt(f, best) for f in calculated if reported
    ]
    alternatives += excluded
    differences = [
        relative_difference(best.value, f.value) for f in pool[1:] if f.unit == best.unit
    ]
    max_pct = (max(differences) * 100).quantize(Decimal("0.0001")) if differences else None
    disagreeing = [f for f in pool[1:] if not agree(best, f)]
    if not disagreeing:
        providers = {f.provider for f in pool}
        if best.cls == "CALCULATION":
            status = ResolutionStatus.CALCULATED
            note = "no reported figure; value is calculated"
        elif len(providers) > 1:
            status = ResolutionStatus.AGREED
            note = f"{len(providers)} sources agree within {CONFLICT_TOLERANCE:.0%}"
        else:
            status = ResolutionStatus.SINGLE_SOURCE
            note = "one source"
        return Resolved(key, status, best, alternatives, max_pct, note)
    rivals = [f for f in disagreeing if f.tier == best.tier]
    if rivals:
        names = ", ".join(sorted({best.provider, *(f.provider for f in rivals)}))
        units = sorted({best.unit, *(f.unit for f in rivals)})
        how = (
            f"disagree by up to {max_pct}%"
            if len(units) == 1
            else f"give the same measure in different units ({', '.join(units)})"
        )
        return Resolved(
            key,
            ResolutionStatus.CONFLICT,
            None,
            alternatives,
            max_pct,
            f"sources of equal authority (tier {best.tier}: {names}) {how}; no value chosen",
        )
    weaker = ", ".join(sorted({f.provider for f in disagreeing}))
    return Resolved(
        key,
        ResolutionStatus.RESOLVED_BY_AUTHORITY,
        best,
        alternatives,
        max_pct,
        f"{best.provider} (tier {best.tier}) used over {weaker} (weaker authority), "
        f"which differ by up to {max_pct}%",
    )


# ---- quality -------------------------------------------------------------------------------------


def grade(resolved: Resolved) -> tuple[Quality, list[str]]:
    """HIGH / MEDIUM / LOW with the reasons, never a probability (methodology section 5)."""
    if resolved.status is ResolutionStatus.CONFLICT:
        return Quality.LOW, ["sources of equal authority disagree"]
    if resolved.status is ResolutionStatus.UNUSABLE or resolved.selected is None:
        return Quality.LOW, ["no usable value"]
    fact = resolved.selected
    reasons: list[str] = []
    details = fact.details
    other_agree = int(details.get("other_documents_agree", 0))
    other_differ = int(details.get("other_documents_differ", 0))
    if fact.provider in USER_SUPPLIED_PROVIDERS:
        return Quality.LOW, ["supplied by a person and not independently verified"]
    if fact.tier >= 5:
        level = Quality.LOW
        reasons.append(f"secondary source (authority tier {fact.tier}), not a primary filing")
    else:
        level = Quality.MEDIUM
        reasons.append(
            f"primary source (authority tier {fact.tier}); arithmetic checks passed"
            if fact.provider == "DOCUMENT_EXTRACTION"
            else f"primary source (authority tier {fact.tier})"
        )
        if resolved.status is ResolutionStatus.AGREED:
            level = Quality.HIGH
            reasons.append("corroborated by another independent source")
        if other_agree:
            level = Quality.HIGH
            reasons.append(f"the same figure appears in {other_agree} other document(s)")
    if fact.cls == "CALCULATION":
        level = min(level, Quality.MEDIUM, key=_rank)
        reasons.append("calculated from reported figures; carries the rounding of its inputs")
    if details.get("ocr_confidence") is not None:
        level = min(level, Quality.MEDIUM, key=_rank)
        reasons.append("read from a scanned page (OCR cross-checked), never rated HIGH")
    if resolved.status is ResolutionStatus.RESOLVED_BY_AUTHORITY:
        level = min(level, Quality.MEDIUM, key=_rank)
        reasons.append("a weaker source disagrees")
    if other_differ:
        level = min(level, Quality.MEDIUM, key=_rank)
        reasons.append(f"{other_differ} other document(s) print a different figure (restated?)")
    if details.get("thin_peer_group"):
        level = Quality.LOW
        reasons.append("fewer peers than the minimum for a reliable comparison")
    if details.get("unit_unstated"):
        level = Quality.LOW
        reasons.append("the unit is not stated in the source")
    if details.get("restated"):
        reasons.append("restated by a later filing (earlier value kept in history)")
    return level, reasons


_ORDER = {Quality.LOW: 0, Quality.MEDIUM: 1, Quality.HIGH: 2}


def _rank(q: Quality) -> int:
    return _ORDER[q]


__all__ = [
    "canon",
    "CONFLICT_TOLERANCE",
    "DEFAULT_TIERS",
    "Fact",
    "Key",
    "Resolved",
    "USER_SUPPLIED_PROVIDERS",
    "WEAKEST_TIER",
    "agree",
    "grade",
    "relative_difference",
    "resolve_group",
]
