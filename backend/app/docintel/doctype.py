"""Is a downloaded file really the kind of document its link claimed?

The fetcher types a file from its link text and file name. That is a guess: a "Secretarial
Compliance Report" on an annual-reports page, or a corporate brochure on a results page, inherits
the page's type. This module checks the claim before extraction and records any change.

Rules are deliberately narrow, because a wrongly retyped document is a lost filing:

* a title or file name that names a non-financial document (secretarial, governance, brochure,
  policy, certificate ...) is never a financial document, whatever page it was linked from;
* a file claimed to be an annual report must look like one: long, or carrying an annual-report title
  on its opening pages;
* nothing is judged from the ABSENCE of words (scanned or damaged text layers are common), and
  a file with no readable text keeps its claimed type and goes to OCR.

A retyped document keeps its row, hash and provenance; the change is written to the link-event log
with the original type, and the document is no longer extracted as a financial filing.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.docintel.pdf import first_pages_text, page_count
from app.models import Document, DocumentLinkEvent

NOT_FINANCIAL = re.compile(
    r"secretarial|compliance|governance|\bcg[\s_-]*report|brochure|company[\s_-]*profile|"
    r"certificate|code[\s_-]*of[\s_-]*conduct|polic(?:y|ies)|whistle|nomination|remuneration|"
    r"insider|related[\s_-]*party|shareholding[\s_-]*pattern|newspaper|advertisement|leaflet|"
    r"catalogue|datasheet|\bfaq\b|annual[\s_-]*return|unclaimed|grievance|credit[\s_-]*rating|"
    r"\bcsr\b|\besg\b|brsr|sustainab|responsib|climate|tcfd",
    re.I,
)
# the opening lines of a short compliance filing name it ("Report on Corporate Governance")
_NOT_FINANCIAL_HEAD = re.compile(
    r"secretarial\s+compliance\s+report|report\s+on\s+corporate\s+governance|"
    r"corporate\s+governance\s+report|corporate\s+brochure",
    re.I,
)
_ANNUAL_TITLE = re.compile(r"annual\s+report|integrated\s+report", re.I)
MIN_ANNUAL_REPORT_PAGES = 30
SMALL_FILE_PAGES = 15
HEAD_PAGES = 3
HEAD_CHARS = 700
FINANCIAL_TYPES = ("annual_report", "quarterly_result", "investor_presentation")


@dataclass
class Decision:
    doc_type: str
    changed: bool
    reason: str
    verified: bool  # True when the content positively supports the type, False when unknown


def inspect(path: Path) -> tuple[int, str]:
    """(page count, text of the opening pages); the text is empty for a scan.

    Raises ValueError for a file that cannot be read (the caller leaves it to the extractor).
    """
    pages = page_count(path)
    if pages is None:
        raise ValueError("not a readable PDF")
    head = " ".join(first_pages_text(path, HEAD_PAGES))
    return pages, " ".join(head.split())


def judge(claimed: str, title: str | None, url: str, pages: int, head: str) -> Decision:
    """The type this file should carry. Pure: no database, no file access."""
    if claimed not in FINANCIAL_TYPES:
        return Decision(claimed, False, "not a financial type; left as it is", False)
    name = url.rsplit("/", 1)[-1]
    subject = f"{title or ''} {name}"
    hit = NOT_FINANCIAL.search(subject)
    if hit:
        return Decision(
            "other",
            True,
            f"the title or file name says '{hit.group(0).lower()}', not a financial filing",
            True,
        )
    if pages <= SMALL_FILE_PAGES and _NOT_FINANCIAL_HEAD.search(head[:HEAD_CHARS]):
        return Decision(
            "other", True, "the opening lines name a compliance or governance report", True
        )
    if claimed == "annual_report" and head:
        if pages >= MIN_ANNUAL_REPORT_PAGES or _ANNUAL_TITLE.search(head):
            return Decision(claimed, False, "looks like an annual report", True)
        return Decision(
            "other",
            True,
            f"claimed as an annual report but it has {pages} pages and no annual-report title",
            True,
        )
    return Decision(claimed, False, "no reason to change the claimed type", bool(head))


def review_type(session: Session, document: Document, path: Path) -> Decision:
    """Check one stored document and apply a retype (with an audit event). Never raises."""
    try:
        pages, head = inspect(path)
    except Exception as exc:  # noqa: BLE001 - an unreadable file is the extractor's to report
        return Decision(document.doc_type, False, f"not inspected ({type(exc).__name__})", False)
    decision = judge(document.doc_type, document.title, document.url, pages, head)
    if decision.changed and decision.doc_type != document.doc_type:
        session.add(
            DocumentLinkEvent(
                document_id=document.id,
                company_before=document.company_id,
                company_after=document.company_id,
                action="RETYPED",
                reason=decision.reason,
                actor="system",
                evidence={"from": document.doc_type, "to": decision.doc_type, "pages": pages},
            )
        )
        document.doc_type = decision.doc_type
        session.flush()
    return decision
