"""Store and use a locally supplied XBRL instance: raw facts first, promotion only if it checks out.

Everything here works on bytes the person supplied. Nothing is downloaded.

1. `xbrl.parse` reads the file (refusing DTDs/entities, iXBRL and non-XBRL XML).
2. The identity of the filer (the entity identifier in its contexts) must match one of the company's
   own identifiers (NSE symbol, BSE scrip code, ISIN). A different identifier is refused; a missing
   or unmatched one leaves the filing UNVERIFIED and nothing is promoted unless the supplier
   confirms the issuer (`force_entity`, recorded as FORCED, like a PDF).
3. The instance is stored as a document (byte-exact, SHA-256, stored once), every fact is kept in
   `xbrl_facts` as filed, and the mapped statements go through the SAME checks as a PDF: arithmetic
   identities, stated basis, stated unit, period classification, conflicts with other documents.
4. Extra guard: an amount that differs from an already stored value for the same item and period by
   exactly a power of ten (1e3, 1e5, 1e6, 1e7) is a scale error, not new data; it is not promoted.

Values reach `financial_facts` (and from there the evidence ledger) only through `_Promoter`, so an
XBRL value is subject to the same cross-document conflict handling as any other.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.docintel import taxonomy, validate, xbrl, xbrl_map
from app.docintel.entity import EntityCheck
from app.docintel.pipeline import (
    PROVIDER_CODE,
    _latest_period,
    _Promoter,
    _unit,
    promotion_problem,
)
from app.models import (
    Company,
    DatasetVerification,
    Document,
    DocumentExtraction,
    ExtractedFact,
    ExtractedStatement,
    ExtractionStatus,
    FinancialFact,
    Security,
    ValidationStatus,
    XbrlFact,
    XbrlFiling,
)
from app.normalization import normalized_value
from app.providers.documents import store_document_bytes

READER_VERSION = "1.0"
_SCALES = (
    Decimal(10) ** 2,
    Decimal(10) ** 3,
    Decimal(10) ** 4,
    Decimal(10) ** 5,
    Decimal(10) ** 6,
    Decimal(10) ** 7,
)
_SCALE_TOLERANCE = Decimal("0.02")


class EntityMismatch(ValueError):
    """The filing's own entity identifier belongs to a different company."""


@dataclass
class IngestReport:
    document: Document
    filing: XbrlFiling
    extraction: DocumentExtraction
    mapping: xbrl_map.Mapping
    created: bool
    promoted: int = 0
    not_promoted: dict[str, int] = field(default_factory=dict)  # reason -> count
    issues: list[dict] = field(default_factory=list)


def company_identifiers(session: Session, company_id: uuid.UUID) -> set[str]:
    ids: set[str] = set()
    for sec in session.scalars(select(Security).where(Security.company_id == company_id)):
        for v in (sec.symbol, sec.security_code, sec.isin):
            if v:
                ids.add(v.strip().upper())
    return ids


def check_entity(session: Session, company_id: uuid.UUID, entity_ids: list[str]) -> tuple[str, str]:
    """('CONFIRMED' | 'MISMATCH' | 'UNVERIFIED', explanation) from the filing's own identifiers."""
    if not entity_ids:
        return "UNVERIFIED", "the filing carries no entity identifier"
    own = company_identifiers(session, company_id)
    seen = {e.strip().upper() for e in entity_ids}
    if seen & own:
        return "CONFIRMED", f"entity identifier {sorted(seen & own)[0]} matches the company"
    # an identifier of ANOTHER listed company is a refusal; an unknown one is only unverified
    other = session.scalar(
        select(Security).where(
            Security.company_id != company_id,
            (Security.symbol.in_(seen))
            | (Security.security_code.in_(seen))
            | (Security.isin.in_(seen)),
        )
    )
    if other is not None:
        return "MISMATCH", (
            f"the filing's entity identifier {sorted(seen)[0]} belongs to another company "
            f"({other.symbol})"
        )
    return "UNVERIFIED", f"entity identifier {sorted(seen)[0]} is not one of the company's"


def _scale_problem(
    session: Session, company_id: uuid.UUID, statement: str, key: str, column, basis, value_inr
) -> str | None:
    """Existing value for the same item/period/basis that differs by exactly a power of ten."""
    if value_inr is None or value_inr == 0 or column.period.period_end is None:
        return None
    rows = session.scalars(
        select(FinancialFact).where(
            FinancialFact.company_id == company_id,
            FinancialFact.provider_code == PROVIDER_CODE,
            FinancialFact.line_item == key,
            FinancialFact.period_end == column.period.period_end,
            FinancialFact.basis == basis,
        )
    ).all()
    for existing in rows:
        if existing.value_inr in (None, 0):
            continue
        ratio = abs(value_inr / existing.value_inr)
        for scale in _SCALES:
            for r in (scale, 1 / scale):
                if abs(ratio / r - 1) <= _SCALE_TOLERANCE:
                    return (
                        f"differs from the stored value ({existing.value_inr}) by a factor of "
                        f"{scale:,.0f}: a scale error, not new data"
                    )
    return None


def ingest(
    session: Session,
    company: Company,
    body: bytes,
    data_dir: Path,
    *,
    filename: str,
    family: str | None = None,
    basis: str | None = None,
    amount_scale: str | None = None,
    pure_means_fraction: bool | None = None,
    force_entity: bool = False,
    promote: bool = True,
) -> IngestReport:
    """Read, store and (if every check passes) promote one XBRL instance for `company`.

    Raises xbrl.XbrlError for a file that is not a readable instance and EntityMismatch for a
    filing of another company; in both cases nothing is stored.
    """
    inst = xbrl.parse(body)
    mapping = xbrl_map.build(
        inst,
        family=family,
        basis=basis,
        amount_scale=amount_scale,
        pure_means_fraction=pure_means_fraction,
    )
    status, why = check_entity(session, company.id, mapping.entity_ids)
    if status == "MISMATCH" and not force_entity:
        raise EntityMismatch(why)
    confirmed = status == "CONFIRMED" or force_entity
    document, created = store_document_bytes(
        session,
        data_dir,
        body,
        url=f"user-upload://{__import__('hashlib').sha256(body).hexdigest()[:16]}",
        doc_type="xbrl",
        source_code="MANUAL_UPLOAD",
        company_id=company.id,
        title=filename,
    )
    if document.company_id not in (None, company.id):
        raise EntityMismatch("this exact file is already stored against a different company")
    document.association_status = (
        "CONFIRMED" if status == "CONFIRMED" else "FORCED" if force_entity else "UNVERIFIED"
    )
    run = DocumentExtraction(
        document_id=document.id,
        extractor_version=f"xbrl-{READER_VERSION}",
        status=ExtractionStatus.FAILED,
        issues=[],
    )
    session.add(run)
    session.flush()
    report = _store(
        session,
        company,
        document,
        run,
        inst,
        mapping,
        confirmed,
        why,
        promote,
        created,
    )
    session.execute(
        update(DocumentExtraction)
        .where(
            DocumentExtraction.document_id == document.id,
            DocumentExtraction.id != run.id,
            DocumentExtraction.superseded_at.is_(None),
        )
        .values(superseded_at=datetime.now(UTC))
    )
    return report


def _store(
    session, company, document, run, inst, mapping, confirmed, entity_note, promote, created
) -> IngestReport:
    issues = list(mapping.issues)
    if inst.problems:
        issues.extend({"page": None, "code": "INSTANCE_DEFECT", "detail": p} for p in inst.problems)
    if not confirmed:
        issues.append({"page": None, "code": "ENTITY_UNCONFIRMED", "detail": entity_note})
    filing = XbrlFiling(
        document_id=document.id,
        extraction_id=run.id,
        reader_version=READER_VERSION,
        family=mapping.family,
        basis=mapping.basis,
        basis_source=mapping.basis_source,
        amount_unit=mapping.unit,
        unit_source=mapping.unit_source,
        entity_ids=mapping.entity_ids,
        entity_confirmed=confirmed,
        schema_refs=inst.schema_refs,
        namespaces=inst.namespaces,
        contexts=[c.as_json() for c in inst.contexts.values()],
        units=[
            {"id": u.id, "measures": list(u.measures), "denominators": list(u.denominators)}
            for u in inst.units.values()
        ],
        facts_total=len(inst.facts),
        facts_mapped=len(mapping.mapped),
        facts_dimensional=mapping.dimensional,
        unmapped_concepts=mapping.unmapped,
        problems=inst.problems,
        issues=issues,
    )
    session.add(filing)
    session.flush()
    for f in inst.facts:
        ctx = inst.context(f)
        unit = inst.unit(f)
        mapped = mapping.mapped.get(f.index)
        session.add(
            XbrlFact(
                filing_id=filing.id,
                ordinal=f.index,
                namespace=f.namespace,
                local_name=f.local[:300],
                prefix=f.prefix,
                context_ref=f.context_ref[:200],
                period_start=ctx.start if ctx else None,
                period_end=ctx.period_end if ctx else None,
                is_instant=bool(ctx and ctx.is_instant),
                dimensions=[list(d) for d in ctx.dimensions] if ctx else None,
                unit_ref=f.unit_ref,
                unit_label=unit.label if unit else None,
                decimals=f.decimals,
                precision_attr=f.precision,
                value_text=f.text,
                is_nil=f.nil,
                mapped_statement=mapped[0] if mapped else None,
                mapped_item=mapped[1] if mapped else None,
            )
        )
    session.flush()

    report = IngestReport(document, filing, run, mapping, created, issues=issues)
    entity = EntityCheck(confirmed, entity_note)
    doc_latest = _latest_period(session, document.id)
    promoter = _Promoter(session, document, doc_latest) if promote else None
    facts_total = 0
    for block in mapping.blocks:
        results = validate.run_checks(block)
        statuses = validate.fact_statuses(block, results)
        for r in results:
            if not r.passed:
                issues.append(
                    {
                        "page": 1,
                        "code": "SUM_NOT_CONFIRMED" if r.confirm_only else "CHECK_FAILED",
                        "detail": f"{r.name}: {r.form} (difference {r.difference})",
                    }
                )
        stmt = ExtractedStatement(
            extraction_id=run.id,
            page_number=1,
            statement=block.statement,
            basis=block.basis,
            unit=block.unit,
            title=block.title,
            detection=block.detection,
            columns=[c.as_json() for c in block.columns],
            rows_matched=len({f.row_index for f in block.facts}),
            rows_unmatched=0,
            checks=[r.as_json() for r in results],
        )
        session.add(stmt)
        session.flush()
        for fact in block.facts:
            vstatus, notes = statuses[(fact.key, fact.column_index)]
            column = block.column(fact.column_index)
            unit = _unit(block, fact)
            value_inr = (
                normalized_value(fact.value, unit) if fact.value is not None and unit else None
            )
            decision = promotion_problem(
                block,
                fact,
                vstatus,
                True,  # machine-readable source: arithmetic still blocks on FAILED (see module doc)
                True,
                entity,
            )
            problem = decision.problem
            if problem is None and taxonomy.item_kind(fact.key) == "amount":
                problem = _scale_problem(
                    session, company.id, block.statement, fact.key, column, block.basis, value_inr
                )
            row = ExtractedFact(
                extraction_id=run.id,
                statement_id=stmt.id,
                document_id=document.id,
                company_id=company.id,
                statement=block.statement,
                basis=block.basis,
                canonical_item=fact.key,
                raw_label=fact.raw_label[:2000],
                page_number=1,
                row_index=fact.row_index,
                column_index=fact.column_index,
                column_header=column.header[:1000],
                period_end=column.period.period_end,
                duration_months=column.period.duration_months,
                audited=column.period.audited,
                raw_value=fact.raw_value[:60],
                original_value=fact.value,
                original_unit=unit,
                value_inr=value_inr,
                bbox=None,
                dimension=None,
                validation_status=vstatus,
                validation_notes=[
                    *notes,
                    "read from an XBRL instance (no page: stands for page 1)",
                ],
                promoted=False,
                not_promoted_reason=None,
                needs_review=False,
                ocr_confidence=None,
            )
            session.add(row)
            facts_total += 1
            if problem is not None or not promote:
                row.not_promoted_reason = (problem or "promotion disabled")[:200]
            elif block.statement in taxonomy.DISCLOSURES:
                row.promoted = True
            elif decision.period_type is not None and promoter is not None:
                ok, reason = promoter.promote(block, fact, decision.period_type, row)
                row.promoted = ok
                row.not_promoted_reason = reason[:200] if reason else None
            else:
                row.not_promoted_reason = "period type could not be determined"
            if row.promoted:
                report.promoted += 1
            else:
                key = (row.not_promoted_reason or "not promoted")[:120]
                report.not_promoted[key] = report.not_promoted.get(key, 0) + 1
    if promoter:
        issues.extend(promoter.conflicts)
        for dataset, count in promoter.datasets.items():
            if count > 0:
                session.add(
                    DatasetVerification(
                        provider_code=PROVIDER_CODE,
                        dataset=dataset,
                        company_id=document.company_id,
                        ok=True,
                        detail=f"{count} validated XBRL value(s) promoted from {document.id}",
                    )
                )
    session.flush()
    run.statements_found = len(mapping.blocks)
    run.facts_extracted = facts_total
    run.facts_promoted = report.promoted
    run.page_count = 1
    run.issues = issues
    run.status = ExtractionStatus.COMPLETED if mapping.blocks else ExtractionStatus.NO_STATEMENTS
    run.finished_at = datetime.now(UTC)
    filing.issues = issues
    session.flush()
    return report


__all__ = ["EntityMismatch", "IngestReport", "ValidationStatus", "ingest"]
