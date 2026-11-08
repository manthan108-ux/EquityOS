"""Lineage: what a piece of evidence was built from, what was built from it, and whether those
dependencies still hold.

`lineage_findings` re-checks every active calculation and interpretation against the CURRENT state
of its inputs. A calculation is re-computed from its inputs and compared with its stored value, so a
value that was edited, or an input that was superseded after the calculation was made, is caught.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.claims import CitedValue, check_figures
from app.evidence.expression import ExpressionError, Quantity, evaluate
from app.evidence.rules import Finding
from app.models import (
    EvidenceClass,
    EvidenceLink,
    EvidenceRecord,
    EvidenceStatus,
    LinkRole,
    Severity,
)

RECOMPUTE_TOLERANCE = Decimal("0.0001")  # absolute, in the value's own unit


def lineage_findings(session: Session, company_id: uuid.UUID) -> list[Finding]:
    records = {
        r.id: r
        for r in session.scalars(
            select(EvidenceRecord).where(EvidenceRecord.company_id == company_id)
        )
    }
    links: dict[int, list[EvidenceLink]] = {}
    for link in session.scalars(
        select(EvidenceLink)
        .join(EvidenceRecord, EvidenceRecord.id == EvidenceLink.evidence_id)
        .where(EvidenceRecord.company_id == company_id)
    ):
        links.setdefault(link.evidence_id, []).append(link)

    out: list[Finding] = []
    for record in records.values():
        if record.status is not EvidenceStatus.ACTIVE:
            continue
        mine = links.get(record.id, [])
        if record.evidence_class is EvidenceClass.CALCULATION:
            if (record.details or {}).get("method"):
                out.extend(_check_method_calculation(session, record))
            else:
                out.extend(_check_calculation(record, mine, records))
        elif record.evidence_class is EvidenceClass.INTERPRETATION:
            out.extend(_check_interpretation(record, mine, records))
    return out


def _check_method_calculation(session: Session, record: EvidenceRecord) -> list[Finding]:
    """Calculations that run an algorithm over stored rows (technical indicators) are recomputed
    by the method that made them; they have no per-row links."""
    from app.calc.technical import InsufficientHistory
    from app.calc.technical_store import METHOD, recompute

    where = dict(metric=record.metric, period_end=record.period_end, blocks=[record.id])
    details = record.details or {}
    if details.get("method") in ("peer.v1", "score.v1"):
        return _check_sector_calculation(session, record, where)
    if details.get("method") != METHOD:
        return [
            Finding(
                "CALC_NOT_REPRODUCIBLE",
                Severity.ERROR,
                f"{record.metric}: unknown calculation method {details.get('method')!r}",
                evidence_ids=[record.id],
                **where,
            )
        ]
    try:
        value, digest = recompute(session, record)
    except (InsufficientHistory, KeyError, ValueError) as exc:
        return [
            Finding(
                "CALC_NOT_REPRODUCIBLE",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: cannot be recomputed from the stored "
                f"prices ({exc})",
                evidence_ids=[record.id],
                **where,
            )
        ]
    out = []
    if digest != details.get("price_digest"):
        out.append(
            Finding(
                "CALC_INPUT_NOT_CURRENT",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: the prices or adjustment events it used "
                "have changed since it was calculated; recalculate",
                evidence_ids=[record.id],
                **where,
            )
        )
    if record.value is None or abs(value - record.value) > RECOMPUTE_TOLERANCE:
        out.append(
            Finding(
                "CALC_RECOMPUTE_MISMATCH",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: stored {record.value} but the stored "
                f"prices give {value}",
                evidence_ids=[record.id],
                details={"stored": str(record.value), "recomputed": str(value)},
                **where,
            )
        )
    return out


def _check_sector_calculation(
    session: Session, record: EvidenceRecord, where: dict
) -> list[Finding]:
    """Peer statistics and sector scores: recomputed from the evidence they are linked to."""
    from app.sector.store import recompute

    value, problems = recompute(session, record)
    if problems:
        return [
            Finding(
                "CALC_INPUT_NOT_CURRENT",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: {'; '.join(problems)}",
                evidence_ids=[record.id],
                **where,
            )
        ]
    if record.value is None or abs(value - record.value) > RECOMPUTE_TOLERANCE:
        return [
            Finding(
                "CALC_RECOMPUTE_MISMATCH",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: stored {record.value} but its inputs "
                f"give {value}",
                evidence_ids=[record.id],
                details={"stored": str(record.value), "recomputed": str(value)},
                **where,
            )
        ]
    return []


def _check_calculation(
    record: EvidenceRecord, links: list[EvidenceLink], records: dict[int, EvidenceRecord]
) -> list[Finding]:
    out: list[Finding] = []
    where = dict(metric=record.metric, period_end=record.period_end, blocks=[record.id])
    missing = (record.details or {}).get("missing_inputs")
    if missing:
        out.append(
            Finding(
                "CALC_INPUT_MISSING",
                Severity.WARNING,
                f"{record.metric} {record.period_end}: inputs {', '.join(missing)} could not be "
                "linked to evidence, so this calculation cannot be re-checked",
                evidence_ids=[record.id],
                **where,
            )
        )
        return out
    if not links:
        out.append(
            Finding(
                "CALC_NO_INPUTS",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: a calculation with no recorded inputs",
                evidence_ids=[record.id],
                **where,
            )
        )
        return out
    inputs = {link.alias: records.get(link.input_id) for link in links}
    stale = [
        (alias, r)
        for alias, r in inputs.items()
        if r is None or r.status is not EvidenceStatus.ACTIVE
    ]
    if stale:
        names = ", ".join(
            f"{a} (evidence {r.id if r else '?'}, {r.status.value if r else 'gone'})"
            for a, r in stale
        )
        out.append(
            Finding(
                "CALC_INPUT_NOT_CURRENT",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: built on input(s) that are no longer "
                f"current: {names}; recalculate from the current evidence",
                evidence_ids=[record.id, *(r.id for _, r in stale if r)],
                **where,
            )
        )
        return out
    measured = [r for r in inputs.values() if r.evidence_class is not EvidenceClass.ASSUMPTION]
    bases = {r.basis for r in measured if r.basis}
    types = {r.period_type for r in measured if r.period_type}
    if len(bases) > 1:
        out.append(
            Finding(
                "CALC_MIXED_BASIS",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: combines {' and '.join(sorted(bases))}",
                evidence_ids=[record.id, *(r.id for r in measured)],
                **where,
            )
        )
    if len(types) > 1 and not (record.details or {}).get("mixed_period_types"):
        out.append(
            Finding(
                "CALC_MIXED_PERIOD",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: combines {' and '.join(sorted(types))} "
                "figures",
                evidence_ids=[record.id, *(r.id for r in measured)],
                **where,
            )
        )
    try:
        variables = {
            alias: Quantity(r.value, r.unit)
            for alias, r in inputs.items()
            if r.value is not None and r.unit is not None
        }
        again = evaluate(record.expression or "", variables)
    except ExpressionError as exc:
        out.append(
            Finding(
                "CALC_NOT_REPRODUCIBLE",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: cannot be recomputed ({exc})",
                evidence_ids=[record.id],
                **where,
            )
        )
        return out
    if record.value is None or abs(again.value - record.value) > RECOMPUTE_TOLERANCE:
        out.append(
            Finding(
                "CALC_RECOMPUTE_MISMATCH",
                Severity.ERROR,
                f"{record.metric} {record.period_end}: stored {record.value} but its inputs give "
                f"{again.value}",
                evidence_ids=[record.id, *(r.id for r in inputs.values())],
                details={"stored": str(record.value), "recomputed": str(again.value)},
                **where,
            )
        )
    return out


def _check_interpretation(
    record: EvidenceRecord, links: list[EvidenceLink], records: dict[int, EvidenceRecord]
) -> list[Finding]:
    out: list[Finding] = []
    where = dict(metric=record.metric, period_end=record.period_end, blocks=[record.id])
    cited = [records.get(link.input_id) for link in links if link.role is LinkRole.CITES]
    if not cited:
        return [
            Finding(
                "CLAIM_NO_EVIDENCE",
                Severity.ERROR,
                f"{record.metric}: a statement that cites no evidence",
                evidence_ids=[record.id],
                **where,
            )
        ]
    stale = [r for r in cited if r is None or r.status is not EvidenceStatus.ACTIVE]
    if stale:
        out.append(
            Finding(
                "CLAIM_CITES_STALE",
                Severity.ERROR,
                f"{record.metric}: cites evidence that is no longer current "
                f"({', '.join(str(r.id) for r in stale if r)}); the statement must be re-derived",
                evidence_ids=[record.id, *(r.id for r in stale if r)],
                **where,
            )
        )
    live = [r for r in cited if r is not None]
    check = check_figures(
        record.statement_text or "",
        [CitedValue(r.id, r.value, r.unit) for r in live if r.value is not None and r.unit],
    )
    if not check.ok:
        out.append(
            Finding(
                "CLAIM_UNTRACED_NUMBER",
                Severity.ERROR,
                f"{record.metric}: numbers not found in the cited evidence: "
                f"{', '.join(repr(f.text) for f in check.untraced)}",
                evidence_ids=[record.id, *(r.id for r in live)],
                **where,
            )
        )
    return out


# ---- reading lineage -----------------------------------------------------------------------------


@dataclass
class Node:
    evidence_id: int
    evidence_class: str
    metric: str
    value: str | None
    unit: str | None
    period_end: str | None
    status: str
    provider: str | None
    document_id: str | None
    page: int | None
    alias: str | None
    role: str | None
    inputs: list["Node"]


def lineage_tree(session: Session, evidence_id: int, *, max_depth: int = 12) -> Node | None:
    """The record and, recursively, everything it was built from."""
    root = session.get(EvidenceRecord, evidence_id)
    if root is None:
        return None

    def build(
        record: EvidenceRecord, alias: str | None, role: str | None, depth: int, path: frozenset
    ) -> Node:
        children: list[Node] = []
        if depth < max_depth:
            for link in session.scalars(
                select(EvidenceLink).where(EvidenceLink.evidence_id == record.id)
            ):
                child = session.get(EvidenceRecord, link.input_id)
                if child is not None and child.id not in path:  # a cycle can never loop forever
                    children.append(
                        build(
                            child, link.alias or None, link.role.value, depth + 1, path | {child.id}
                        )
                    )
        return Node(
            evidence_id=record.id,
            evidence_class=record.evidence_class.value,
            metric=record.metric,
            value=str(record.value) if record.value is not None else None,
            unit=record.unit,
            period_end=str(record.period_end) if record.period_end else None,
            status=record.status.value,
            provider=record.provider_code,
            document_id=str(record.document_id) if record.document_id else None,
            page=record.source_page,
            alias=alias,
            role=role,
            inputs=children,
        )

    return build(root, None, None, 0, frozenset({root.id}))


def dependents(session: Session, evidence_id: int) -> list[int]:
    """Evidence built directly on this record."""
    return list(
        session.scalars(
            select(EvidenceLink.evidence_id).where(EvidenceLink.input_id == evidence_id)
        )
    )


__all__ = ["Node", "dependents", "lineage_findings", "lineage_tree"]
