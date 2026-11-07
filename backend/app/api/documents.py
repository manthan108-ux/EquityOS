"""Read-only API for stored documents and what document intelligence found in them."""

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.storage import storage_report
from app.models import (
    Company,
    Document,
    DocumentExtraction,
    DocumentPage,
    ExtractedFact,
    ExtractedStatement,
)

router = APIRouter(tags=["documents"])
SessionDep = Annotated[Session, Depends(get_session)]


class ExtractionSummary(BaseModel):
    id: int
    status: str
    extractor_version: str
    page_count: int
    pages_with_text: int
    pages_needing_ocr: int
    ocr_used: bool
    statements_found: int
    facts_extracted: int
    facts_promoted: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str | None
    doc_type: str
    url: str
    financial_period: str | None
    published_at: date | None
    retrieved_at: datetime
    size_bytes: int
    latest_extraction: ExtractionSummary | None


class StatementOut(BaseModel):
    page_number: int
    statement: str
    basis: str | None
    unit: str | None
    title: str | None
    detection: str
    columns: list[dict]
    rows_matched: int
    rows_unmatched: int
    checks: list[dict]


class ExtractionDetail(BaseModel):
    document: DocumentOut
    issues: list[dict]
    statements: list[StatementOut]
    not_promoted: dict[str, int]  # reason -> number of values


class PageOut(BaseModel):
    document_id: uuid.UUID
    page_number: int
    text_method: str
    needs_ocr: bool
    text: str


def _summary(run: DocumentExtraction | None) -> ExtractionSummary | None:
    if run is None:
        return None
    return ExtractionSummary(
        id=run.id,
        status=run.status.value,
        extractor_version=run.extractor_version,
        page_count=run.page_count,
        pages_with_text=run.pages_with_text,
        pages_needing_ocr=run.pages_needing_ocr,
        ocr_used=run.ocr_used,
        statements_found=run.statements_found,
        facts_extracted=run.facts_extracted,
        facts_promoted=run.facts_promoted,
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _latest_run(session: Session, document_id: uuid.UUID) -> DocumentExtraction | None:
    return session.scalar(
        select(DocumentExtraction)
        .where(
            DocumentExtraction.document_id == document_id,
            DocumentExtraction.superseded_at.is_(None),
        )
        .order_by(DocumentExtraction.started_at.desc(), DocumentExtraction.id.desc())
        .limit(1)
    )


def _document_out(session: Session, doc: Document) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        title=doc.title,
        doc_type=doc.doc_type,
        url=doc.url,
        financial_period=doc.financial_period,
        published_at=doc.published_at,
        retrieved_at=doc.retrieved_at,
        size_bytes=doc.size_bytes,
        latest_extraction=_summary(_latest_run(session, doc.id)),
    )


@router.get("/api/companies/{company_id}/documents", response_model=list[DocumentOut])
def company_documents(company_id: uuid.UUID, session: SessionDep) -> list[DocumentOut]:
    if session.get(Company, company_id) is None:
        raise HTTPException(status_code=404, detail="Company not found")
    docs = session.scalars(
        select(Document)
        .where(Document.company_id == company_id)
        .order_by(Document.retrieved_at.desc())
    ).all()
    return [_document_out(session, d) for d in docs]


@router.get("/api/documents/{document_id}/extraction", response_model=ExtractionDetail)
def document_extraction(document_id: uuid.UUID, session: SessionDep) -> ExtractionDetail:
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    run = _latest_run(session, document_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Document has not been processed yet")
    statements = session.scalars(
        select(ExtractedStatement)
        .where(ExtractedStatement.extraction_id == run.id)
        .order_by(ExtractedStatement.page_number, ExtractedStatement.id)
    ).all()
    reasons = session.execute(
        select(ExtractedFact.not_promoted_reason, func.count())
        .where(ExtractedFact.extraction_id == run.id, ExtractedFact.promoted.is_(False))
        .group_by(ExtractedFact.not_promoted_reason)
    ).all()
    return ExtractionDetail(
        document=_document_out(session, doc),
        issues=run.issues or [],
        statements=[
            StatementOut(
                page_number=s.page_number,
                statement=s.statement,
                basis=s.basis,
                unit=s.unit,
                title=s.title,
                detection=s.detection,
                columns=s.columns,
                rows_matched=s.rows_matched,
                rows_unmatched=s.rows_unmatched,
                checks=s.checks or [],
            )
            for s in statements
        ],
        not_promoted={reason or "unknown": n for reason, n in reasons},
    )


@router.get("/api/documents/{document_id}/pages/{page_number}", response_model=PageOut)
def document_page(document_id: uuid.UUID, page_number: int, session: SessionDep) -> PageOut:
    """Text of one page from the latest extraction, for citing evidence."""
    run = _latest_run(session, document_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Document has not been processed yet")
    page = session.scalar(
        select(DocumentPage).where(
            DocumentPage.extraction_id == run.id, DocumentPage.page_number == page_number
        )
    )
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return PageOut(
        document_id=document_id,
        page_number=page.page_number,
        text_method=page.text_method,
        needs_ocr=page.needs_ocr,
        text=page.text,
    )


class StorageStatus(BaseModel):
    root: str
    exists: bool
    writable: bool
    documents: int
    missing_files: list[str]
    hash_mismatches: list[str]
    unreferenced_files: int
    verified_hashes: bool
    healthy: bool


@router.get("/api/storage/status", response_model=StorageStatus)
def storage_status(session: SessionDep, verify_hashes: bool = False) -> StorageStatus:
    """Do the document rows and the files on disk agree? (Same answer locally and in Docker.)"""
    report = storage_report(session, verify_hashes=verify_hashes)
    return StorageStatus(**report.__dict__, healthy=report.healthy)
