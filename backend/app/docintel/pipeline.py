"""Run document intelligence on a stored document and persist everything it found.

Steps for one document:
  1. read every page (text layer, OCR only if installed) and keep the page text;
  2. find statement tables and read every cell with its page, label, column header and position;
  3. run the arithmetic checks per statement column;
  4. stage all values in `extracted_facts`, and PROMOTE into `financial_facts` only the values that
     meet every rule in `promotion_problem` (provider code DOCUMENT_EXTRACTION, with the
     document and page recorded on the fact);
  5. record a dataset verification when values were promoted, so the coverage engine can tell
     that this route has actually produced validated data.

Restatements: when two documents report the same line and period with different values, the
document whose own latest period is newer wins (later filings carry restated comparatives); the
older value stays in `extracted_facts` with a note. Nothing is ever averaged or blended.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.docintel import taxonomy, validate
from app.docintel.entity import COVER_PAGES, EntityCheck, check_entity, company_names
from app.docintel.pdf import PdfContent, read_pdf
from app.docintel.statements import (
    CellFact,
    ColumnInfo,
    StatementBlock,
    extract_page_statements,
    merge_continuations,
)
from app.models import (
    Basis,
    DatasetVerification,
    Document,
    DocumentExtraction,
    DocumentPage,
    ExtractedFact,
    ExtractedStatement,
    ExtractionStatus,
    FinancialFact,
    PeriodType,
    Statement,
    ValidationStatus,
)
from app.normalization import fiscal_year_label, normalized_value, quarter_label

EXTRACTOR_VERSION = "5.0"
PROVIDER_CODE = "DOCUMENT_EXTRACTION"
RESTATEMENT_TOLERANCE = Decimal("0.001")
_PG_TEXT_LIMIT = 1_000_000  # characters of text kept per page (defensive cap)


@dataclass
class Decision:
    period_type: PeriodType | None
    problem: str | None  # None = promote


def period_type_for(statement: str, column: ColumnInfo) -> tuple[PeriodType | None, str | None]:
    period = column.period
    if period.period_end is None:
        return None, "column has no readable period"
    if statement == taxonomy.BALANCE_SHEET:
        return (PeriodType.ANNUAL if period.period_end.month == 3 else PeriodType.QUARTERLY), None
    if period.duration_months == 3:
        return PeriodType.QUARTERLY, None
    if period.duration_months == 12:
        return PeriodType.ANNUAL, None
    return None, (
        f"{period.duration_months}-month period: kept as extracted, not stored as a quarterly or "
        "annual figure"
    )


def promotion_problem(
    block: StatementBlock,
    fact: CellFact,
    status: ValidationStatus,
    column_ok: bool,
    company_linked: bool,
    entity: EntityCheck | None = None,
    scanned_page: bool = False,
) -> Decision:
    """Why this value must NOT enter financial_facts, or Decision(problem=None) if it may."""
    column = block.column(fact.column_index)
    if not company_linked:
        return Decision(None, "document is not linked to a company")
    if entity is not None and not entity.confirmed:
        return Decision(None, f"entity not confirmed: {entity.how}")
    if fact.duplicate:
        return Decision(None, "item read twice in this column with different values")
    if fact.value is None:
        return Decision(None, "cell is blank, nil or not a number")
    if not column.usable:
        return Decision(None, column.problem or "column is not a period column")
    if block.basis is None:
        return Decision(None, "consolidated/standalone basis not stated")
    if taxonomy.item_kind(fact.key) == "amount" and block.unit is None:
        return Decision(None, "reporting unit not stated")
    if status is ValidationStatus.FAILED:
        return Decision(None, "an arithmetic check involving this value failed")
    if status is ValidationStatus.UNCHECKED and not column_ok:
        return Decision(None, "no arithmetic check verified this column")
    if scanned_page and status is not ValidationStatus.PASSED:
        # OCR can misread a single digit; only values an identity confirms are trusted.
        return Decision(None, "scanned page (OCR text): only values confirmed by a check are used")
    period_type, problem = period_type_for(block.statement, column)
    return Decision(period_type, problem)


def _unit(block: StatementBlock, fact: CellFact) -> str | None:
    return "inr_per_share" if taxonomy.item_kind(fact.key) == "per_share" else block.unit


def _period_label(statement: str, period_end: date, period_type: PeriodType) -> str:
    if statement == taxonomy.BALANCE_SHEET:
        return f"as at {period_end.isoformat()}"
    return (
        quarter_label(period_end)
        if period_type is PeriodType.QUARTERLY
        else fiscal_year_label(period_end)
    )


_DATASET = {
    (Statement.INCOME, PeriodType.QUARTERLY): "INCOME_QUARTERLY",
    (Statement.INCOME, PeriodType.ANNUAL): "INCOME_ANNUAL",
    (Statement.BALANCE_SHEET, PeriodType.ANNUAL): "BALANCE_SHEET",
    (Statement.BALANCE_SHEET, PeriodType.QUARTERLY): "BALANCE_SHEET",
    (Statement.CASH_FLOW, PeriodType.ANNUAL): "CASH_FLOW",
    (Statement.CASH_FLOW, PeriodType.QUARTERLY): "CASH_FLOW",
}


def _latest_period(session: Session, document_id) -> date | None:
    return session.scalar(
        select(func.max(ExtractedFact.period_end)).where(
            ExtractedFact.document_id == document_id, ExtractedFact.promoted.is_(True)
        )
    )


class _Promoter:
    def __init__(self, session: Session, document: Document, doc_latest: date | None) -> None:
        self.s, self.document, self.doc_latest = session, document, doc_latest
        self.datasets: dict[str, int] = {}
        self._rank_cache: dict = {}
        # values promoted in THIS run, to catch two tables of one document disagreeing
        self._seen: dict[tuple, tuple[ExtractedFact, Decimal | None, FinancialFact]] = {}
        self.conflicts: list[dict] = []
        # A re-run replaces this document's earlier promotions (rules may have changed).
        session.execute(
            delete(FinancialFact).where(
                FinancialFact.provider_code == PROVIDER_CODE,
                FinancialFact.document_id == document.id,
            )
        )

    def _rank(self, document_id) -> tuple:
        if document_id not in self._rank_cache:
            doc = self.s.get(Document, document_id) if document_id else None
            latest = _latest_period(self.s, document_id) if document_id else None
            self._rank_cache[document_id] = (
                latest or date.min,
                (doc.published_at if doc and doc.published_at else date.min),
                (doc.retrieved_at if doc else datetime.min.replace(tzinfo=UTC)),
            )
        return self._rank_cache[document_id]

    def promote(
        self, block: StatementBlock, fact: CellFact, period_type: PeriodType, row: ExtractedFact
    ) -> tuple[bool, str | None]:
        column = block.column(fact.column_index)
        period_end = column.period.period_end
        statement = Statement(block.statement)
        basis = Basis(block.basis)
        unit = _unit(block, fact)
        value_inr = normalized_value(fact.value, unit)
        ident = (statement, fact.key, period_end, period_type, basis)
        if ident in self._seen:
            first_row, first_value, first_fact = self._seen[ident]
            if not _differs(first_value, value_inr):
                return False, "same value already promoted from another table in this document"
            # Two tables of one document disagree: neither value is trusted.
            if first_row.promoted:
                first_row.promoted = False
                first_row.not_promoted_reason = "conflicts with another table in the same document"
                self.s.delete(first_fact)
                self.datasets[_DATASET[(statement, period_type)]] -= 1
            self.conflicts.append(
                {
                    "page": block.page_number,
                    "code": "IN_DOCUMENT_CONFLICT",
                    "detail": f"{fact.key} {period_end}: {first_value} vs {value_inr}",
                }
            )
            return False, "conflicts with another table in the same document"
        existing = self.s.scalar(
            select(FinancialFact).where(
                FinancialFact.company_id == self.document.company_id,
                FinancialFact.provider_code == PROVIDER_CODE,
                FinancialFact.statement == statement,
                FinancialFact.line_item == fact.key,
                FinancialFact.period_end == period_end,
                FinancialFact.period_type == period_type,
                FinancialFact.basis == basis,
            )
        )
        if existing is not None and existing.document_id not in (None, self.document.id):
            differs = _differs(existing.value_inr, value_inr)
            mine = (
                self.doc_latest or date.min,
                self.document.published_at or date.min,
                self.document.retrieved_at,
            )
            # Cross-document evidence is recorded either way (Phase 6 builds on it).
            other = f"document {existing.document_id} p.{existing.source_page}"
            row.validation_notes = [
                *(row.validation_notes or []),
                (
                    f"differs from {other}: {existing.original_value} {existing.original_unit}"
                    if differs
                    else f"agrees with {other}"
                ),
            ]
            if self._rank(existing.document_id) >= mine:
                note = f"a more recent document supplies this period ({other})" + (
                    "; values differ" if differs else "; values agree"
                )
                return False, note
            if differs:
                row.validation_notes.append("replaces the older document's value (restated)")
        if existing is None:
            existing = FinancialFact(
                company_id=self.document.company_id,
                provider_code=PROVIDER_CODE,
                statement=statement,
                line_item=fact.key,
                period_end=period_end,
                period_type=period_type,
                basis=basis,
            )
            self.s.add(existing)
        existing.original_value = fact.value
        existing.original_unit = unit
        existing.period_label = _period_label(block.statement, period_end, period_type)
        existing.value_inr = value_inr
        existing.source_url = self.document.url
        existing.retrieved_at = self.document.retrieved_at
        existing.document_id = self.document.id
        existing.source_page = fact.page_number or block.page_number
        existing.payload_id = None
        self._seen[ident] = (row, value_inr, existing)
        dataset = _DATASET[(statement, period_type)]
        self.datasets[dataset] = self.datasets.get(dataset, 0) + 1
        return True, None


def _differs(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return a is not b
    scale = max(abs(a), abs(b), Decimal(1))
    return abs(a - b) / scale > RESTATEMENT_TOLERANCE


@dataclass
class ExtractionReport:
    extraction: DocumentExtraction
    blocks: list[StatementBlock]


def extract_document(
    session: Session,
    document: Document,
    data_dir: Path,
    *,
    use_ocr: bool | None = None,
    promote: bool = True,
    content: PdfContent | None = None,
) -> ExtractionReport:
    """Run the full pipeline on one stored document. Every outcome is recorded, never raised."""
    run = DocumentExtraction(
        document_id=document.id,
        extractor_version=EXTRACTOR_VERSION,
        status=ExtractionStatus.FAILED,
        issues=[],
    )
    session.add(run)
    session.flush()
    issues: list[dict] = []

    if content is None:
        if document.content_type != "application/pdf":
            return _finish(
                session,
                run,
                ExtractionStatus.UNSUPPORTED,
                issues,
                [],
                error=f"only PDF documents are supported (got {document.content_type})",
            )
        path = (data_dir / document.storage_path).resolve()
        if not path.is_file() or data_dir.resolve() not in path.parents:
            return _finish(
                session, run, ExtractionStatus.FAILED, issues, [], error="stored file is missing"
            )
        content = read_pdf(path, use_ocr=use_ocr)
    if content.error:
        return _finish(session, run, ExtractionStatus.UNSUPPORTED, issues, [], error=content.error)

    run.page_count = content.page_count
    run.ocr_used = content.ocr_used
    if content.truncated:
        issues.append(
            {
                "page": None,
                "code": "TRUNCATED",
                "detail": f"only the first {len(content.pages)} of {content.page_count} "
                "pages were read",
            }
        )
    for page in content.pages:
        session.add(
            DocumentPage(
                extraction_id=run.id,
                page_number=page.number,
                char_count=page.char_count,
                needs_ocr=page.needs_ocr,
                text_method=page.method,
                text=page.text[:_PG_TEXT_LIMIT].replace("\x00", ""),
            )
        )
    run.pages_with_text = sum(1 for p in content.pages if p.char_count and not p.needs_ocr)
    run.pages_needing_ocr = sum(1 for p in content.pages if p.needs_ocr)
    if run.pages_needing_ocr:
        issues.append(
            {
                "page": None,
                "code": "NO_TEXT_LAYER",
                "detail": f"{run.pages_needing_ocr} page(s) have no usable text layer "
                "and were not read" + ("" if content.ocr_used else " (no OCR engine installed)"),
            }
        )

    blocks: list[StatementBlock] = []
    for page in content.pages:
        if page.needs_ocr or not page.words:
            continue
        blocks.extend(extract_page_statements(page.number, page.words))
    blocks = merge_continuations(blocks)

    doc_latest = max(
        (
            c.period.period_end
            for b in blocks
            for c in b.columns
            if c.usable and c.period.period_end
        ),
        default=None,
    )
    names = company_names(session, document.company_id) if document.company_id else set()
    page_text = {p.number: p.text for p in content.pages}
    page_method = {p.number: p.method for p in content.pages}
    cover = "\n".join(p.text for p in content.pages[:COVER_PAGES])
    promoter = _Promoter(session, document, doc_latest) if promote else None
    facts_total = 0
    for block in blocks:
        results = validate.run_checks(block)
        statuses = validate.fact_statuses(block, results)
        entity = check_entity(
            page_text.get(block.page_number, ""),
            cover,
            names,
            first_page_text=content.pages[0].text if content.pages else None,
        )
        if not entity.confirmed and document.company_id is not None:
            block.issues.append(
                {"page": block.page_number, "code": "ENTITY_UNCONFIRMED", "detail": entity.how}
            )
        block_pages = [block.page_number, *block.continued_pages]
        if any(page_method.get(p) == "embedded_ocr" for p in block_pages):
            block.issues.append(
                {
                    "page": block.page_number,
                    "code": "EMBEDDED_OCR_TEXT",
                    "detail": "scanned page with an OCR text layer: only checked values used",
                }
            )
        issues.extend(block.issues)
        for r in results:
            if not r.passed:
                issues.append(
                    {
                        "page": block.page_number,
                        "code": "CHECK_FAILED",
                        "detail": f"{r.name}: {r.form} (difference {r.difference})",
                    }
                )
        stmt_row = ExtractedStatement(
            extraction_id=run.id,
            page_number=block.page_number,
            statement=block.statement,
            basis=block.basis,
            unit=block.unit,
            title=block.title,
            detection=block.detection[:200],
            columns=[c.as_json() for c in block.columns],
            rows_matched=len({f.row_index for f in block.facts}),
            rows_unmatched=len(block.unmatched_labels),
            checks=[r.as_json() for r in results],
        )
        session.add(stmt_row)
        session.flush()
        for fact in block.facts:
            status, notes = statuses[(fact.key, fact.column_index)]
            column = block.column(fact.column_index)
            decision = promotion_problem(
                block,
                fact,
                status,
                validate.column_verified(results, fact.column_index),
                document.company_id is not None,
                entity,
                scanned_page=page_method.get(fact.page_number or block.page_number)
                == "embedded_ocr",
            )
            unit = _unit(block, fact)
            row = ExtractedFact(
                extraction_id=run.id,
                statement_id=stmt_row.id,
                document_id=document.id,
                company_id=document.company_id,
                statement=block.statement,
                basis=block.basis,
                canonical_item=fact.key,
                raw_label=fact.raw_label[:2000],
                page_number=fact.page_number or block.page_number,
                row_index=fact.row_index,
                column_index=fact.column_index,
                column_header=column.header[:1000],
                period_end=column.period.period_end,
                duration_months=column.period.duration_months,
                audited=column.period.audited,
                raw_value=fact.raw_value[:60],
                original_value=fact.value,
                original_unit=unit,
                value_inr=normalized_value(fact.value, unit)
                if fact.value is not None and unit
                else None,
                bbox=list(fact.bbox) if fact.bbox else None,
                validation_status=status,
                validation_notes=notes or None,
                promoted=False,
                not_promoted_reason=None,
            )
            session.add(row)
            facts_total += 1
            if decision.problem is None and promoter and decision.period_type is not None:
                promoted, reason = promoter.promote(block, fact, decision.period_type, row)
                row.promoted = promoted
                row.not_promoted_reason = reason[:200] if reason else None
            else:
                row.not_promoted_reason = (decision.problem or "promotion disabled")[:200]
    if promoter:
        issues.extend(promoter.conflicts)
    session.flush()
    run.statements_found = len(blocks)
    run.facts_extracted = facts_total
    run.facts_promoted = session.scalar(
        select(func.count())
        .select_from(ExtractedFact)
        .where(ExtractedFact.extraction_id == run.id, ExtractedFact.promoted.is_(True))
    )

    for dataset, count in (promoter.datasets if promoter else {}).items():
        if count <= 0:
            continue
        session.add(
            DatasetVerification(
                provider_code=PROVIDER_CODE,
                dataset=dataset,
                company_id=document.company_id,
                ok=True,
                detail=f"{count} validated value(s) promoted from document {document.id}",
            )
        )

    period = _period_from_content(document, blocks)
    if period:
        document.financial_period = period  # only ever fills an EMPTY period
        issues.append(
            {
                "page": None,
                "code": "PERIOD_FROM_CONTENT",
                "detail": f"document period set to {period} from the statements it contains",
            }
        )

    if blocks:
        status = ExtractionStatus.COMPLETED
    elif content.pages and run.pages_needing_ocr == len(content.pages):
        status = ExtractionStatus.NEEDS_OCR
    else:
        status = ExtractionStatus.NO_STATEMENTS
    return _finish(session, run, status, issues, blocks)


def _period_from_content(document: Document, blocks: list[StatementBlock]) -> str | None:
    """The period a results document / annual report covers, read from its own statements.

    Used only when the document has no period yet (link text gave none). Quarterly results take
    the newest 3-month column, annual reports the newest 12-month column; only usable columns of
    statements with a stated basis count.
    """
    if document.financial_period:
        return None
    months = {"quarterly_result": 3, "annual_report": 12}.get(document.doc_type)
    if months is None:
        return None
    ends = [
        c.period.period_end
        for b in blocks
        if b.statement == taxonomy.INCOME and b.basis
        for c in b.columns
        if c.usable and c.period.duration_months == months and c.period.period_end
    ]
    if not ends:
        return None
    latest = max(ends)
    if months == 3:
        try:
            return quarter_label(latest)
        except ValueError:  # not a calendar quarter end
            return None
    return fiscal_year_label(latest)


def _finish(
    session: Session,
    run: DocumentExtraction,
    status: ExtractionStatus,
    issues: list[dict],
    blocks: list[StatementBlock],
    error: str | None = None,
) -> ExtractionReport:
    run.status = status
    run.issues = issues
    run.error = error
    run.finished_at = datetime.now(UTC)
    session.flush()
    return ExtractionReport(run, blocks)
