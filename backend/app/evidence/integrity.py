"""Read-only evidence-integrity audit.

Looks for places where the evidence store no longer agrees with what it was built from, or where
data that was retrieved never reached it. It changes nothing: no row is written, withdrawn or
repaired. A finding is a question for a person or for the `evidence` stage to resolve.

Checks (severity in brackets):
  DUPLICATE_ACTIVE      more than one ACTIVE version of the same source record [ERROR]
  ORPHAN_FINANCIAL      ACTIVE evidence whose provider fact no longer exists [ERROR]
  VALUE_MISMATCH        ACTIVE evidence whose original value or unit differs from its source
                        fact [ERROR]
  SOURCE_SUPERSEDED     document evidence whose extraction was replaced and promotes nothing
                        now [ERROR]
  MISMATCHED_DOCUMENT   evidence drawn from a document filed by another issuer [ERROR]
  MISSING_BASIS         statement evidence with no accounting basis [ERROR]
  UNIT_CONFLICT         one metric, period and basis stored in different units [ERROR]
  STALE_CALCULATION     a calculation whose input evidence is no longer ACTIVE [ERROR]
  UNSYNCED_PROVIDER     provider facts stored but no statement evidence built [WARNING]
  UNSYNCED_EXTRACTION   extracted figures promoted but no evidence built from the file [WARNING]
  FUTURE_PERIOD         a reported figure for a period that has not ended [WARNING]
  ZERO_TOTAL            revenue, assets or equity of exactly zero (missing is not zero) [WARNING]
  MISSING_FILE          a stored document whose file is not on disk [WARNING]
  OPEN_CONFLICT         sources of equal authority disagree; no value is chosen [INFO]
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from app.evidence.sync import T_DISCLOSURE, T_FINANCIAL, _financial_key
from app.models import (
    Company,
    Document,
    DocumentExtraction,
    EvidenceClass,
    EvidenceLink,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    ExtractedFact,
    FinancialFact,
)
from app.models.evidence import ResolutionStatus

ERROR, WARNING, INFO = "ERROR", "WARNING", "INFO"
EXAMPLES = 8
ACTIVE = EvidenceStatus.ACTIVE


@dataclass
class Check:
    code: str
    title: str
    severity: str
    count: int = 0
    companies: int = 0
    examples: list[dict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.count == 0


@dataclass
class IntegrityReport:
    generated_at: str
    scope: str
    evidence_rows: int
    checks: list[Check]

    @property
    def errors(self) -> int:
        return sum(c.count for c in self.checks if c.severity == ERROR)

    @property
    def warnings(self) -> int:
        return sum(c.count for c in self.checks if c.severity == WARNING)

    @property
    def status(self) -> str:
        if self.errors:
            return "FAIL"
        return "WARN" if self.warnings else "PASS"

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "scope": self.scope,
            "status": self.status,
            "errors": self.errors,
            "warnings": self.warnings,
            "evidence_rows": self.evidence_rows,
            "checks": [asdict(c) | {"clean": c.clean} for c in self.checks],
        }


def _scope(company_id: uuid.UUID | None):
    return EvidenceRecord.company_id == company_id if company_id else True


def _check(code, title, severity, rows, *, key=None, describe=None) -> Check:
    """Summarise offending rows: how many, in how many companies, and a few examples."""
    rows = list(rows)
    companies = {key(r) for r in rows} if key else set()
    out = Check(code, title, severity, count=len(rows), companies=len(companies))
    for r in rows[:EXAMPLES]:
        out.examples.append(describe(r) if describe else {"row": str(r)})
    return out


def _ev(r: EvidenceRecord) -> dict:
    return {
        "evidence_id": r.id,
        "company_id": str(r.company_id),
        "metric": r.metric,
        "period_end": str(r.period_end) if r.period_end else None,
        "basis": r.basis,
        "unit": r.unit,
        "value": str(r.value) if r.value is not None else None,
    }


def audit(
    session: Session,
    *,
    company_id: uuid.UUID | None = None,
    data_dir: Path | None = None,
    today=None,
) -> IntegrityReport:
    today = today or datetime.now(UTC).date()
    scope = "one company" if company_id else "all companies"
    total = session.scalar(
        select(func.count()).select_from(EvidenceRecord).where(_scope(company_id))
    )
    checks: list[Check] = []
    active = [EvidenceRecord.status == ACTIVE, _scope(company_id)]

    # 1 duplicates -------------------------------------------------------------------------------
    dup = session.execute(
        select(
            EvidenceRecord.company_id,
            EvidenceRecord.source_table,
            EvidenceRecord.source_id,
            func.count(),
        )
        .where(*active, EvidenceRecord.source_table.is_not(None))
        .group_by(EvidenceRecord.company_id, EvidenceRecord.source_table, EvidenceRecord.source_id)
        .having(func.count() > 1)
    ).all()
    checks.append(
        _check(
            "DUPLICATE_ACTIVE",
            "More than one ACTIVE version of the same source record",
            ERROR,
            dup,
            key=lambda r: r[0],
            describe=lambda r: {
                "company_id": str(r[0]),
                "source": f"{r[1]}:{str(r[2])[:80]}",
                "active_versions": r[3],
            },
        )
    )

    # 2 + 3 provider-fact traceability ----------------------------------------------------------
    fact_q = select(FinancialFact)
    if company_id:
        fact_q = fact_q.where(FinancialFact.company_id == company_id)
    # the natural key is unique within a company only, so the company is part of the lookup
    facts = {(f.company_id, _financial_key(f)): f for f in session.scalars(fact_q)}
    fin_evidence = list(
        session.scalars(
            select(EvidenceRecord).where(*active, EvidenceRecord.source_table == T_FINANCIAL)
        )
    )
    orphans = [e for e in fin_evidence if (e.company_id, e.source_id) not in facts]
    checks.append(
        _check(
            "ORPHAN_FINANCIAL",
            "ACTIVE evidence whose provider fact no longer exists",
            ERROR,
            orphans,
            key=lambda e: e.company_id,
            describe=_ev,
        )
    )
    mismatched = []
    for e in fin_evidence:
        f = facts.get((e.company_id, e.source_id))
        if f is None or e.evidence_class is not EvidenceClass.FACT:
            continue
        if e.original_value is not None and e.original_value != f.original_value:
            mismatched.append(e)
        elif e.original_unit and e.original_unit != f.original_unit:
            mismatched.append(e)
    checks.append(
        _check(
            "VALUE_MISMATCH",
            "ACTIVE evidence whose original value or unit differs from its source fact",
            ERROR,
            mismatched,
            key=lambda e: e.company_id,
            describe=_ev,
        )
    )

    # 4 document-sourced evidence ----------------------------------------------------------------
    live_extraction = exists().where(
        ExtractedFact.document_id == EvidenceRecord.document_id,
        ExtractedFact.promoted.is_(True),
        ExtractedFact.extraction_id == DocumentExtraction.id,
        DocumentExtraction.superseded_at.is_(None),
    )
    superseded = session.scalars(
        select(EvidenceRecord).where(
            *active,
            EvidenceRecord.source_table == T_DISCLOSURE,
            EvidenceRecord.document_id.is_not(None),
            ~live_extraction,
        )
    ).all()
    checks.append(
        _check(
            "SOURCE_SUPERSEDED",
            "Document evidence whose extraction was replaced and promotes nothing now",
            ERROR,
            superseded,
            key=lambda e: e.company_id,
            describe=_ev,
        )
    )
    wrong_issuer = session.scalars(
        select(EvidenceRecord)
        .join(Document, Document.id == EvidenceRecord.document_id)
        .where(*active, Document.association_status == "MISMATCH")
    ).all()
    checks.append(
        _check(
            "MISMATCHED_DOCUMENT",
            "Evidence drawn from a document filed by another issuer",
            ERROR,
            wrong_issuer,
            key=lambda e: e.company_id,
            describe=_ev,
        )
    )

    # 5 basis and units --------------------------------------------------------------------------
    no_basis = session.scalars(
        select(EvidenceRecord).where(
            *active,
            EvidenceRecord.kind == "FINANCIAL_STATEMENT",
            EvidenceRecord.basis.is_(None),
        )
    ).all()
    checks.append(
        _check(
            "MISSING_BASIS",
            "Statement evidence with no accounting basis",
            ERROR,
            no_basis,
            key=lambda e: e.company_id,
            describe=_ev,
        )
    )
    unit_conflict = session.execute(
        select(
            EvidenceRecord.company_id,
            EvidenceRecord.metric,
            EvidenceRecord.period_end,
            EvidenceRecord.basis,
            func.count(func.distinct(EvidenceRecord.unit)),
        )
        .where(
            *active,
            EvidenceRecord.evidence_class == EvidenceClass.FACT,
            EvidenceRecord.value.is_not(None),
        )
        .group_by(
            EvidenceRecord.company_id,
            EvidenceRecord.metric,
            EvidenceRecord.dimension,
            EvidenceRecord.period_end,
            EvidenceRecord.period_type,
            EvidenceRecord.basis,
        )
        .having(func.count(func.distinct(EvidenceRecord.unit)) > 1)
    ).all()
    checks.append(
        _check(
            "UNIT_CONFLICT",
            "One metric, period and basis stored in more than one unit",
            ERROR,
            unit_conflict,
            key=lambda r: r[0],
            describe=lambda r: {
                "company_id": str(r[0]),
                "metric": r[1],
                "period_end": str(r[2]),
                "basis": r[3],
                "units": r[4],
            },
        )
    )

    # 6 calculations -----------------------------------------------------------------------------
    inp = EvidenceRecord.__table__.alias("input")
    stale = session.scalars(
        select(EvidenceRecord)
        .where(
            *active,
            EvidenceRecord.evidence_class == EvidenceClass.CALCULATION,
            exists().where(
                EvidenceLink.evidence_id == EvidenceRecord.id,
                EvidenceLink.input_id == inp.c.id,
                inp.c.status != ACTIVE.value,
            ),
        )
        .distinct()
    ).all()
    checks.append(
        _check(
            "STALE_CALCULATION",
            "A calculation whose input evidence is no longer ACTIVE",
            ERROR,
            stale,
            key=lambda e: e.company_id,
            describe=_ev,
        )
    )

    # 7 data that never arrived -----------------------------------------------------------------
    has_evidence = exists().where(
        EvidenceRecord.company_id == FinancialFact.company_id,
        EvidenceRecord.source_table == T_FINANCIAL,
        EvidenceRecord.status == ACTIVE,
    )
    unsynced = session.execute(
        select(FinancialFact.company_id, func.count())
        .where(~has_evidence, *([FinancialFact.company_id == company_id] if company_id else []))
        .group_by(FinancialFact.company_id)
    ).all()
    checks.append(
        _check(
            "UNSYNCED_PROVIDER",
            "Provider facts stored but no statement evidence built",
            WARNING,
            unsynced,
            key=lambda r: r[0],
            describe=lambda r: {"company_id": str(r[0]), "facts": r[1]},
        )
    )
    has_doc_evidence = exists().where(
        EvidenceRecord.document_id == Document.id, EvidenceRecord.status == ACTIVE
    )
    promoted_doc = (
        select(Document.id, Document.company_id, func.sum(DocumentExtraction.facts_promoted))
        .join(DocumentExtraction, DocumentExtraction.document_id == Document.id)
        .where(
            DocumentExtraction.superseded_at.is_(None),
            DocumentExtraction.facts_promoted > 0,
            Document.association_status != "MISMATCH",
            Document.company_id.is_not(None),
            ~has_doc_evidence,
            *([Document.company_id == company_id] if company_id else []),
        )
        .group_by(Document.id, Document.company_id)
    )
    unsynced_docs = session.execute(promoted_doc).all()
    checks.append(
        _check(
            "UNSYNCED_EXTRACTION",
            "Extracted figures promoted but no evidence built from the file",
            WARNING,
            unsynced_docs,
            key=lambda r: r[1],
            describe=lambda r: {
                "document_id": str(r[0]),
                "company_id": str(r[1]),
                "facts_promoted": int(r[2] or 0),
            },
        )
    )

    # 8 plausibility -----------------------------------------------------------------------------
    future = session.scalars(
        select(EvidenceRecord).where(
            *active,
            EvidenceRecord.evidence_class == EvidenceClass.FACT,
            EvidenceRecord.kind == "FINANCIAL_STATEMENT",
            EvidenceRecord.period_end > today,
        )
    ).all()
    checks.append(
        _check(
            "FUTURE_PERIOD",
            "A reported figure for a period that has not ended",
            WARNING,
            future,
            key=lambda e: e.company_id,
            describe=_ev,
        )
    )
    zero = session.scalars(
        select(EvidenceRecord).where(
            *active,
            EvidenceRecord.evidence_class == EvidenceClass.FACT,
            EvidenceRecord.value == 0,
            and_(
                EvidenceRecord.metric.in_(
                    ["INCOME.revenue", "BALANCE_SHEET.total_assets", "BALANCE_SHEET.total_equity"]
                )
            ),
        )
    ).all()
    checks.append(
        _check(
            "ZERO_TOTAL",
            "Revenue, total assets or total equity of exactly zero (missing is not zero)",
            WARNING,
            zero,
            key=lambda e: e.company_id,
            describe=_ev,
        )
    )

    # 9 files ------------------------------------------------------------------------------------
    if data_dir is not None:
        docs_q = select(Document.id, Document.company_id, Document.storage_path)
        if company_id:
            docs_q = docs_q.where(Document.company_id == company_id)
        missing = [r for r in session.execute(docs_q).all() if not (Path(data_dir) / r[2]).exists()]
        checks.append(
            _check(
                "MISSING_FILE",
                "A stored document whose file is not on disk",
                WARNING,
                missing,
                key=lambda r: r[1],
                describe=lambda r: {"document_id": str(r[0]), "path": r[2]},
            )
        )

    # 10 open conflicts (informational) ----------------------------------------------------------
    conflicts = session.execute(
        select(
            EvidenceResolution.company_id, EvidenceResolution.metric, EvidenceResolution.period_end
        ).where(
            EvidenceResolution.status == ResolutionStatus.CONFLICT,
            *([EvidenceResolution.company_id == company_id] if company_id else []),
        )
    ).all()
    checks.append(
        _check(
            "OPEN_CONFLICT",
            "Sources of equal authority disagree; no value is chosen",
            INFO,
            conflicts,
            key=lambda r: r[0],
            describe=lambda r: {"company_id": str(r[0]), "metric": r[1], "period_end": str(r[2])},
        )
    )
    return IntegrityReport(datetime.now(UTC).isoformat(), scope, total or 0, checks)


def names(session: Session, report: IntegrityReport) -> IntegrityReport:
    """Add company names to the examples (display only)."""
    ids = {e["company_id"] for c in report.checks for e in c.examples if "company_id" in e}
    if not ids:
        return report
    found = {
        str(c.id): c.common_name
        for c in session.scalars(select(Company).where(Company.id.in_([uuid.UUID(i) for i in ids])))
    }
    for c in report.checks:
        for e in c.examples:
            if "company_id" in e:
                e["company"] = found.get(e["company_id"], "?")
    return report
