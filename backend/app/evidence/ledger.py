"""The only supported way to create CALCULATION, ASSUMPTION and INTERPRETATION evidence, and to
read resolved values. Phases 7-11 (calculations, agents, report) go through here.

What the ledger enforces, so the other phases cannot get it wrong:

* a calculation's value is computed here from stored evidence (never supplied by the caller), only
  from the *resolved* value of each input, never from a blocked or losing source;
* consolidated and standalone inputs are never combined; quarterly and annual never combined;
  figures of different periods only when the caller says the calculation spans periods;
* units are tracked through the formula (see expression.py);
* an assumption must state what it is and why;
* an interpretation must cite evidence, and every number in it must be found in that evidence.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.claims import CitedValue, check_figures
from app.evidence.core import canon
from app.evidence.expression import (
    ExpressionError,
    Quantity,
    apply_result_unit,
    evaluate,
    names_in,
)
from app.evidence.sync import load_tiers
from app.models import (
    Document,
    EodPrice,
    EvidenceClass,
    EvidenceLink,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    LinkRole,
    Quality,
    ResolutionStatus,
    Security,
)

ENGINE_PROVIDER = "EVIDENCE_ENGINE"
_METRIC = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
MIN_RATIONALE = 15


class LedgerError(ValueError):
    """A registration broke an evidence rule. Nothing was written."""


def _digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _record(session: Session, company_id: uuid.UUID, evidence_id: int) -> EvidenceRecord:
    record = session.get(EvidenceRecord, evidence_id)
    if record is None or record.company_id != company_id:
        raise LedgerError(f"evidence {evidence_id} does not exist for this company")
    if record.status is not EvidenceStatus.ACTIVE:
        raise LedgerError(
            f"evidence {evidence_id} is {record.status.value} "
            f"({record.status_reason or 'no reason'})"
            " and cannot be used"
        )
    return record


def resolution_for(session: Session, record: EvidenceRecord) -> EvidenceResolution | None:
    if record.period_end is None or record.evidence_class not in (
        EvidenceClass.FACT,
        EvidenceClass.CALCULATION,
    ):
        return None
    return session.scalar(
        select(EvidenceResolution).where(
            EvidenceResolution.company_id == record.company_id,
            EvidenceResolution.metric == record.metric,
            EvidenceResolution.dimension == (record.dimension or ""),
            EvidenceResolution.period_end == record.period_end,
            EvidenceResolution.period_type == (record.period_type or ""),
            EvidenceResolution.basis == (record.basis or ""),
        )
    )


def _require_usable(session: Session, record: EvidenceRecord) -> None:
    """A FACT/CALCULATION may be built on only when it is its key's resolved, unblocked value."""
    if record.source_table in ("eod_prices", "price_adjustments"):
        return  # derived directly from the official price files: nothing to reconcile against
    resolution = resolution_for(session, record)
    if record.evidence_class is EvidenceClass.CALCULATION and record.source_table is None:
        # calculations made by the ledger are not resolved against sources; blocked via lineage
        if resolution is not None and resolution.blocked:
            raise LedgerError(f"evidence {record.id} is blocked by validation errors")
        return
    if resolution is None:
        raise LedgerError(
            f"evidence {record.id} ({record.metric} {record.period_end}) has not been validated: "
            "run the validation engine first"
        )
    if resolution.blocked:
        raise LedgerError(
            f"evidence {record.id} ({record.metric} {record.period_end}) is blocked by "
            f"validation: {', '.join(resolution.blocked_by or [])}"
        )
    if resolution.selected_evidence_id != record.id:
        raise LedgerError(
            f"evidence {record.id} is not the resolved value for {record.metric} "
            f"{record.period_end} ({resolution.status.value}; resolved id is "
            f"{resolution.selected_evidence_id})"
        )


# ---- assumptions -------------------------------------------------------------------------------


def register_assumption(
    session: Session,
    company_id: uuid.UUID,
    metric: str,
    statement: str,
    *,
    rationale: str,
    created_by: str,
    value: Decimal | None = None,
    unit: str | None = None,
    period_end: date | None = None,
    period_type: str | None = None,
    basis: str | None = None,
    supports: tuple[int, ...] = (),
    now: datetime | None = None,
) -> EvidenceRecord:
    """Record an assumption: stated, justified, attributed. Idempotent for identical input."""
    if not _METRIC.match(metric):
        raise LedgerError(f"metric {metric!r} must look like 'FORECAST.revenue_growth'")
    if not statement or not statement.strip():
        raise LedgerError("an assumption must state what is assumed")
    if len(rationale.strip()) < MIN_RATIONALE:
        raise LedgerError(f"an assumption needs a rationale (at least {MIN_RATIONALE} characters)")
    if not created_by.strip():
        raise LedgerError("an assumption must name who or what made it")
    if (value is None) != (unit is None):
        raise LedgerError("give both a value and its unit, or neither")
    for evidence_id in supports:
        _record(session, company_id, evidence_id)
    digest = _digest(
        company_id,
        metric,
        statement,
        rationale,
        canon(value),
        unit,
        str(period_end),
        period_type,
        basis,
        sorted(supports),
        created_by,
    )
    existing = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.evidence_class == EvidenceClass.ASSUMPTION,
            EvidenceRecord.content_hash == digest,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )
    if existing is not None:
        return existing
    record = EvidenceRecord(
        company_id=company_id,
        evidence_class=EvidenceClass.ASSUMPTION,
        kind="ASSUMPTION",
        metric=metric,
        period_end=period_end,
        period_type=period_type,
        basis=basis,
        value=value,
        unit=unit,
        statement_text=statement.strip(),
        details={"rationale": rationale.strip()},
        content_hash=digest,
        created_by=created_by,
        status_changed_at=now or datetime.now(UTC),
    )
    session.add(record)
    session.flush()
    for evidence_id in supports:
        session.add(
            EvidenceLink(evidence_id=record.id, input_id=evidence_id, role=LinkRole.SUPPORTS)
        )
    session.flush()
    return record


# ---- calculations ------------------------------------------------------------------------------


def register_calculation(
    session: Session,
    company_id: uuid.UUID,
    metric: str,
    expression: str,
    inputs: dict[str, int],
    *,
    created_by: str,
    result_unit: str | None = None,
    spans_periods: bool = False,
    dimension: str = "",
    notes: list[str] | None = None,
    definition: str | None = None,
    period_type_label: str | None = None,
    now: datetime | None = None,
) -> EvidenceRecord:
    """Compute `expression` over stored evidence and record the result with its lineage.

    `inputs` maps each name used in the formula to an evidence id. The value is computed here.
    `spans_periods=True` is required when inputs belong to different period ends (growth, CAGR);
    even then quarterly and annual figures cannot be combined, EXCEPT when the caller names the
    result's period (`period_type_label="TTM"`): a trailing-twelve-month figure built as
    "year + year-to-date - prior year-to-date" legitimately combines annual and cumulative
    inputs, and the label says what the result is.
    """
    if not _METRIC.match(metric):
        raise LedgerError(f"metric {metric!r} must look like 'RATIO.return_on_equity'")
    try:
        used = names_in(expression)
    except ExpressionError as exc:
        raise LedgerError(str(exc)) from exc
    missing = [name for name in used if name not in inputs]
    extra = [name for name in inputs if name not in used]
    if missing or extra:
        raise LedgerError(
            "formula and inputs disagree"
            + (f"; no evidence for: {', '.join(missing)}" if missing else "")
            + (f"; unused inputs: {', '.join(extra)}" if extra else "")
        )
    records: dict[str, EvidenceRecord] = {}
    variables: dict[str, Quantity] = {}
    for alias, evidence_id in inputs.items():
        record = _record(session, company_id, evidence_id)
        if record.evidence_class is EvidenceClass.INTERPRETATION:
            raise LedgerError(f"evidence {evidence_id} is an interpretation; it cannot be an input")
        if record.value is None or record.unit is None:
            raise LedgerError(f"evidence {evidence_id} has no numeric value")
        if record.evidence_class is not EvidenceClass.ASSUMPTION:
            _require_usable(session, record)
        records[alias] = record
        variables[alias] = Quantity(record.value, record.unit)

    measured = [r for r in records.values() if r.evidence_class is not EvidenceClass.ASSUMPTION]
    bases = {r.basis for r in measured if r.basis}
    if len(bases) > 1:
        raise LedgerError(
            f"inputs mix {' and '.join(sorted(bases))} figures; one basis only (never mixed)"
        )
    types = {r.period_type for r in measured if r.period_type}
    if len(types) > 1 and period_type_label is None:
        raise LedgerError(f"inputs mix {' and '.join(sorted(types))} periods")
    ends = {r.period_end for r in measured if r.period_end}
    if len(ends) > 1 and not spans_periods:
        raise LedgerError(
            "inputs belong to different period ends "
            f"({', '.join(sorted(str(e) for e in ends))}); pass spans_periods=True only for "
            "growth-type calculations"
        )
    try:
        outcome = evaluate(expression, variables)
        unit = apply_result_unit(outcome.unit, result_unit)
    except ExpressionError as exc:
        raise LedgerError(f"calculation refused: {exc}") from exc
    period_end = max(ends) if ends else None
    basis = next(iter(bases)) if bases else None
    period_type = period_type_label or (next(iter(types)) if types else None)
    weakest = max((r.authority_tier or 6 for r in measured), default=None)
    aliases = {alias: r.id for alias, r in sorted(records.items())}
    digest = _digest(
        company_id,
        metric,
        expression,
        aliases,
        canon(outcome.value),
        unit,
        str(period_end),
        basis,
        period_type,
    )
    existing = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.evidence_class == EvidenceClass.CALCULATION,
            EvidenceRecord.content_hash == digest,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )
    if existing is not None:
        return existing
    stamp = now or datetime.now(UTC)
    record = EvidenceRecord(
        company_id=company_id,
        evidence_class=EvidenceClass.CALCULATION,
        kind="CALCULATION",
        metric=metric,
        dimension=dimension,
        period_end=period_end,
        period_type=period_type,
        basis=basis,
        value=outcome.value,
        unit=unit,
        expression=expression,
        provider_code=ENGINE_PROVIDER,
        authority_tier=weakest,
        source_url=f"calculation:{metric}",
        retrieved_at=stamp,
        details={
            "inputs": {
                alias: {
                    "evidence_id": r.id,
                    "metric": r.metric,
                    "period_end": str(r.period_end) if r.period_end else None,
                    "value": str(r.value),
                    "unit": r.unit,
                    "class": r.evidence_class.value,
                }
                for alias, r in sorted(records.items())
            },
            "spans_periods": spans_periods,
            "notes": notes or [],
            "mixed_period_types": sorted(types) if len(types) > 1 else None,
            "definition": definition,
            "assumption_inputs": [
                a for a, r in records.items() if r.evidence_class is EvidenceClass.ASSUMPTION
            ],
        },
        content_hash=digest,
        created_by=created_by,
        status_changed_at=stamp,
    )
    session.add(record)
    session.flush()
    for alias, source in records.items():
        session.add(
            EvidenceLink(
                evidence_id=record.id, input_id=source.id, role=LinkRole.INPUT, alias=alias
            )
        )
    # A recalculation of the same quantity by the same method replaces the earlier one (which
    # stays, as SUPERSEDED). A DIFFERENT method for the same quantity (two ways to get a trailing
    # twelve-month figure) is kept: if the two disagree that is a conflict worth seeing.
    earlier_query = select(EvidenceRecord).where(
        EvidenceRecord.company_id == company_id,
        EvidenceRecord.evidence_class == EvidenceClass.CALCULATION,
        EvidenceRecord.source_table.is_(None),
        EvidenceRecord.metric == metric,
        EvidenceRecord.dimension == dimension,
        EvidenceRecord.period_end == period_end,
        EvidenceRecord.period_type == period_type,
        EvidenceRecord.basis == basis,
        EvidenceRecord.status == EvidenceStatus.ACTIVE,
        EvidenceRecord.id != record.id,
    )
    if definition is not None:
        method = definition.split("#")[0]
        text = EvidenceRecord.details["definition"].as_string()
        earlier_query = earlier_query.where(
            text.like(f"{method}#%") if "#" in definition else text == definition
        )
    for old in session.scalars(earlier_query):
        old.status = EvidenceStatus.SUPERSEDED
        old.status_reason = f"recalculated as evidence {record.id}"
        old.status_changed_at = stamp
    session.flush()
    return record


# ---- interpretations ---------------------------------------------------------------------------


def register_interpretation(
    session: Session,
    company_id: uuid.UUID,
    topic: str,
    statement: str,
    cites: list[int],
    *,
    created_by: str,
    period_end: date | None = None,
    basis: str | None = None,
    now: datetime | None = None,
) -> EvidenceRecord:
    """Record a reasoned statement. It must cite evidence and may use no other number."""
    if not _METRIC.match(topic):
        raise LedgerError(f"topic {topic!r} must look like 'THESIS.profitability'")
    if not statement or not statement.strip():
        raise LedgerError("an interpretation needs text")
    if not cites:
        raise LedgerError("an interpretation must cite at least one piece of evidence")
    cited_records: list[EvidenceRecord] = []
    for evidence_id in dict.fromkeys(cites):
        record = _record(session, company_id, evidence_id)
        if record.evidence_class is EvidenceClass.INTERPRETATION:
            raise LedgerError(
                f"evidence {evidence_id} is itself an interpretation; cite the facts it rests on"
            )
        if record.evidence_class in (EvidenceClass.FACT, EvidenceClass.CALCULATION):
            resolution = resolution_for(session, record)
            if resolution is not None and resolution.blocked:
                raise LedgerError(f"evidence {evidence_id} is blocked by validation errors")
        cited_records.append(record)
    check = check_figures(
        statement,
        [
            CitedValue(r.id, r.value, r.unit)
            for r in cited_records
            if r.value is not None and r.unit is not None
        ],
    )
    if not check.ok:
        raise LedgerError(
            "numbers not found in the cited evidence: "
            + ", ".join(repr(f.text) for f in check.untraced)
            + ". Cite the fact/calculation/assumption that supplies each number."
        )
    digest = _digest(
        company_id,
        topic,
        statement.strip(),
        sorted(r.id for r in cited_records),
        str(period_end),
        basis,
        created_by,
    )
    existing = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.evidence_class == EvidenceClass.INTERPRETATION,
            EvidenceRecord.content_hash == digest,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )
    if existing is not None:
        return existing
    record = EvidenceRecord(
        company_id=company_id,
        evidence_class=EvidenceClass.INTERPRETATION,
        kind="CLAIM",
        metric=topic,
        period_end=period_end,
        basis=basis,
        statement_text=statement.strip(),
        details={"figures_traced": [[f.text, evidence_id] for f, evidence_id in check.matched]},
        content_hash=digest,
        created_by=created_by,
        status_changed_at=now or datetime.now(UTC),
    )
    session.add(record)
    session.flush()
    for cited in cited_records:
        session.add(EvidenceLink(evidence_id=record.id, input_id=cited.id, role=LinkRole.CITES))
    session.flush()
    return record


def register_text_fact(
    session: Session,
    company_id: uuid.UUID,
    metric: str,
    text: str,
    *,
    provider_code: str,
    source_url: str,
    created_by: str,
    retrieved_at: datetime | None = None,
    period_end: date | None = None,
    document_id: uuid.UUID | None = None,
    page: int | None = None,
    details: dict | None = None,
) -> EvidenceRecord:
    """A narrative FACT from a source that has no fact table (a news item, a regulator notice).

    The text is stored exactly as given, with where it came from. Its authority is the provider's
    registered tier (6 = weakest if the provider is unknown). Idempotent for identical input.
    """
    if not _METRIC.match(metric):
        raise LedgerError(f"metric {metric!r} must look like 'NEWS.headline'")
    if not text or not text.strip():
        raise LedgerError("a text fact needs text")
    if not source_url or not source_url.strip():
        raise LedgerError("a fact must say where it came from (source_url)")
    if document_id is not None:
        document = session.get(Document, document_id)
        if document is None or document.company_id != company_id:
            raise LedgerError("that document does not belong to this company")
    tiers = load_tiers(session)
    digest = _digest(
        company_id,
        metric,
        text.strip(),
        provider_code,
        source_url,
        str(document_id),
        page,
        str(period_end),
    )
    existing = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.evidence_class == EvidenceClass.FACT,
            EvidenceRecord.content_hash == digest,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )
    if existing is not None:
        return existing
    record = EvidenceRecord(
        company_id=company_id,
        evidence_class=EvidenceClass.FACT,
        kind="TEXT",
        metric=metric,
        period_end=period_end,
        statement_text=text.strip(),
        provider_code=provider_code,
        authority_tier=tiers.get(provider_code, 6),
        source_url=source_url,
        document_id=document_id,
        source_page=page,
        retrieved_at=retrieved_at or datetime.now(UTC),
        details=details or None,
        content_hash=digest,
        created_by=created_by,
        status_changed_at=datetime.now(UTC),
    )
    session.add(record)
    session.flush()
    return record


def withdraw(session: Session, evidence_id: int, reason: str, *, by: str) -> EvidenceRecord:
    """Withdraw a calculation, assumption or interpretation made through the ledger.

    Evidence mirrored from the fact tables cannot be withdrawn here: correct the source instead.
    """
    record = session.get(EvidenceRecord, evidence_id)
    if record is None:
        raise LedgerError(f"no evidence {evidence_id}")
    if record.source_table is not None:
        raise LedgerError("mirrored facts are withdrawn by correcting their source, not here")
    if not reason.strip():
        raise LedgerError("a reason is required")
    record.status = EvidenceStatus.WITHDRAWN
    record.status_reason = f"{by}: {reason.strip()}"[:200]
    record.status_changed_at = datetime.now(UTC)
    session.flush()
    return record


# ---- reading -----------------------------------------------------------------------------------


class NoUsableValue(LookupError):
    """There is no value a report may use (missing, conflicting, unusable or blocked)."""


@dataclass
class ResolvedValue:
    evidence: EvidenceRecord
    status: ResolutionStatus
    quality: Quality
    reasons: list[str]
    alternatives: list[dict]
    note: str | None


def resolved_value(
    session: Session,
    company_id: uuid.UUID,
    metric: str,
    period_end: date,
    *,
    period_type: str = "",
    basis: str = "",
    dimension: str = "",
) -> ResolvedValue:
    """The value a report may use, or NoUsableValue saying exactly why there is none."""
    row = session.scalar(
        select(EvidenceResolution).where(
            EvidenceResolution.company_id == company_id,
            EvidenceResolution.metric == metric,
            EvidenceResolution.dimension == dimension,
            EvidenceResolution.period_end == period_end,
            EvidenceResolution.period_type == period_type,
            EvidenceResolution.basis == basis,
        )
    )
    label = f"{metric} {period_end} {period_type or 'point'} {basis or ''}".strip()
    if row is None:
        raise NoUsableValue(f"{label}: no evidence stored (or validation has not run)")
    if row.selected_evidence_id is None:
        raise NoUsableValue(f"{label}: {row.status.value}: {row.note}")
    if row.blocked:
        raise NoUsableValue(
            f"{label}: blocked by validation errors ({', '.join(row.blocked_by or [])})"
        )
    record = session.get(EvidenceRecord, row.selected_evidence_id)
    if record is None or record.status is not EvidenceStatus.ACTIVE:
        raise NoUsableValue(
            f"{label}: the selected evidence is no longer current; re-run validation"
        )
    return ResolvedValue(
        evidence=record,
        status=row.status,
        quality=row.quality,
        reasons=list(row.quality_reasons),
        alternatives=list(row.alternatives),
        note=row.note,
    )


def price_evidence(
    session: Session, company_id: uuid.UUID, security_id: int, trade_date: date, series: str = "EQ"
) -> EvidenceRecord:
    """FACT for an official end-of-day close (created on demand: the price table is huge).

    Prices are UNADJUSTED for corporate actions; the record says so and Phase 7 must adjust before
    comparing across a split or bonus.
    """
    security = session.get(Security, security_id)
    if security is None or security.company_id != company_id:
        raise LedgerError("that security does not belong to this company")
    price = session.scalar(
        select(EodPrice).where(
            EodPrice.security_id == security_id,
            EodPrice.trade_date == trade_date,
            EodPrice.series == series,
        )
    )
    if price is None or price.close is None:
        raise LedgerError(f"no stored close for {security.symbol} on {trade_date}")
    source_id = f"{security_id}|{trade_date}|{series}"
    digest = _digest(source_id, str(price.close), price.source_url, price.file_sha256)
    existing = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.source_table == "eod_prices",
            EvidenceRecord.source_id == source_id,
            EvidenceRecord.content_hash == digest,
        )
    )
    if existing is not None:
        return existing
    record = EvidenceRecord(
        company_id=company_id,
        evidence_class=EvidenceClass.FACT,
        kind="PRICE",
        metric="PRICE.close",
        dimension=f"{security.symbol}",
        period_end=trade_date,
        value=price.close,
        unit="inr_per_share",
        original_value=price.close,
        original_unit="inr_per_share",
        provider_code="MARKET_DATA",
        authority_tier=1,
        source_table="eod_prices",
        source_id=source_id,
        source_url=price.source_url,
        retrieved_at=price.retrieved_at,
        details={"unadjusted": True, "series": series, "file_sha256": price.file_sha256},
        content_hash=digest,
        created_by="evidence-price",
        status_changed_at=datetime.now(UTC),
    )
    session.add(record)
    session.flush()
    return record


__all__ = [
    "ENGINE_PROVIDER",
    "LedgerError",
    "NoUsableValue",
    "ResolvedValue",
    "price_evidence",
    "register_assumption",
    "register_calculation",
    "register_interpretation",
    "register_text_fact",
    "resolution_for",
    "resolved_value",
    "withdraw",
]
