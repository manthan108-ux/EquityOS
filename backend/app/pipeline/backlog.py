"""Companies whose stored data has not been carried through the pipeline.

The audit of 2026-11-13 found 117 of 134 Upstox-refreshed companies with facts but no validated
statement evidence, because only the `universe` evidence step creates it. These queries find such
companies directly from the data (not from a log), so a backlog cannot be forgotten.
"""

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models import (
    Company,
    Document,
    DocumentExtraction,
    EvidenceRecord,
    ExtractionStatus,
    FinancialFact,
)


def _has_statement_evidence(company_col):
    return exists().where(
        EvidenceRecord.company_id == company_col,
        EvidenceRecord.kind == "FINANCIAL_STATEMENT",
        EvidenceRecord.status == "ACTIVE",
    )


def missing_evidence(session: Session, limit: int = 100) -> list[Company]:
    """Companies with stored financial facts but no active statement evidence, in name order."""
    has_facts = exists().where(FinancialFact.company_id == Company.id)
    q = (
        select(Company)
        .where(has_facts, ~_has_statement_evidence(Company.id))
        .order_by(Company.common_name)
        .limit(limit)
    )
    return list(session.scalars(q))


def unread_documents(session: Session, limit: int = 100) -> list[Company]:
    """Companies with a stored financial PDF that no extraction run has settled."""
    ran = exists().where(DocumentExtraction.document_id == Document.id)
    q = (
        select(Company)
        .join(Document, Document.company_id == Company.id)
        .where(
            Document.content_type == "application/pdf",
            Document.doc_type.in_(("annual_report", "quarterly_result", "investor_presentation")),
            Document.association_status != "MISMATCH",
            ~ran,
        )
        .group_by(Company.id)
        .order_by(Company.common_name)
        .limit(limit)
    )
    return list(session.scalars(q))


def summary(session: Session) -> dict:
    with_facts = session.scalar(select(func.count(func.distinct(FinancialFact.company_id)))) or 0
    synced = (
        session.scalar(
            select(func.count(func.distinct(EvidenceRecord.company_id))).where(
                EvidenceRecord.kind == "FINANCIAL_STATEMENT", EvidenceRecord.status == "ACTIVE"
            )
        )
        or 0
    )
    docs = session.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            Document.content_type == "application/pdf",
            Document.doc_type.in_(("annual_report", "quarterly_result", "investor_presentation")),
        )
    )
    settled = session.scalar(
        select(func.count(func.distinct(DocumentExtraction.document_id))).where(
            DocumentExtraction.status.in_(
                (
                    ExtractionStatus.COMPLETED,
                    ExtractionStatus.NO_STATEMENTS,
                    ExtractionStatus.UNSUPPORTED,
                )
            )
        )
    )
    return {
        "companies_with_provider_facts": with_facts,
        "companies_with_statement_evidence": synced,
        "companies_missing_evidence": len(missing_evidence(session, 100000)),
        "financial_pdfs_stored": docs or 0,
        "financial_pdfs_read": settled or 0,
        "companies_with_unread_documents": len(unread_documents(session, 100000)),
    }
