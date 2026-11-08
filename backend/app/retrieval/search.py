"""Page-cited search over the text of a company's stored documents.

The research agents (Phase 9) never open PDFs and never browse: they ask this module a question
and get back pages, each with its document, page number and a short snippet, so every statement
they make can cite where it came from. It is a plain PostgreSQL full-text search (no model, no
embeddings, no external service), so the same question always returns the same pages.

Rules built in:

* only the company's own documents (a document whose issuer check failed is excluded) and only the
  CURRENT extraction run of each document;
* the query is parsed by PostgreSQL's `websearch_to_tsquery` (quotes for phrases, `-` to exclude,
  `or`), so user or model text can never inject query syntax;
* every result is marked UNTRUSTED. Page text is quoted material from a document, never
  instructions; a sentence on a page that tells a reader what to do is data like any other text;
* a hit is a lead to a page, not a number: figures still come only from the evidence store.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func as sa_func
from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from app.models import Document, DocumentExtraction, DocumentPage

MAX_QUERY_CHARS = 300
MAX_LIMIT = 50
_SNIPPET_OPTIONS = "MaxFragments=2, MaxWords=40, MinWords=12, StartSel=<<, StopSel=>>"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class SearchError(ValueError):
    pass


@dataclass(frozen=True)
class Hit:
    document_id: uuid.UUID
    title: str
    doc_type: str
    url: str
    page_number: int
    rank: float
    snippet: str  # matches are wrapped in << >>
    text_method: str  # text_layer | embedded_ocr | ocr
    untrusted: bool = True

    @property
    def citation(self) -> str:
        return f"{self.title}, p.{self.page_number}"


def clean_query(query: str) -> str:
    text = _CONTROL.sub(" ", query or "").strip()
    if not text:
        raise SearchError("the query is empty")
    if len(text) > MAX_QUERY_CHARS:
        raise SearchError(f"the query is longer than {MAX_QUERY_CHARS} characters")
    return text


def search_company_documents(
    session: Session,
    company_id: uuid.UUID,
    query: str,
    *,
    limit: int = 10,
    doc_types: list[str] | None = None,
    document_id: uuid.UUID | None = None,
) -> list[Hit]:
    """The best-matching pages, best first (at most one hit per page)."""
    text = clean_query(query)
    limit = min(max(limit, 1), MAX_LIMIT)
    english = literal_column("'english'")
    vector = sa_func.to_tsvector(english, DocumentPage.text)  # the same expression as the index
    tsquery = sa_func.websearch_to_tsquery(english, text)
    rank = sa_func.ts_rank_cd(vector, tsquery)
    stmt = (
        select(
            DocumentPage,
            Document,
            rank.label("rank"),
            sa_func.ts_headline(english, DocumentPage.text, tsquery, _SNIPPET_OPTIONS).label(
                "snip"
            ),
        )
        .join(DocumentExtraction, DocumentExtraction.id == DocumentPage.extraction_id)
        .join(Document, Document.id == DocumentExtraction.document_id)
        .where(
            Document.company_id == company_id,
            Document.association_status != "MISMATCH",
            DocumentExtraction.superseded_at.is_(None),
            vector.op("@@")(tsquery),
        )
        .order_by(rank.desc(), Document.retrieved_at.desc(), DocumentPage.page_number)
        .limit(limit)
    )
    if doc_types:
        stmt = stmt.where(Document.doc_type.in_(doc_types))
    if document_id is not None:
        stmt = stmt.where(Document.id == document_id)
    hits = []
    for page, document, score, snippet in session.execute(stmt):
        hits.append(
            Hit(
                document_id=document.id,
                title=document.title or document.url,
                doc_type=document.doc_type,
                url=document.url,
                page_number=page.page_number,
                rank=float(score),
                snippet=" ".join(snippet.split()),
                text_method=page.text_method,
            )
        )
    return hits


__all__ = [
    "Hit",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "SearchError",
    "clean_query",
    "search_company_documents",
]
