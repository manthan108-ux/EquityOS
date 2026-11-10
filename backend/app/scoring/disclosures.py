"""Bank key figures read from disclosure evidence (NIM, NPA, capital adequacy, CASA).

These are published ratios, not calculations: `docintel/kpi.py` reads them from investor
presentations and results press releases into DISCLOSURE evidence (`RATIOS.*`). The absolute scorer
may use them, but only after these checks - a figure that fails a check is a GAP with the reason,
never a guess and never a zero:

* unit: must be percent;
* plausibility: inside the range a real bank ratio can take (a misread table is caught here);
* period: the newest period not after the scoring date, and not older than the metric's limit;
* conflict: two ACTIVE records for the same period and dimension that disagree make the metric
  unusable until someone resolves them;
* dimension: a bank may publish a ratio on several bases (NIM on total assets vs on interest-earning
  assets, domestic vs whole bank). The least qualified is preferred, and the one used is named, so
  the reader knows which definition the score rests on.

These ratios are bank-level and carry no consolidated/standalone statement basis; they are
labelled "AS_REPORTED". `check_pair` enforces net NPA <= gross NPA.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EvidenceRecord, EvidenceStatus

DIMENSION_ORDER = {
    "": 0,
    "whole_bank": 1,
    "interest_earning_assets": 2,
    "domestic": 3,
    "total_assets": 4,
}
AS_REPORTED = "AS_REPORTED"


@dataclass
class Pick:
    record: EvidenceRecord | None
    reason: str | None = None  # why there is no usable record
    note: str | None = None  # what was used (period, dimension)


def _unit_ok(record: EvidenceRecord) -> bool:
    return (record.unit or "").lower() in ("percent", "%", "pct")


def pick(
    session: Session,
    company_id: uuid.UUID,
    codes: tuple[str, ...],
    *,
    as_of: date,
    bounds: tuple[float, float] | None,
    max_age_days: int | None,
) -> Pick:
    rows = list(
        session.scalars(
            select(EvidenceRecord).where(
                EvidenceRecord.company_id == company_id,
                EvidenceRecord.metric.in_(codes),
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
                EvidenceRecord.value.is_not(None),
                EvidenceRecord.period_end <= as_of,
            )
        )
    )
    if not rows:
        return Pick(None, "the company has no disclosed value for it")
    newest = max(r.period_end for r in rows)
    if max_age_days is not None and (as_of - newest).days > max_age_days:
        return Pick(
            None, f"the newest disclosure ({newest}) is older than {max_age_days} days; not used"
        )
    at_newest = [r for r in rows if r.period_end == newest]
    problems = []
    usable = []
    for r in at_newest:
        if not _unit_ok(r):
            problems.append(f"unit {r.unit!r} is not percent")
        elif bounds and not (bounds[0] <= float(r.value) <= bounds[1]):
            problems.append(
                f"{r.value.normalize():f}% is outside the plausible range "
                f"{bounds[0]:g}-{bounds[1]:g}% (a misread figure is not scored)"
            )
        else:
            usable.append(r)
    if not usable:
        return Pick(None, "; ".join(dict.fromkeys(problems)) or "no usable value")
    best_dim = min(DIMENSION_ORDER.get(r.dimension or "", 9) for r in usable)
    best = [r for r in usable if DIMENSION_ORDER.get(r.dimension or "", 9) == best_dim]
    values = {Decimal(r.value).normalize() for r in best}
    if len(values) > 1:
        return Pick(
            None,
            f"conflicting disclosed values for {newest}: "
            + ", ".join(sorted(f"{v:f}" for v in values))
            + "; resolve the conflict before it can be scored",
        )
    chosen = sorted(best, key=lambda r: r.id)[0]
    dim = f", {chosen.dimension.replace('_', ' ')}" if chosen.dimension else ""
    return Pick(chosen, None, f"disclosed for {newest}{dim}; basis {AS_REPORTED}")


def check_pair(gross: Pick, net: Pick) -> str | None:
    """Net NPA cannot exceed gross NPA at the same date: if it does, neither figure is trusted."""
    if gross.record is None or net.record is None:
        return None
    if gross.record.period_end != net.record.period_end:
        return None
    if Decimal(net.record.value) > Decimal(gross.record.value):
        return (
            f"net NPA ({net.record.value.normalize():f}%) exceeds gross NPA "
            f"({gross.record.value.normalize():f}%) for {gross.record.period_end}: inconsistent"
        )
    return None
