"""The validation engine: sync -> resolve -> validate -> block -> record.

`validate_company` is the single entry point. After it runs, `evidence_resolutions` holds, for each
(metric, dimension, period, basis), the one value later phases may use together with its quality,
and `validation_runs` / `validation_findings` hold what was checked and found.

The engine only ever ADDS evidence rows and rewrites the derived resolution index. It never edits
the fact tables or documents it reads.
"""

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.evidence.core import Fact, Key, Resolved, grade, resolve_group
from app.evidence.lineage import lineage_findings
from app.evidence.rules import RULES_VERSION, Context, Finding, run_rules
from app.evidence.sync import SyncReport, sync_company
from app.models import (
    Document,
    DocumentExtraction,
    EvidenceClass,
    EvidenceLink,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    ExtractedFact,
    Quality,
    Severity,
    ValidationFinding,
    ValidationRun,
)

NUMERIC_CLASSES = (EvidenceClass.FACT, EvidenceClass.CALCULATION)


@dataclass
class ValidationReport:
    run: ValidationRun
    sync: SyncReport
    findings: list[Finding]
    resolved: dict[Key, Resolved] = field(default_factory=dict)
    blocked: int = 0


def _document_problem(document: Document | None, company_id: uuid.UUID) -> str | None:
    if document is None:
        return None
    if document.company_id != company_id:
        return "the source document is not linked to this company"
    if document.association_status == "MISMATCH":
        return "the source document's issuer check failed (it describes a different entity)"
    return None


def load_facts(session: Session, company_id: uuid.UUID) -> list[Fact]:
    records = list(
        session.scalars(
            select(EvidenceRecord).where(
                EvidenceRecord.company_id == company_id,
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
                EvidenceRecord.evidence_class.in_(NUMERIC_CLASSES),
                EvidenceRecord.period_end.is_not(None),
                EvidenceRecord.value.is_not(None),
            )
        )
    )
    document_ids = {r.document_id for r in records if r.document_id}
    documents = (
        {d.id: d for d in session.scalars(select(Document).where(Document.id.in_(document_ids)))}
        if document_ids
        else {}
    )
    facts = []
    for r in records:
        facts.append(
            Fact(
                id=r.id,
                cls=r.evidence_class.value,
                metric=r.metric,
                dimension=r.dimension or "",
                period_end=r.period_end,
                period_type=r.period_type or "",
                basis=r.basis or "",
                value=r.value,
                unit=r.unit or "",
                provider=r.provider_code or "",
                tier=r.authority_tier if r.authority_tier is not None else 6,
                original_unit=r.original_unit,
                document_id=r.document_id,
                page=r.source_page,
                source_url=r.source_url,
                retrieved_at=r.retrieved_at,
                details=dict(r.details or {}),
                problem=_document_problem(documents.get(r.document_id), company_id)
                if r.document_id
                else None,
            )
        )
    return facts


def _dependents(session: Session, company_id: uuid.UUID) -> dict[int, list[int]]:
    graph: dict[int, list[int]] = defaultdict(list)
    for evidence_id, input_id in session.execute(
        select(EvidenceLink.evidence_id, EvidenceLink.input_id)
        .join(EvidenceRecord, EvidenceRecord.id == EvidenceLink.evidence_id)
        .where(EvidenceRecord.company_id == company_id)
    ):
        graph[input_id].append(evidence_id)
    return graph


def _blocked_ids(
    findings: list[Finding], graph: dict[int, list[int]], selected_ids: set[int]
) -> dict[int, set[str]]:
    """Evidence that may not be used: involved in an ERROR finding, or built on such evidence."""
    blocked: dict[int, set[str]] = defaultdict(set)
    for f in findings:
        if f.severity is not Severity.ERROR:
            continue
        for evidence_id in f.blocking_ids():
            if evidence_id in selected_ids:
                blocked[evidence_id].add(f.rule)
    frontier = list(blocked)
    while frontier:
        current = frontier.pop()
        inherited = {r if r.startswith("INPUT_") else f"INPUT_{r}" for r in blocked[current]}
        for dependent in graph.get(current, []):
            if not inherited <= blocked[dependent]:
                blocked[dependent] |= inherited
                frontier.append(dependent)
    return blocked


_RANK = {Quality.LOW: 0, Quality.MEDIUM: 1, Quality.HIGH: 2}


def _limit_by_inputs(
    session: Session, company_id: uuid.UUID, resolved: dict[Key, Resolved]
) -> None:
    """A calculation is no better than its weakest input (an assumption caps it at MEDIUM)."""
    inputs_of: dict[int, list[int]] = defaultdict(list)
    for evidence_id, input_id in session.execute(
        select(EvidenceLink.evidence_id, EvidenceLink.input_id)
        .join(EvidenceRecord, EvidenceRecord.id == EvidenceLink.evidence_id)
        .where(EvidenceRecord.company_id == company_id)
    ):
        inputs_of[evidence_id].append(input_id)
    by_id = {r.selected.id: r for r in resolved.values() if r.selected is not None}
    done: set[int] = set()

    def settle(evidence_id: int, trail: frozenset) -> Quality | None:
        r = by_id.get(evidence_id)
        if r is None:
            return None
        if evidence_id not in done and evidence_id not in trail:
            for input_id in inputs_of.get(evidence_id, []):
                if r.selected.cls != "CALCULATION":
                    continue
                q = settle(input_id, trail | {evidence_id})
                if q is None:
                    q = Quality.MEDIUM  # an assumption or text: not independently verified
                    reason = f"built on evidence {input_id} that is not a verified fact"
                else:
                    reason = f"limited by input evidence {input_id} ({q.value})"
                if _RANK[q] < _RANK[r.quality]:
                    r.quality = q
                    r.quality_reasons = [*r.quality_reasons, reason]
            done.add(evidence_id)
        return r.quality

    for evidence_id in list(by_id):
        settle(evidence_id, frozenset())


def _review_findings(session: Session, company_id: uuid.UUID, sync: SyncReport) -> list[Finding]:
    out: list[Finding] = []
    pending = session.scalar(
        select(func.count())
        .select_from(ExtractedFact)
        .join(DocumentExtraction, DocumentExtraction.id == ExtractedFact.extraction_id)
        .join(Document, Document.id == ExtractedFact.document_id)
        .where(
            Document.company_id == company_id,
            Document.association_status != "MISMATCH",
            DocumentExtraction.superseded_at.is_(None),
            ExtractedFact.needs_review.is_(True),
        )
    )
    if pending:
        out.append(
            Finding(
                "NEEDS_REVIEW_PENDING",
                Severity.INFO,
                f"{pending} value(s) read from scanned pages were withheld pending a person's "
                "review; they are not in the evidence store",
                details={"count": pending},
            )
        )
    if sync.skipped:
        out.append(
            Finding(
                "SOURCE_ROWS_NOT_USED",
                Severity.INFO,
                f"{len(sync.skipped)} stored row(s) were not turned into evidence (unit not "
                "convertible or no period)",
                details={"examples": sync.skipped[:10], "count": len(sync.skipped)},
            )
        )
    return out


def validate_company(
    session: Session,
    company_id: uuid.UUID,
    *,
    today: date | None = None,
    now: datetime | None = None,
    sync: bool = True,
) -> ValidationReport:
    now = now or datetime.now(UTC)
    today = today or now.date()
    sync_report = sync_company(session, company_id, now=now) if sync else SyncReport()

    facts = load_facts(session, company_id)
    groups: dict[Key, list[Fact]] = defaultdict(list)
    for f in facts:
        groups[f.key].append(f)
    resolved: dict[Key, Resolved] = {}
    for key, members in groups.items():
        r = resolve_group(members)
        r.quality, r.quality_reasons = grade(r)
        resolved[key] = r
    _limit_by_inputs(session, company_id, resolved)

    context = Context(resolved=resolved, facts=facts, today=today)
    findings = run_rules(context)
    findings += lineage_findings(session, company_id)
    findings += _review_findings(session, company_id, sync_report)

    selected_ids = {r.selected.id for r in resolved.values() if r.selected is not None}
    # Calculations and the facts they depend on may be chosen by resolution; every selected id is
    # a candidate for blocking.
    graph = _dependents(session, company_id)
    blocked = _blocked_ids(findings, graph, selected_ids | set(graph.keys()))
    # Interpretations built on blocked evidence are findings of their own.
    for evidence_id, rules in list(blocked.items()):
        for dependent in graph.get(evidence_id, []):
            record = session.get(EvidenceRecord, dependent)
            if (
                record is not None
                and record.status is EvidenceStatus.ACTIVE
                and record.evidence_class is EvidenceClass.INTERPRETATION
            ):
                findings.append(
                    Finding(
                        "CLAIM_CITES_BLOCKED",
                        Severity.ERROR,
                        f"{record.metric}: cites evidence {evidence_id} that is blocked "
                        f"({', '.join(sorted(rules))})",
                        evidence_ids=[record.id, evidence_id],
                    )
                )

    counts = Counter(f.severity for f in findings)
    run = ValidationRun(
        company_id=company_id,
        rules_version=RULES_VERSION,
        started_at=now,
        finished_at=datetime.now(UTC),
        as_of=today,
        evidence_checked=len(facts),
        errors=counts[Severity.ERROR],
        warnings=counts[Severity.WARNING],
        infos=counts[Severity.INFO],
        summary={
            "by_rule": dict(Counter(f"{f.severity.value}:{f.rule}" for f in findings)),
            "resolution": dict(Counter(r.status.value for r in resolved.values())),
            "quality": dict(Counter(r.quality.value for r in resolved.values())),
            "checks": dict(sorted(context.stats.items())),
            "sync": {
                "created": sync_report.created,
                "unchanged": sync_report.unchanged,
                "superseded": sync_report.superseded,
                "withdrawn": sync_report.withdrawn,
                "reactivated": sync_report.reactivated,
            },
        },
    )
    session.add(run)
    session.flush()
    for f in findings:
        session.add(
            ValidationFinding(
                run_id=run.id,
                company_id=company_id,
                rule_code=f.rule,
                severity=f.severity,
                message=f.message,
                metric=f.metric,
                period_end=f.period_end,
                evidence_ids=sorted(set(f.evidence_ids)) or None,
                details=f.details or None,
            )
        )
    _store_resolutions(session, company_id, resolved, blocked, run, now)
    session.flush()
    return ValidationReport(
        run=run,
        sync=sync_report,
        findings=findings,
        resolved=resolved,
        blocked=sum(1 for r in resolved.values() if r.selected and r.selected.id in blocked),
    )


def _store_resolutions(
    session: Session,
    company_id: uuid.UUID,
    resolved: dict[Key, Resolved],
    blocked: dict[int, set[str]],
    run: ValidationRun,
    now: datetime,
) -> None:
    """Rebuild the derived index. It holds no original data, so replacing it loses nothing."""
    session.execute(delete(EvidenceResolution).where(EvidenceResolution.company_id == company_id))
    for key, r in resolved.items():
        metric, dimension, period_end, period_type, basis = key
        selected_id = r.selected.id if r.selected else None
        reasons = sorted(blocked.get(selected_id, set())) if selected_id else []
        session.add(
            EvidenceResolution(
                company_id=company_id,
                metric=metric,
                dimension=dimension,
                period_end=period_end,
                period_type=period_type,
                basis=basis,
                selected_evidence_id=selected_id,
                status=r.status,
                quality=r.quality,
                quality_reasons=r.quality_reasons,
                alternatives=r.alternatives,
                max_difference_pct=r.max_difference_pct,
                blocked=bool(reasons),
                blocked_by=reasons or None,
                note=r.note,
                run_id=run.id,
                updated_at=now,
            )
        )


__all__ = ["ValidationReport", "load_facts", "validate_company"]
