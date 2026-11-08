"""Runs the metric catalogue for one company on its validated evidence.

Inputs are read from `evidence_resolutions` (the single value per metric, period and basis that
passed source authority and validation), never from the raw fact tables, and results are registered
through the evidence ledger, so every number carries its lineage. A metric whose inputs are absent,
blocked, or fail a precondition becomes a recorded GAP; nothing is estimated and nothing is zero.
"""

import operator
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calc.catalogue import (
    BANK_GROUP_CAVEAT,
    BANK_GROUP_NOTE,
    CATALOGUE,
    Metric,
    Ref,
    Variant,
)
from app.evidence.ledger import LedgerError, register_calculation
from app.models import EvidenceRecord, EvidenceResolution
from app.normalization import month_end

CREATED_BY = "calc-engine"
_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "!=": operator.ne}

Key = tuple[str, date, str, str]  # metric, period end, period type, basis


def shift_month_end(d: date, months: int) -> date:
    """The month end `months` before the month end `d`."""
    index = d.year * 12 + d.month - 1 - months
    return month_end(index // 12, index % 12 + 1)


@dataclass
class Gap:
    metric: str
    period_end: date | None
    period_type: str | None
    basis: str | None
    reason: str
    missing: list[str] = field(default_factory=list)


@dataclass
class FundamentalsResult:
    calculated: list[EvidenceRecord] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    profile: str = "NONFIN"
    reused: int = 0


class Inputs:
    """Usable evidence of one company, by (metric, period end, period type, basis)."""

    def __init__(self, session: Session, company_id: uuid.UUID) -> None:
        self.s = session
        self.usable: dict[Key, int] = {}
        self.blocked: set[Key] = set()
        self.local: dict[Key, EvidenceRecord] = {}  # calculations made in this run
        self._records: dict[int, EvidenceRecord] = {}
        for r in session.scalars(
            select(EvidenceResolution).where(
                EvidenceResolution.company_id == company_id, EvidenceResolution.dimension == ""
            )
        ):
            key = (r.metric, r.period_end, r.period_type, r.basis)
            if r.selected_evidence_id is None:
                continue
            if r.blocked:
                self.blocked.add(key)
            else:
                self.usable[key] = r.selected_evidence_id

    def record(self, key: Key) -> EvidenceRecord | None:
        if key in self.local:
            return self.local[key]
        evidence_id = self.usable.get(key)
        if evidence_id is None:
            return None
        if evidence_id not in self._records:
            self._records[evidence_id] = self.s.get(EvidenceRecord, evidence_id)
        return self._records[evidence_id]

    def has_metric(self, metric: str) -> bool:
        return any(k[0] == metric for k in (*self.usable, *self.local))

    def periods(self, metrics: tuple[str, ...], period_types: tuple[str, ...]) -> set[Key]:
        out = set()
        for k in (*self.usable, *self.local):
            if k[0] in metrics and k[2] in period_types:
                out.add(k)
        return out


def _anchors(metric: Metric) -> tuple[str, ...]:
    if metric.anchor:
        return metric.anchor
    names: list[str] = []
    for v in metric.variants:
        first = next(iter(v.inputs.values()))
        names.extend(first.metrics)
    return tuple(dict.fromkeys(names))


def _profile_of(inputs: Inputs) -> str:
    return "BANK" if inputs.has_metric("INCOME.interest_earned") else "NONFIN"


def _resolve(
    inputs: Inputs, ref: Ref, end: date, ptype: str, basis: str
) -> tuple[EvidenceRecord | None, str]:
    target = shift_month_end(end, ref.shift_months) if ref.shift_months else end
    ptype = ref.period_type or ptype
    for name in ref.metrics:
        record = inputs.record((name, target, ptype, basis))
        if record is not None:
            return record, name
    blocked = [n for n in ref.metrics if (n, target, ptype, basis) in inputs.blocked]
    label = " or ".join(ref.metrics)
    why = f" (blocked by validation: {', '.join(blocked)})" if blocked else ""
    return None, f"{label} for {target} {ptype} {basis}{why}"


def _try_variant(
    inputs: Inputs, variant: Variant, end: date, ptype: str, basis: str
) -> tuple[dict[str, EvidenceRecord] | None, list[str]]:
    found: dict[str, EvidenceRecord] = {}
    missing: list[str] = []
    for alias, ref in variant.inputs.items():
        record, what = _resolve(inputs, ref, end, ptype, basis)
        if record is None:
            missing.append(what)
        else:
            found[alias] = record
    return (found if not missing else None), missing


def _precondition_failure(metric: Metric, found: dict[str, EvidenceRecord]) -> str | None:
    for alias, op, number in metric.requires:
        record = found.get(alias)
        if record is None or record.value is None:
            continue
        if not _OPS[op](record.value, Decimal(number)):
            return (
                f"{alias} is {record.value:f} ({record.metric}); this metric needs {alias} "
                f"{op} {number}, otherwise it is undefined or misleading"
            )
    return None


def run_fundamentals(
    session: Session, company_id: uuid.UUID, *, now: datetime | None = None
) -> FundamentalsResult:
    inputs = Inputs(session, company_id)
    result = FundamentalsResult(profile=_profile_of(inputs))
    for metric in CATALOGUE:
        if metric.profile not in ("ANY", result.profile):
            continue
        anchors = sorted(
            inputs.periods(_anchors(metric), metric.period_types), key=lambda k: (k[3], k[2], k[1])
        )
        if not anchors:
            result.gaps.append(
                Gap(
                    metric.code,
                    None,
                    None,
                    None,
                    "no usable data for any period: " + ", ".join(_anchors(metric)),
                    list(_anchors(metric)),
                )
            )
            continue
        seen: set[tuple[date, str, str]] = set()
        for _name, end, ptype, basis in anchors:
            if (end, ptype, basis) in seen:
                continue
            seen.add((end, ptype, basis))
            _calculate(session, company_id, inputs, metric, end, ptype, basis, result, now)
    _drop_redundant_ttm_gaps(result)
    return result


def _drop_redundant_ttm_gaps(result: FundamentalsResult) -> None:
    """A TTM gap is noise when the same figure was obtained for that period or a later one by the
    other method (four quarters, or year + year-to-date - prior year-to-date)."""
    latest: dict[tuple[str, str], date] = {}
    for r in result.calculated:
        if r.metric.startswith("TTM.") and r.period_end is not None:
            k = (r.metric, r.basis or "")
            latest[k] = max(latest.get(k, r.period_end), r.period_end)
    result.gaps = [
        g
        for g in result.gaps
        if not (
            g.metric.startswith("TTM.")
            and (
                (g.period_end is None and any(m == g.metric for m, _ in latest))
                or (
                    g.period_end is not None
                    and latest.get((g.metric, g.basis or ""), date.min) >= g.period_end
                )
            )
        )
    ]


def _calculate(
    session: Session,
    company_id: uuid.UUID,
    inputs: Inputs,
    metric: Metric,
    end: date,
    ptype: str,
    basis: str,
    result: FundamentalsResult,
    now: datetime | None,
) -> None:
    best_missing: list[str] | None = None
    for index, variant in enumerate(metric.variants):
        if variant.profile not in ("ANY", result.profile):
            continue
        if variant.bases is not None and basis not in variant.bases:
            continue
        found, missing = _try_variant(inputs, variant, end, ptype, basis)
        if found is None:
            if best_missing is None or len(missing) < len(best_missing):
                best_missing = missing
            continue
        failure = _precondition_failure(metric, found)
        if failure:
            result.gaps.append(Gap(metric.code, end, ptype, basis, failure))
            return
        notes = [variant.note] if variant.note else []
        if (
            metric.code in BANK_GROUP_CAVEAT
            and result.profile == "BANK"
            and basis == "CONSOLIDATED"
        ):
            notes.append(BANK_GROUP_NOTE)
        try:
            record = register_calculation(
                session,
                company_id,
                metric.code,
                variant.expression,
                {alias: r.id for alias, r in found.items()},
                created_by=CREATED_BY,
                result_unit=metric.unit,
                spans_periods=metric.spans,
                notes=notes,
                definition=f"{metric.code}@{','.join(metric.period_types)}#{index}: {metric.label}",
                period_type_label=metric.period_label,
                now=now,
            )
        except LedgerError as exc:
            result.gaps.append(Gap(metric.code, end, ptype, basis, f"refused by the ledger: {exc}"))
            return
        inputs.local[(metric.code, end, metric.period_label or ptype, basis)] = record
        result.calculated.append(record)
        return
    if best_missing is not None:
        result.gaps.append(
            Gap(
                metric.code,
                end,
                ptype,
                basis,
                "missing input: " + "; ".join(best_missing),
                best_missing,
            )
        )


__all__ = ["FundamentalsResult", "Gap", "Inputs", "run_fundamentals", "shift_month_end"]
