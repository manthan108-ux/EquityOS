# ruff: noqa: E501
"""The valuation assumption ledger: what a valuation is allowed to rest on, and who said so.

Every input to a valuation is one of four kinds, and the report keeps them apart:

* HISTORICAL_ACTUAL  a validated figure from the evidence store (price, P/B, ROE): a FACT or a CALCULATION
  with its evidence id.
* SOURCED_INPUT      a market input (risk-free rate, equity risk premium, beta) or other figure taken from a
  NAMED, DATED source (a government yield, a published premium, a beta computed by a named provider). It
  records the source, an optional URL, the date the figure refers to, who entered it and why it applies.
* USER_ASSUMPTION    a judgement a person makes (cost of equity, long-run growth, ROE shift per scenario). It
  records the author, the date and the rationale, and is labelled as a judgement, never as data.
* DERIVED_OUTPUT     a calculation over the above (the CAPM cost of equity, the justified P/B, a value per
  share), registered with links to every input it used.

The first rule is that nothing market-related is ever invented. There is no default risk-free rate, equity
risk premium, beta, cost of equity or growth rate: a valuation that needs one that has not been entered is
listed as NOT COMPLETED with exactly what is missing. A figure is accepted without a source only as a
USER_ASSUMPTION, and is then labelled as one (a "market" input without a source is flagged as not
source-backed). The service cannot verify a citation; it records it exactly as given and dates it.

Editing never overwrites. Changing an input registers a new record that names the one it replaces and
retires the old one as SUPERSEDED with the reason and the editor; the history is kept and queryable.
Withdrawing retires an input without a replacement.

A sourced input older than its limit is flagged STALE in the report (never silently used as if current); the
limits are rules stated here: risk-free rate 45 days, equity risk premium and beta 400 days.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence import ledger
from app.evidence.ledger import LedgerError
from app.models import EvidenceClass, EvidenceLink, EvidenceRecord, EvidenceStatus

PREFIX = "VALUATION_INPUT"
SCENARIOS = ("bear", "base", "bull")
SOURCED, USER = "SOURCED_INPUT", "USER_ASSUMPTION"
STALE_NEVER = None


@dataclass(frozen=True)
class InputSpec:
    name: str
    label: str
    unit: str  # percent | ratio
    low: Decimal  # plausible range, to catch a unit slip (0.07 instead of 7)
    high: Decimal
    market: bool  # a market input: expected to be source-backed
    max_age_days: int | None
    scenario: bool = False  # one value per scenario (name_bear / name_base / name_bull)


SPECS: dict[str, InputSpec] = {
    "risk_free_rate": InputSpec("risk_free_rate", "Risk-free rate", "percent", Decimal(0), Decimal(25), True, 45),
    "equity_risk_premium": InputSpec("equity_risk_premium", "Equity risk premium", "percent", Decimal(0), Decimal(15), True, 400),
    "beta": InputSpec("beta", "Beta", "ratio", Decimal("0.05"), Decimal(5), True, 400),
    "cost_of_equity": InputSpec("cost_of_equity", "Cost of equity", "percent", Decimal(1), Decimal(40), False, None, scenario=True),
    "terminal_growth": InputSpec("terminal_growth", "Long-run growth", "percent", Decimal(-5), Decimal(15), False, None, scenario=True),
    "roe_shift": InputSpec("roe_shift", "Shift in return on equity", "percent", Decimal(-20), Decimal(20), False, None, scenario=True),
}  # fmt: skip
MIN_RATIONALE = ledger.MIN_RATIONALE


class AssumptionError(ValueError):
    """An input was refused; nothing was written."""


def metric_for(name: str, scenario: str | None) -> str:
    spec = SPECS.get(name)
    if spec is None:
        raise AssumptionError(f"unknown input {name!r}; known: {', '.join(sorted(SPECS))}")
    if spec.scenario:
        if scenario not in SCENARIOS:
            raise AssumptionError(
                f"{spec.label} is set per scenario ({', '.join(SCENARIOS)}); give one"
            )
        return f"{PREFIX}.{name}_{scenario}"
    if scenario is not None:
        raise AssumptionError(f"{spec.label} is not set per scenario")
    return f"{PREFIX}.{name}"


def _current(session: Session, company_id: uuid.UUID, metric: str) -> EvidenceRecord | None:
    return session.scalar(
        select(EvidenceRecord)
        .where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.metric == metric,
            EvidenceRecord.evidence_class == EvidenceClass.ASSUMPTION,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
        .order_by(EvidenceRecord.id.desc())
    )


def _retire_dependents(session: Session, input_id: int, reason: str, now: datetime | None) -> int:
    """Withdraw the valuation calculations built on an input that has just been replaced or withdrawn.

    A derived figure (the CAPM cost of equity) must not stay current while an input it used is not; it is
    registered again from the current inputs the next time a valuation is built.
    """
    rows = session.scalars(
        select(EvidenceRecord)
        .join(EvidenceLink, EvidenceLink.evidence_id == EvidenceRecord.id)
        .where(
            EvidenceLink.input_id == input_id,
            EvidenceRecord.evidence_class == EvidenceClass.CALCULATION,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.created_by == "valuation-engine",
        )
    ).all()
    for r in rows:
        r.status = EvidenceStatus.WITHDRAWN
        r.status_reason = f"an input was changed: {reason}"[:200]
        r.status_changed_at = now or datetime.now(UTC)
    session.flush()
    return len(rows)


def _same_input(old, value, unit, source, source_url, as_of, author, rationale) -> bool:
    d = old.details or {}
    return bool(
        old.value == value
        and old.unit == unit
        and (d.get("source") or None) == (source or None)
        and (d.get("source_url") or None) == ((source_url or "").strip() or None)
        and d.get("as_of") == as_of.isoformat()
        and old.created_by == author.strip()
        and (d.get("rationale") or "") == rationale.strip()
    )


def set_input(
    session: Session,
    company_id: uuid.UUID,
    name: str,
    value: Decimal | str | float,
    *,
    author: str,
    rationale: str,
    as_of: date,
    scenario: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    edit_reason: str | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> EvidenceRecord:
    """Register (or edit) one valuation input. An existing active input of the same kind is superseded."""
    today = today or date.today()
    spec = SPECS.get(name)
    metric = metric_for(name, scenario)
    try:
        v = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise AssumptionError(f"{value!r} is not a number") from exc
    if not v.is_finite():
        raise AssumptionError("the value must be a finite number")
    if not (spec.low <= v <= spec.high):
        unit = "%" if spec.unit == "percent" else ""
        raise AssumptionError(f"{spec.label} {v}{unit} is outside the plausible range {spec.low} to {spec.high}{unit}; enter percent points (7 for 7%), not a fraction")  # fmt: skip
    if as_of > today:
        raise AssumptionError("the as-of date is in the future")
    if not author or not author.strip():
        raise AssumptionError("an input must name who entered it")
    if not rationale or len(rationale.strip()) < MIN_RATIONALE:
        raise AssumptionError(f"an input needs a rationale of at least {MIN_RATIONALE} characters: why this value applies to this company")  # fmt: skip
    source = (source or "").strip() or None
    if source_url and not source:
        raise AssumptionError("a source URL needs a source name")
    klass = SOURCED if source else USER
    old = _current(session, company_id, metric)
    if old is not None and _same_input(
        old, v, spec.unit, source, source_url, as_of, author, rationale
    ):
        return old  # entering the identical input again changes nothing
    extra = {
        "input": name,
        "scenario": scenario,
        "class": klass,
        "source": source,
        "source_url": (source_url or "").strip() or None,
        "as_of": as_of.isoformat(),
        "entered_on": today.isoformat(),
        "supersedes": old.id if old is not None else None,
        "source_backed": bool(source),
    }
    statement = f"{spec.label}{f' ({scenario} scenario)' if scenario else ''} is {v}{'%' if spec.unit == 'percent' else ''} as of {as_of.isoformat()}" + (f", per {source}" if source else ", as the author's own assumption")  # fmt: skip
    try:
        record = ledger.register_assumption(
            session, company_id, metric, statement, rationale=rationale, created_by=author.strip(),
            value=v, unit=spec.unit, period_end=as_of, extra=extra, now=now,
        )  # fmt: skip
    except LedgerError as exc:
        raise AssumptionError(str(exc)) from exc
    if old is not None and old.id != record.id:
        old.status = EvidenceStatus.SUPERSEDED
        old.status_reason = f"{author.strip()}: replaced by evidence {record.id}" + (f" ({edit_reason.strip()})" if edit_reason and edit_reason.strip() else "")  # fmt: skip
        old.status_reason = old.status_reason[:200]
        old.status_changed_at = now or datetime.now(UTC)
        session.flush()
        _retire_dependents(session, old.id, f"{old.metric} was edited", now)
    return record


def withdraw_input(session: Session, evidence_id: int, reason: str, *, by: str) -> EvidenceRecord:
    record = session.get(EvidenceRecord, evidence_id)
    if record is None or record.evidence_class is not EvidenceClass.ASSUMPTION or not record.metric.startswith(PREFIX + "."):  # fmt: skip
        raise AssumptionError(f"evidence {evidence_id} is not a valuation input")
    try:
        out = ledger.withdraw(session, evidence_id, reason, by=by)
    except LedgerError as exc:
        raise AssumptionError(str(exc)) from exc
    _retire_dependents(session, evidence_id, f"{record.metric} was withdrawn", None)
    return out


def _describe(record: EvidenceRecord, today: date) -> dict:
    d = record.details or {}
    name = d.get("input") or record.metric.split(".", 1)[1]
    spec = SPECS.get(name)
    as_of = date.fromisoformat(d["as_of"]) if d.get("as_of") else record.period_end
    age = (today - as_of).days if as_of else None
    limit = spec.max_age_days if spec else None
    return {
        "evidence_id": record.id,
        "input": name,
        "label": spec.label if spec else name,
        "scenario": d.get("scenario"),
        "value": str(record.value.normalize()) if record.value is not None else None,
        "unit": record.unit,
        "class": d.get("class") or USER,
        "source": d.get("source"),
        "source_url": d.get("source_url"),
        "source_backed": bool(d.get("source")),
        "as_of": as_of.isoformat() if as_of else None,
        "entered_on": d.get("entered_on"),
        "author": record.created_by,
        "rationale": d.get("rationale"),
        "age_days": age,
        "max_age_days": limit,
        "stale": bool(limit is not None and age is not None and age > limit),
        "market_input_without_source": bool(spec and spec.market and not d.get("source")),
        "status": record.status.value,
        "status_reason": record.status_reason,
        "supersedes": d.get("supersedes"),
    }


def current_inputs(
    session: Session, company_id: uuid.UUID, today: date | None = None
) -> dict[str, dict]:
    """The active valuation inputs of a company, keyed by metric (e.g. 'VALUATION_INPUT.beta')."""
    today = today or date.today()
    rows = session.scalars(
        select(EvidenceRecord)
        .where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.evidence_class == EvidenceClass.ASSUMPTION,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.metric.like(f"{PREFIX}.%"),
        )
        .order_by(EvidenceRecord.id)
    )
    return {r.metric: _describe(r, today) for r in rows}  # the latest active record of a key wins


def history(
    session: Session,
    company_id: uuid.UUID,
    name: str,
    scenario: str | None = None,
    today: date | None = None,
) -> list[dict]:
    """Every record ever made for one input, newest first, with who changed it and why."""
    metric = metric_for(name, scenario)
    today = today or date.today()
    rows = session.scalars(
        select(EvidenceRecord)
        .where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.metric == metric,
            EvidenceRecord.evidence_class == EvidenceClass.ASSUMPTION,
        )
        .order_by(EvidenceRecord.id.desc())  # fmt: skip
    )
    return [_describe(r, today) for r in rows]


# ---------------------------------------------------------------- derived: CAPM and the scenario sets


def capm_cost_of_equity(
    session: Session, company_id: uuid.UUID, inputs: dict[str, dict], now: datetime | None = None
) -> dict:
    """Cost of equity = risk-free rate + beta x equity risk premium, only from entered inputs.

    Registered as a calculation linked to the three inputs, so it is traceable and recomputed when one is
    edited. Missing inputs are named; nothing is defaulted.
    """
    keys = {
        "rf": f"{PREFIX}.risk_free_rate",
        "erp": f"{PREFIX}.equity_risk_premium",
        "beta": f"{PREFIX}.beta",
    }
    missing = [SPECS[k.split(".", 1)[1]].label for k in keys.values() if k not in inputs]
    if missing:
        return {"completed": False, "missing": missing, "reason": "needs " + ", ".join(missing).lower() + ": none is assumed"}  # fmt: skip
    ids = {alias: inputs[k]["evidence_id"] for alias, k in keys.items()}
    record = ledger.register_calculation(
        session, company_id, "VALUATION.capm_cost_of_equity", "rf + beta * erp", ids,
        created_by="valuation-engine", result_unit="percent", spans_periods=True, now=now,
    )  # fmt: skip
    used = [inputs[k] for k in keys.values()]
    return {
        "completed": True,
        "value": str(record.value.normalize()),
        "unit": "percent",
        "evidence_id": record.id,
        "formula": "risk-free rate + beta x equity risk premium",
        "input_ids": list(ids.values()),
        "stale_inputs": [u["label"] for u in used if u["stale"]],
        "not_source_backed": [u["label"] for u in used if not u["source_backed"]],
    }


def scenario_sets(inputs: dict[str, dict], capm: dict) -> tuple[dict, dict]:
    """Per scenario: the cost of equity, growth and ROE shift entered for it, or exactly what is missing.

    The base cost of equity may come from CAPM when no base cost of equity was entered directly; every other
    figure must be entered. Returns (complete scenarios, {scenario: [missing labels]}).
    """
    complete: dict = {}
    missing: dict = {}
    for s in SCENARIOS:
        parts = {}
        lacking = []
        for name in ("cost_of_equity", "terminal_growth", "roe_shift"):
            item = inputs.get(f"{PREFIX}.{name}_{s}")
            if item is None and name == "cost_of_equity" and s == "base" and capm.get("completed"):
                parts[name] = {"value": capm["value"], "evidence_id": capm["evidence_id"], "label": "Cost of equity (CAPM from the entered risk-free rate, beta and premium)", "class": "DERIVED_OUTPUT", "stale": bool(capm["stale_inputs"]), "input_ids": capm["input_ids"]}  # fmt: skip
            elif item is None:
                lacking.append(f"{SPECS[name].label.lower()} for the {s} scenario")
            else:
                parts[name] = item
        if lacking:
            missing[s] = lacking
        else:
            complete[s] = {
                "cost_of_equity": Decimal(str(parts["cost_of_equity"]["value"])),
                "growth": Decimal(str(parts["terminal_growth"]["value"])),
                "roe_shift": Decimal(str(parts["roe_shift"]["value"])),
                "inputs": parts,
                "input_ids": sorted(
                    {parts[n]["evidence_id"] for n in parts}
                    | {i for n in parts for i in parts[n].get("input_ids", [])}
                ),
                "flags": sorted(
                    {f"{parts[n]['label']} is stale" for n in parts if parts[n].get("stale")}
                    | {
                        f"{parts[n]['label']} is not source-backed"
                        for n in parts
                        if parts[n].get("market_input_without_source")
                    }
                ),  # fmt: skip
            }
    return complete, missing


def ledger_view(inputs: dict[str, dict], capm: dict) -> dict:
    """The inputs grouped by kind for the report, each with its provenance."""
    sourced = [v for v in inputs.values() if v["class"] == SOURCED]
    user = [v for v in inputs.values() if v["class"] == USER]
    return {
        "sourced_inputs": sorted(sourced, key=lambda v: (v["input"], v["scenario"] or "")),
        "user_assumptions": sorted(user, key=lambda v: (v["input"], v["scenario"] or "")),
        "derived": [capm] if capm.get("completed") else [],
        "capm": capm,
        "counts": {"sourced": len(sourced), "user": len(user)},
        "market_inputs_missing": [
            SPECS[n].label
            for n in ("risk_free_rate", "equity_risk_premium", "beta")
            if f"{PREFIX}.{n}" not in inputs
        ],  # fmt: skip
        "flags": sorted(
            {
                f"{v['label']}{' (' + v['scenario'] + ')' if v['scenario'] else ''} is stale: as of {v['as_of']}, older than {v['max_age_days']} days"
                for v in inputs.values()
                if v["stale"]
            }
            | {
                f"{v['label']} is a market input without a source (recorded as the author's own assumption)"
                for v in inputs.values()
                if v["market_input_without_source"]
            }  # fmt: skip
        ),
    }


def lineage_ids(session: Session, evidence_id: int) -> list[int]:
    """Evidence ids a derived record rests on (direct links)."""
    return list(
        session.scalars(
            select(EvidenceLink.input_id).where(EvidenceLink.evidence_id == evidence_id)
        )
    )
