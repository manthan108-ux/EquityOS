"""Turn what Phases 4 and 5 stored into evidence records, idempotently.

Sources mirrored as FACT (or CALCULATION for derived quarters):

* `financial_facts`    statement line items (any provider)
* `extracted_facts`    validated disclosures from documents: segments, ratios, schedules,
                       shareholding (current extraction runs of documents linked to the company)
* `shareholding_facts` provider shareholding percentages

Each source row has a NATURAL KEY (not its database id, which changes whenever a document is
re-processed). A record is identified by (source_table, natural key, content hash):

* same key, same content          -> nothing happens
* same key, different content     -> the old record becomes SUPERSEDED, a new one is created
* key no longer present           -> the old record becomes WITHDRAWN
* an old version comes back       -> that record is reactivated (it is the same evidence)

Records are never deleted, so a calculation or sentence that cited a value keeps pointing at it,
and the validation engine can say the input is no longer current.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.core import DEFAULT_TIERS, WEAKEST_TIER, Fact, agree, canon
from app.models import (
    Document,
    DocumentExtraction,
    DocumentSection,
    EvidenceClass,
    EvidenceLink,
    EvidenceRecord,
    EvidenceStatus,
    ExtractedFact,
    FinancialFact,
    LinkRole,
    MasterDataImport,
    PeriodType,
    Security,
    ShareholdingFact,
    Source,
    ValidationStatus,
)
from app.normalization import PER_SHARE_UNITS, UNIT_TO_INR
from app.providers.upstox_map import UPSTOX_CODE, canonical_line

T_FINANCIAL = "financial_facts"
T_DISCLOSURE = "extracted_facts"
T_SHAREHOLDING = "shareholding_facts"
T_SECTION = "document_sections"
T_SECURITY = "securities"
SYNC_TABLES = (T_FINANCIAL, T_DISCLOSURE, T_SHAREHOLDING, T_SECTION, T_SECURITY)

DISCLOSURE_KINDS = ("SEGMENT", "RATIOS", "NOTE", "SHAREHOLDING", "OPERATING")
PRIMARY_STATEMENTS = ("INCOME", "BALANCE_SHEET", "CASH_FLOW")
DERIVED_PROVIDERS = {"DERIVED_QUARTER"}
_MONTHS = {
    PeriodType.QUARTERLY.value: 3,
    PeriodType.HALF_YEAR.value: 6,
    PeriodType.NINE_MONTH.value: 9,
    PeriodType.ANNUAL.value: 12,
}
_DURATION_TYPE = {3: "QUARTERLY", 6: "HALF_YEAR", 9: "NINE_MONTH", 12: "ANNUAL"}


@dataclass
class Draft:
    """An evidence record about to be created or matched to an existing one."""

    source_table: str
    source_id: str
    evidence_class: EvidenceClass
    kind: str
    metric: str
    period_end: object
    value: Decimal | None
    unit: str | None
    provider_code: str
    tier: int
    source_url: str
    text: str | None = None  # narrative evidence: the statement itself
    dimension: str = ""
    period_type: str | None = None
    basis: str | None = None
    original_value: Decimal | None = None
    original_unit: str | None = None
    expression: str | None = None
    document_id: uuid.UUID | None = None
    page: int | None = None
    retrieved_at: datetime | None = None
    details: dict = field(default_factory=dict)
    inputs: list[tuple[str, str]] = field(default_factory=list)  # (alias, source_id) of facts

    def content_hash(self) -> str:
        payload = {
            "k": [self.source_table, self.source_id],
            "m": [self.metric, self.dimension, str(self.period_end), self.period_type, self.basis],
            "v": [canon(self.value), self.unit, canon(self.original_value), self.original_unit],
            "src": [self.provider_code, str(self.document_id), self.page, self.source_url],
            "e": self.expression,
            "t": self.text,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class SyncReport:
    created: int = 0
    unchanged: int = 0
    superseded: int = 0
    withdrawn: int = 0
    reactivated: int = 0
    linked: int = 0
    skipped: list[str] = field(default_factory=list)

    def total_changes(self) -> int:
        return self.created + self.superseded + self.withdrawn + self.reactivated


def load_tiers(session: Session) -> dict[str, int]:
    tiers = dict(DEFAULT_TIERS)
    for code, tier in session.execute(select(Source.code, Source.authority_tier)):
        tiers[code] = tier
    return tiers


def _financial_key(f: FinancialFact) -> str:
    return (
        f"{f.provider_code}|{f.statement.value}|{f.line_item}|{f.period_end}|"
        f"{f.period_type.value}|{f.basis.value}"
    )


def _decimals(raw: str | None) -> int:
    if not raw or "." not in raw:
        return 0
    tail = raw.strip().split(".")[-1]
    digits = "".join(ch for ch in tail if ch.isdigit())
    return len(digits)


def normalized(
    original_value: Decimal | None, original_unit: str | None, value_inr: Decimal | None
) -> tuple[Decimal, str, bool] | None:
    """(value, unit, unit_was_unstated) in the unit evidence uses; None if there is no value."""
    if original_value is None:
        return None
    unit = (original_unit or "").strip().lower()
    if value_inr is not None and unit in UNIT_TO_INR:
        return value_inr, "inr", False
    if unit in PER_SHARE_UNITS:
        return original_value, "inr_per_share", False
    if unit:
        return original_value, unit, False
    return original_value, "unstated", True


def _extracted_index(session: Session, company_id: uuid.UUID) -> dict[tuple, list[ExtractedFact]]:
    """Validated cells of the company's current extraction runs, by what they measure."""
    rows = session.scalars(
        select(ExtractedFact)
        .join(DocumentExtraction, DocumentExtraction.id == ExtractedFact.extraction_id)
        .join(Document, Document.id == ExtractedFact.document_id)
        .where(
            Document.company_id == company_id,
            DocumentExtraction.superseded_at.is_(None),
            ExtractedFact.statement.in_(PRIMARY_STATEMENTS),
            ExtractedFact.validation_status == ValidationStatus.PASSED,
            ExtractedFact.needs_review.is_(False),
        )
    )
    index: dict[tuple, list[ExtractedFact]] = {}
    for r in rows:
        key = (r.statement, r.canonical_item, r.period_end, r.basis, r.duration_months)
        index.setdefault(key, []).append(r)
    return index


def _financial_drafts(
    session: Session, company_id: uuid.UUID, tiers: dict[str, int], report: SyncReport
) -> list[Draft]:
    facts = list(
        session.scalars(select(FinancialFact).where(FinancialFact.company_id == company_id))
    )
    by_id = {f.id: f for f in facts}
    cells = _extracted_index(session, company_id)
    # Upstox lines whose definition can differ from a filing's (ITC's filing shows revenue gross of
    # excise duty, Upstox net of it) are only filed under the canonical keys for a statement,
    # period and basis that no filing covers: blending parts of both inside one statement would
    # break the arithmetic checks. The raw Upstox lines are always kept (providers/upstox_map.py).
    filing_covered = {
        (f.statement.value, f.period_end, f.period_type.value, f.basis.value)
        for f in facts
        if f.provider_code != UPSTOX_CODE and f.provider_code not in DERIVED_PROVIDERS
    }
    drafts: dict[str, Draft] = {}
    for f in facts:
        unit_name = (f.original_unit or "").strip().lower()
        # A statement line item is either rupees (convertible) or per share; anything else is a
        # unit we do not understand and must not be turned into evidence.
        convertible = unit_name in PER_SHARE_UNITS or (
            f.value_inr is not None and unit_name in UNIT_TO_INR
        )
        norm = normalized(f.original_value, f.original_unit, f.value_inr) if convertible else None
        if norm is None or norm[2]:
            report.skipped.append(
                f"{f.provider_code} {f.statement.value}.{f.line_item} {f.period_label}: unit not "
                f"convertible ({f.original_unit!r}); not used as evidence"
            )
            continue
        value, unit, _ = norm
        derived = f.provider_code in DERIVED_PROVIDERS
        statement = f.statement.value
        covered = (statement, f.period_end, f.period_type.value, f.basis.value) in filing_covered
        line_name = canonical_line(f.provider_code, statement, f.line_item, covered)
        details: dict = {"period_label": f.period_label}
        if f.history:
            details["restated"] = True
            details["history"] = f.history
        # Printed decimals / OCR confidence / the same cell in other documents (Phase 5 data).
        if f.document_id is not None and not derived:
            months = _MONTHS.get(f.period_type.value) if statement != "BALANCE_SHEET" else None
            candidates = cells.get(
                (statement, f.line_item, f.period_end, f.basis.value, months), []
            )
            mine = [c for c in candidates if c.document_id == f.document_id]
            if mine:
                details["decimals"] = _decimals(mine[-1].raw_value)
                if mine[-1].ocr_confidence is not None:
                    details["ocr_confidence"] = str(mine[-1].ocr_confidence)
            others = [c for c in candidates if c.document_id != f.document_id]
            if others:
                this = Fact(
                    0,
                    "FACT",
                    "",
                    "",
                    f.period_end,
                    "",
                    "",
                    value,
                    unit,
                    f.provider_code,
                    2,
                    original_unit=f.original_unit,
                    details=dict(details),
                )
                agreeing, differing = [], []
                for c in others:
                    if c.value_inr is None:
                        continue
                    other = Fact(
                        0,
                        "FACT",
                        "",
                        "",
                        f.period_end,
                        "",
                        "",
                        c.value_inr,
                        unit,
                        f.provider_code,
                        2,
                        original_unit=c.original_unit,
                        details={"decimals": _decimals(c.raw_value)},
                    )
                    row = {
                        "document_id": str(c.document_id),
                        "page": c.page_number,
                        "value": str(c.value_inr),
                    }
                    (agreeing if agree(this, other) else differing).append(row)
                details["other_documents_agree"] = len(agreeing)
                details["other_documents_differ"] = len(differing)
                details["other_documents"] = (agreeing + differing)[:6]
        draft = Draft(
            source_table=T_FINANCIAL,
            source_id=_financial_key(f),
            evidence_class=EvidenceClass.CALCULATION if derived else EvidenceClass.FACT,
            kind="FINANCIAL_STATEMENT",
            metric=f"{statement}.{line_name}",
            period_end=f.period_end,
            period_type=f.period_type.value,
            basis=f.basis.value,
            value=value,
            unit=unit,
            original_value=None if derived else f.original_value,
            original_unit=None if derived else f.original_unit,
            provider_code=f.provider_code,
            tier=tiers.get(f.provider_code, DEFAULT_TIERS.get(f.provider_code, WEAKEST_TIER)),
            source_url=f.source_url,
            document_id=f.document_id,
            page=f.source_page,
            retrieved_at=f.retrieved_at,
            details=details,
        )
        if derived:
            _fill_derived(draft, f, by_id, tiers, details)
        drafts[draft.source_id] = draft
    # A derived quarter is only as authoritative as its weakest input.
    for d in drafts.values():
        if d.evidence_class is EvidenceClass.CALCULATION and d.inputs:
            input_tiers = [drafts[sid].tier for _, sid in d.inputs if sid in drafts]
            if input_tiers:
                d.tier = max([d.tier, *input_tiers])
    return list(drafts.values())


def _fill_derived(
    draft: Draft,
    f: FinancialFact,
    by_id: dict[int, FinancialFact],
    tiers: dict[str, int],
    details: dict,
) -> None:
    source = f.derived_from or {}
    draft.expression = "later_cumulative - earlier_cumulative"
    if source.get("rounding_uncertainty_inr") is not None:
        details["rounding"] = str(source["rounding_uncertainty_inr"])
    details["method"] = source.get("method")
    for alias, part in (("later_cumulative", "minuend"), ("earlier_cumulative", "subtrahend")):
        ref = (source.get(part) or {}).get("fact_id")
        origin = by_id.get(ref) if ref is not None else None
        if origin is None:
            details.setdefault("missing_inputs", []).append(alias)
            continue
        draft.inputs.append((alias, _financial_key(origin)))


def _disclosure_drafts(
    session: Session, company_id: uuid.UUID, tiers: dict[str, int], report: SyncReport
) -> list[Draft]:
    rows = session.scalars(
        select(ExtractedFact)
        .join(DocumentExtraction, DocumentExtraction.id == ExtractedFact.extraction_id)
        .join(Document, Document.id == ExtractedFact.document_id)
        .where(
            Document.company_id == company_id,
            Document.association_status != "MISMATCH",
            DocumentExtraction.superseded_at.is_(None),
            ExtractedFact.promoted.is_(True),
            ExtractedFact.statement.in_(DISCLOSURE_KINDS),
        )
    )
    documents: dict[uuid.UUID, Document] = {}
    drafts: dict[str, Draft] = {}
    for r in rows:
        norm = normalized(r.original_value, r.original_unit, r.value_inr)
        if norm is None or r.period_end is None:
            report.skipped.append(
                f"{r.statement}.{r.canonical_item} on page {r.page_number}: no value or period"
            )
            continue
        value, unit, unstated = norm
        document = documents.get(r.document_id) or session.get(Document, r.document_id)
        documents[r.document_id] = document
        natural = "|".join(
            str(x)
            for x in (
                r.document_id,
                r.statement,
                r.canonical_item,
                r.dimension or "",
                r.period_end,
                r.duration_months,
                r.basis or "",
                r.page_number,
                r.row_index,
                r.column_index,
            )
        )
        sid = hashlib.sha1(natural.encode()).hexdigest()
        details: dict = {"decimals": _decimals(r.raw_value), "raw_value": r.raw_value}
        if unstated:
            details["unit_unstated"] = True
        if r.ocr_confidence is not None:
            details["ocr_confidence"] = str(r.ocr_confidence)
        drafts[sid] = Draft(
            source_table=T_DISCLOSURE,
            source_id=sid,
            evidence_class=EvidenceClass.FACT,
            kind="SHAREHOLDING" if r.statement == "SHAREHOLDING" else "DISCLOSURE",
            # A shareholding category is printed twice (number of shares and % holding): two
            # different measures, so the unit is part of the metric name.
            metric=f"{r.statement}.{r.canonical_item}"
            + (f".{unit}" if r.statement == "SHAREHOLDING" else ""),
            dimension=r.dimension or "",
            period_end=r.period_end,
            period_type=_DURATION_TYPE.get(r.duration_months) if r.duration_months else None,
            basis=r.basis,
            value=value,
            unit=unit,
            original_value=r.original_value,
            original_unit=r.original_unit,
            provider_code="DOCUMENT_EXTRACTION",
            tier=tiers.get("DOCUMENT_EXTRACTION", 2),
            source_url=document.url if document else "",
            document_id=r.document_id,
            page=r.page_number,
            retrieved_at=document.retrieved_at if document else None,
            details=details,
        )
    return list(drafts.values())


def _shareholding_drafts(
    session: Session, company_id: uuid.UUID, tiers: dict[str, int]
) -> list[Draft]:
    drafts = []
    for s in session.scalars(
        select(ShareholdingFact).where(ShareholdingFact.company_id == company_id)
    ):
        drafts.append(
            Draft(
                source_table=T_SHAREHOLDING,
                source_id=f"{s.provider_code}|{s.category}|{s.period_end}",
                evidence_class=EvidenceClass.FACT,
                kind="SHAREHOLDING",
                metric=f"SHAREHOLDING.{s.category}.percent",
                period_end=s.period_end,
                value=s.percent,
                unit="percent",
                original_value=s.percent,
                original_unit="percent",
                provider_code=s.provider_code,
                tier=tiers.get(s.provider_code, DEFAULT_TIERS.get(s.provider_code, WEAKEST_TIER)),
                source_url=s.source_url,
                retrieved_at=s.retrieved_at,
                details={"period_label": s.period_label},
            )
        )
    return drafts


def _security_drafts(session: Session, company_id: uuid.UUID, tiers: dict[str, int]) -> list[Draft]:
    """The face value printed in the exchange's own security list, as a static FACT.

    It is not tied to a period (a face value changes only on a split, which the price-adjustment
    events record), so it has no resolution; it is what turns paid-up capital into a share count.
    """
    drafts = []
    for security in session.scalars(select(Security).where(Security.company_id == company_id)):
        if security.face_value is None or security.face_value <= 0:
            continue
        source = (
            session.get(MasterDataImport, security.last_import_id)
            if security.last_import_id
            else None
        )
        exchange = source.exchange_code if source else "NSE"
        name = source.filename if source else "security-list"
        drafts.append(
            Draft(
                source_table=T_SECURITY,
                source_id=f"{security.id}|face_value",
                evidence_class=EvidenceClass.FACT,
                kind="SECURITY",
                metric="SECURITY.face_value",
                period_end=None,
                value=security.face_value,
                unit="inr_per_share",
                original_value=security.face_value,
                original_unit="inr_per_share",
                provider_code="NSE_ARCHIVES",
                tier=tiers.get("NSE_ARCHIVES", 1),
                source_url=f"master-data://{exchange}/{name}",
                dimension=f"{exchange}:{security.symbol}",
                retrieved_at=source.imported_at if source else None,
                details={"listing": security.symbol, "isin": security.isin},
            )
        )
    return drafts


def _section_drafts(session: Session, company_id: uuid.UUID, tiers: dict[str, int]) -> list[Draft]:
    """Narrative sections (auditor's report, policies, MD&A) as text FACTs.

    The evidence record holds the heading, document and pages, plus the explicit flags found in the
    text; the full text stays in `document_sections` so a citation can quote it.
    """
    rows = session.execute(
        select(DocumentSection, Document)
        .join(DocumentExtraction, DocumentExtraction.id == DocumentSection.extraction_id)
        .join(Document, Document.id == DocumentSection.document_id)
        .where(
            Document.company_id == company_id,
            Document.association_status != "MISMATCH",
            DocumentExtraction.superseded_at.is_(None),
        )
    ).all()
    drafts: dict[str, Draft] = {}
    for section, document in rows:
        natural = f"{section.document_id}|{section.kind}|{section.heading}|{section.start_page}"
        sid = hashlib.sha1(natural.encode()).hexdigest()
        drafts[sid] = Draft(
            source_table=T_SECTION,
            source_id=sid,
            evidence_class=EvidenceClass.FACT,
            kind="DOCUMENT_SECTION",
            metric=f"SECTION.{section.kind.lower()}",
            period_end=None,
            value=None,
            unit=None,
            text=section.heading.strip() or section.kind,
            provider_code="DOCUMENT_EXTRACTION",
            tier=tiers.get("DOCUMENT_EXTRACTION", 2),
            source_url=document.url,
            document_id=document.id,
            page=section.start_page,
            retrieved_at=document.retrieved_at,
            details={
                "section_id": section.id,
                "kind": section.kind,
                "start_page": section.start_page,
                "end_page": section.end_page,
                "characters": len(section.text),
                "flags": section.flags,
            },
        )
    return list(drafts.values())


def _new_record(company_id: uuid.UUID, d: Draft, digest: str, now: datetime) -> EvidenceRecord:
    return EvidenceRecord(
        company_id=company_id,
        evidence_class=d.evidence_class,
        kind=d.kind,
        metric=d.metric,
        dimension=d.dimension,
        period_end=d.period_end,
        period_type=d.period_type,
        basis=d.basis,
        value=d.value,
        unit=d.unit,
        original_value=d.original_value,
        original_unit=d.original_unit,
        expression=d.expression,
        statement_text=d.text,
        provider_code=d.provider_code,
        authority_tier=d.tier,
        source_table=d.source_table,
        source_id=d.source_id,
        document_id=d.document_id,
        source_page=d.page,
        source_url=d.source_url,
        retrieved_at=d.retrieved_at,
        details=d.details or None,
        content_hash=digest,
        status=EvidenceStatus.ACTIVE,
        created_by="evidence-sync",
        status_changed_at=now,
    )


def sync_company(
    session: Session, company_id: uuid.UUID, *, now: datetime | None = None
) -> SyncReport:
    """Bring the evidence store in line with the fact tables for one company."""
    now = now or datetime.now(UTC)
    report = SyncReport()
    tiers = load_tiers(session)
    drafts = [
        *_financial_drafts(session, company_id, tiers, report),
        *_disclosure_drafts(session, company_id, tiers, report),
        *_shareholding_drafts(session, company_id, tiers),
        *_section_drafts(session, company_id, tiers),
        *_security_drafts(session, company_id, tiers),
    ]
    existing = list(
        session.scalars(
            select(EvidenceRecord).where(
                EvidenceRecord.company_id == company_id,
                EvidenceRecord.source_table.in_(SYNC_TABLES),
            )
        )
    )
    by_version = {(r.source_table, r.source_id, r.content_hash): r for r in existing}
    active = {
        (r.source_table, r.source_id): r for r in existing if r.status is EvidenceStatus.ACTIVE
    }
    seen: set[tuple[str, str]] = set()
    current: dict[tuple[str, str], EvidenceRecord] = {}
    for d in drafts:
        key = (d.source_table, d.source_id)
        digest = d.content_hash()
        seen.add(key)
        record = by_version.get((d.source_table, d.source_id, digest))
        live = active.get(key)
        if record is not None and record.status is EvidenceStatus.ACTIVE:
            report.unchanged += 1
        else:
            if live is not None:
                live.status = EvidenceStatus.SUPERSEDED
                live.status_reason = "the source now holds a different value or location"
                live.status_changed_at = now
                report.superseded += 1
            if record is not None:  # an older version returned
                record.status = EvidenceStatus.ACTIVE
                record.status_reason = None
                record.status_changed_at = now
                report.reactivated += 1
            else:
                record = _new_record(company_id, d, digest, now)
                session.add(record)
                report.created += 1
        current[key] = record
        # keep the in-memory index right for derived-quarter links below
    for key, record in active.items():
        if key not in seen and record.status is EvidenceStatus.ACTIVE:
            record.status = EvidenceStatus.WITHDRAWN
            record.status_reason = "the source row no longer holds this value"
            record.status_changed_at = now
            report.withdrawn += 1
    session.flush()
    _link_derived(session, drafts, current, report)
    return report


def _link_derived(
    session: Session,
    drafts: list[Draft],
    current: dict[tuple[str, str], EvidenceRecord],
    report: SyncReport,
) -> None:
    for d in drafts:
        if not d.inputs:
            continue
        record = current[(d.source_table, d.source_id)]
        links = list(
            session.scalars(select(EvidenceLink).where(EvidenceLink.evidence_id == record.id))
        )
        for alias, sid in d.inputs:
            source = current.get((T_FINANCIAL, sid))
            if source is None:
                continue
            same_alias = [link for link in links if link.alias == alias]
            if any(link.input_id == source.id for link in same_alias):
                continue
            for stale in same_alias:  # the input record was replaced: point at the current one
                session.delete(stale)
            session.add(
                EvidenceLink(
                    evidence_id=record.id, input_id=source.id, role=LinkRole.INPUT, alias=alias
                )
            )
            report.linked += 1
    session.flush()


__all__ = [
    "Draft",
    "SyncReport",
    "load_tiers",
    "normalized",
    "sync_company",
]
