"""Manual data routes: documents and financial-statement exports supplied by the user.

Use these when no permitted API or website covers a company, or to add data you hold under your own
licence (for example an export from a data service you subscribe to). Everything is recorded with
its origin (`MANUAL_UPLOAD` / `MANUAL_IMPORT`), file name and SHA-256, so it is never confused with
data fetched from a provider.

Financial CSV format (header row required, one fact per line):

    isin,statement,line_item,period_end,period_type,basis,value,unit
    INE002A01018,INCOME,Revenue,2026-03-31,ANNUAL,CONSOLIDATED,1086181,crore

  statement    INCOME | BALANCE_SHEET | CASH_FLOW
  period_end   YYYY-MM-DD, or a month-year label such as "Mar 2026"
  period_type  ANNUAL | QUARTERLY
  basis        CONSOLIDATED | STANDALONE       (the two are never mixed)
  value        number; '(123)' means negative; '-' or empty is rejected (missing is not zero)
  unit         crore | lakh | million | billion | thousand | inr | inr_per_share

The import is ALL-OR-NOTHING: if any row is invalid nothing is stored, and every problem is listed.
"""

import csv
import hashlib
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_data_dir
from app.models import Basis, Document, FinancialFact, PeriodType, Security, Statement
from app.normalization import (
    PER_SHARE_UNITS,
    UNIT_TO_INR,
    normalized_value,
    parse_amount,
    parse_period_label,
)
from app.providers.base import CompanyRef, Dataset, Outcome, ProviderResult, ProviderSpec
from app.providers.documents import ALLOWED_DOC_TYPES, MAX_MANUAL_BYTES, store_document_bytes
from app.providers.persistence import FactRow, archive_payload, store_financial_facts

UPLOAD_CODE = "MANUAL_UPLOAD"
IMPORT_CODE = "MANUAL_IMPORT"
REQUIRED_COLUMNS = {
    "isin",
    "statement",
    "line_item",
    "period_end",
    "period_type",
    "basis",
    "value",
    "unit",
}
VALID_UNITS = set(UNIT_TO_INR) | PER_SHARE_UNITS


class ManualImportError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
        )
        self.problems = problems


@dataclass
class FinancialsImportReport:
    filename: str
    sha256: str
    rows: int
    companies: int
    periods: list[str] = field(default_factory=list)


def _period_end(text: str) -> date | None:
    text = text.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return parse_period_label(text)


def import_financials_csv(
    session: Session, content: bytes, filename: str
) -> FinancialsImportReport:
    """Validate and store a financial-statement CSV; raises ManualImportError with all problems."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    header = {(h or "").strip().lower() for h in reader.fieldnames or []}
    if not REQUIRED_COLUMNS <= header:
        raise ManualImportError([f"missing column(s): {sorted(REQUIRED_COLUMNS - header)}"])

    sha = hashlib.sha256(content).hexdigest()
    problems: list[str] = []
    parsed: dict[uuid.UUID, list[FactRow]] = {}
    seen: set[tuple] = set()
    isin_to_company: dict[str, uuid.UUID | None] = {}

    for line, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        where = f"line {line}"
        isin = row["isin"].upper()
        if isin not in isin_to_company:
            isin_to_company[isin] = session.scalar(
                select(Security.company_id).where(Security.isin == isin).limit(1)
            )
        company_id = isin_to_company[isin]
        if company_id is None:
            problems.append(f"{where}: unknown ISIN '{isin}'")
            continue
        try:
            statement = Statement(row["statement"].upper())
            period_type = PeriodType(row["period_type"].upper())
            basis = Basis(row["basis"].upper())
        except ValueError:
            problems.append(
                f"{where}: invalid statement/period_type/basis "
                f"({row['statement']}/{row['period_type']}/{row['basis']})"
            )
            continue
        end = _period_end(row["period_end"])
        value = parse_amount(row["value"])
        unit = row["unit"].lower()
        if not row["line_item"]:
            problems.append(f"{where}: line_item is empty")
        if end is None:
            problems.append(f"{where}: unreadable period_end '{row['period_end']}'")
        if value is None:
            problems.append(f"{where}: value '{row['value']}' is missing or not a number")
        if unit not in VALID_UNITS:
            problems.append(f"{where}: unknown unit '{row['unit']}'")
        if not row["line_item"] or end is None or value is None or unit not in VALID_UNITS:
            continue
        key = (company_id, statement, row["line_item"], end, period_type, basis)
        if key in seen:
            problems.append(f"{where}: duplicate of an earlier row ({row['line_item']}, {end})")
            continue
        seen.add(key)
        parsed.setdefault(company_id, []).append(
            FactRow(
                statement,
                row["line_item"],
                end,
                period_type,
                basis,
                value,
                unit,
                row["period_end"],
                normalized_value(value, unit),
            )
        )

    if not parsed and not problems:
        problems.append("file contains no data rows")
    if problems:
        raise ManualImportError(problems)

    retrieved = datetime.now(UTC)
    url = f"manual-import://{filename}#{sha[:12]}"
    total = 0
    periods: set[str] = set()
    for company_id, rows in parsed.items():
        payload = archive_payload(
            session,
            company_id=company_id,
            provider_code=IMPORT_CODE,
            dataset="MANUAL_FINANCIALS",
            request_url=url,
            payload=[
                {
                    "line_item": r.line_item,
                    "period_end": r.period_end.isoformat(),
                    "value": str(r.original_value),
                    "unit": r.original_unit,
                    "basis": r.basis.value,
                    "statement": r.statement.value,
                }
                for r in rows
            ],
            retrieved_at=retrieved,
        )
        total += store_financial_facts(
            session,
            company_id,
            IMPORT_CODE,
            rows,
            source_url=url,
            retrieved_at=retrieved,
            payload_id=payload.id,
        )
        periods |= {r.period_end.isoformat() for r in rows}
    return FinancialsImportReport(filename, sha, total, len(parsed), sorted(periods, reverse=True))


def ingest_document_file(
    session: Session,
    path: Path,
    *,
    company_id: uuid.UUID,
    doc_type: str,
    title: str | None = None,
    published_at: date | None = None,
    financial_period: str | None = None,
    data_dir: Path | None = None,
    force_association: bool = False,
) -> tuple[Document, bool]:
    """Store a local file as a document. The file name is never used to build a storage path.

    A PDF issued by a different entity than `company_id` is REFUSED (AssociationMismatch) unless
    `force_association=True`, which links it anyway and records that it was forced.
    """
    if doc_type not in ALLOWED_DOC_TYPES:
        raise ValueError(f"Unknown doc_type '{doc_type}'")
    size = path.stat().st_size
    if size > MAX_MANUAL_BYTES:
        raise ValueError(f"File is larger than {MAX_MANUAL_BYTES} bytes")
    return store_document_bytes(
        session,
        data_dir or resolve_data_dir(get_settings()),
        path.read_bytes(),
        url=f"manual-upload://{path.name}",
        doc_type=doc_type,
        source_code=UPLOAD_CODE,
        company_id=company_id,
        title=title or path.name,
        published_at=published_at,
        financial_period=financial_period,
        on_mismatch="raise",
        force_association=force_association,
    )


_DOC_DATASETS = {
    Dataset.ANNUAL_REPORTS: "annual_report",
    Dataset.QUARTERLY_RESULT_DOCS: "quarterly_result",
    Dataset.INVESTOR_PRESENTATIONS: "investor_presentation",
    Dataset.EARNINGS_TRANSCRIPTS: "earnings_transcript",
    Dataset.XBRL_FILINGS: "xbrl",
    Dataset.CORPORATE_ANNOUNCEMENTS: "corporate_announcement",
}
_FIN_DATASETS = {
    Dataset.INCOME_ANNUAL: (Statement.INCOME, PeriodType.ANNUAL),
    Dataset.INCOME_QUARTERLY: (Statement.INCOME, PeriodType.QUARTERLY),
    Dataset.BALANCE_SHEET: (Statement.BALANCE_SHEET, PeriodType.ANNUAL),
    Dataset.CASH_FLOW: (Statement.CASH_FLOW, PeriodType.ANNUAL),
}

SPEC = ProviderSpec(
    code="MANUAL",
    name="Manual documents and financial exports (user supplied)",
    authority_tier=6,
    datasets=frozenset(_DOC_DATASETS) | frozenset(_FIN_DATASETS),
    auth_requirement="None (local files provided by the user)",
    access_method="CLI: ingest-document / import-financials; files validated on entry",
    rate_limits="n/a",
    coverage_note="Only what the user supplies. Reported as USER_SUPPLIED, never as verified.",
    terms_note="The user is responsible for holding the right to use the supplied data.",
    redistribution_policy="Follows the user's own licence for the supplied data",
)


class ManualProvider:
    """Reports what the user has already supplied; it fetches nothing."""

    spec = SPEC

    def is_configured(self) -> bool:
        return True

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        if dataset in _DOC_DATASETS:
            docs = session.scalars(
                select(Document).where(
                    Document.company_id == company.id,
                    Document.doc_type == _DOC_DATASETS[dataset],
                    Document.url.like("manual-upload://%"),
                )
            ).all()
            if not docs:
                return ProviderResult(
                    IMPORT_CODE, dataset, Outcome.NO_DATA, "no manually supplied documents"
                )
            periods = sorted({d.financial_period for d in docs if d.financial_period}, reverse=True)
            return ProviderResult(
                UPLOAD_CODE,
                dataset,
                Outcome.OK,
                f"{len(docs)} manual document(s)",
                len(docs),
                periods,
            )
        if dataset in _FIN_DATASETS:
            statement, period_type = _FIN_DATASETS[dataset]
            rows = session.execute(
                select(FinancialFact.period_label)
                .where(
                    FinancialFact.company_id == company.id,
                    FinancialFact.provider_code == IMPORT_CODE,
                    FinancialFact.statement == statement,
                    FinancialFact.period_type == period_type,
                )
                .distinct()
            ).all()
            if not rows:
                return ProviderResult(
                    IMPORT_CODE, dataset, Outcome.NO_DATA, "no manually imported data"
                )
            return ProviderResult(
                IMPORT_CODE,
                dataset,
                Outcome.OK,
                f"{len(rows)} imported period(s)",
                len(rows),
                sorted(r[0] for r in rows),
            )
        return ProviderResult(
            IMPORT_CODE, dataset, Outcome.UNSUPPORTED, f"manual route has no {dataset.value}"
        )
