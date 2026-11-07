"""Router adapter: financial statements read from the company's own stored documents.

`acquire` runs document intelligence on the company's stored PDFs that have not yet been
processed by the current extractor version, then reports which periods have validated values.
It fetches nothing from the internet: documents arrive through Company IR discovery or manual
upload first.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_data_dir
from app.docintel.pipeline import EXTRACTOR_VERSION, PROVIDER_CODE, extract_document
from app.models import Document, DocumentExtraction, FinancialFact, PeriodType, Statement
from app.providers.base import CompanyRef, Dataset, Outcome, ProviderResult, ProviderSpec

FINANCIAL_DOC_TYPES = ("annual_report", "quarterly_result", "investor_presentation")

_DATASETS = {
    Dataset.INCOME_ANNUAL: (Statement.INCOME, PeriodType.ANNUAL),
    Dataset.INCOME_QUARTERLY: (Statement.INCOME, PeriodType.QUARTERLY),
    Dataset.BALANCE_SHEET: (Statement.BALANCE_SHEET, None),
    Dataset.CASH_FLOW: (Statement.CASH_FLOW, None),
}

SPEC = ProviderSpec(
    code=PROVIDER_CODE,
    name="Document intelligence (financial statements read from official company documents)",
    authority_tier=2,
    datasets=frozenset(_DATASETS),
    auth_requirement="None (works on documents already stored)",
    access_method="Local PDF parsing of stored annual reports / results; deterministic, no LLM",
    rate_limits="n/a (local processing)",
    coverage_note=(
        "Only companies and periods whose official PDFs are stored. Values are promoted only "
        "with a stated unit and basis, a readable period and passing arithmetic checks. "
        "Scanned PDFs need OCR (Tesseract), which is optional."
    ),
    terms_note="Company disclosures published for investors; used for research with attribution.",
    redistribution_policy="Cite the document and page; do not republish whole documents",
)


def pending_documents(session: Session, company_id) -> list[Document]:
    """The company's financial PDFs with no completed run by the current extractor version."""
    docs = session.scalars(
        select(Document).where(
            Document.company_id == company_id,
            Document.content_type == "application/pdf",
            Document.doc_type.in_(FINANCIAL_DOC_TYPES),
        )
    ).all()
    done = set(
        session.scalars(
            select(DocumentExtraction.document_id).where(
                DocumentExtraction.document_id.in_([d.id for d in docs]),
                DocumentExtraction.extractor_version == EXTRACTOR_VERSION,
                DocumentExtraction.finished_at.is_not(None),
            )
        )
    )
    return [d for d in docs if d.id not in done]


class DocumentExtractionProvider:
    spec = SPEC

    def is_configured(self) -> bool:
        return True

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        if dataset not in _DATASETS:
            return ProviderResult(
                PROVIDER_CODE,
                dataset,
                Outcome.UNSUPPORTED,
                f"documents do not provide {dataset.value}",
            )
        data_dir = resolve_data_dir(get_settings())
        processed = 0
        for document in pending_documents(session, company.id):
            extract_document(session, document, data_dir)
            processed += 1
        statement, period_type = _DATASETS[dataset]
        query = select(FinancialFact.period_label).where(
            FinancialFact.company_id == company.id,
            FinancialFact.provider_code == PROVIDER_CODE,
            FinancialFact.statement == statement,
        )
        if period_type is not None:
            query = query.where(FinancialFact.period_type == period_type)
        periods = sorted(set(session.scalars(query)), reverse=True)
        note = f"{processed} new document(s) processed" if processed else "no new documents"
        if not periods:
            return ProviderResult(
                PROVIDER_CODE,
                dataset,
                Outcome.NO_DATA,
                f"no validated values in stored documents ({note})",
                extra={"documents_processed": processed},
            )
        return ProviderResult(
            PROVIDER_CODE,
            dataset,
            Outcome.OK,
            f"{len(periods)} period(s) with validated values ({note})",
            rows=len(periods),
            periods=periods,
            extra={"documents_processed": processed},
        )
