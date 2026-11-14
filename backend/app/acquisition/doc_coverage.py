"""Annual-report coverage per company and fiscal year, with an honest reason for every gap.

For each target fiscal year (the latest four) a company either HAS an annual report stored, or is
MISSING one, and a missing year carries a reason taken from what the last collection run actually
saw (the site refuses automated access, robots.txt disallows the files, the pages list no files, the
report for that year is not linked). A gap is never worked around: it is left for the user to supply
when a report is generated. Nothing here fetches anything.
"""

import re
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Company,
    Document,
    DocumentExtraction,
    EvidenceFetchRun,
    Security,
)
from app.providers.company_ir import guess_period

TARGET_YEARS = 4
_FY = re.compile(r"FY(\d{4})")


def latest_fiscal_year(today: date) -> int:
    """The newest fiscal year whose annual report should exist (March year end, out by August)."""
    return today.year if today.month >= 8 else today.year - 1


def target_years(today: date, count: int = TARGET_YEARS) -> list[str]:
    latest = latest_fiscal_year(today)
    return [f"FY{latest - i}" for i in range(count)]


def fiscal_year_of(doc: Document) -> str | None:
    """The fiscal year an annual report covers: the recorded period, else title or file name."""
    if doc.financial_period and _FY.fullmatch(doc.financial_period):
        return doc.financial_period
    hint = guess_period(f"{doc.title or ''} {doc.url}")
    return hint if hint and _FY.fullmatch(hint) else None


def _reason(run: EvidenceFetchRun | None, has_any: bool) -> tuple[str, str]:
    """(code, plain-language reason) for a missing year, from the last collection run."""
    if run is None:
        return "NOT_LOOKED_FOR", "documents have not been looked for on this company's website yet"
    detail = run.detail or {}
    notes = (
        " | ".join(detail.get("notes") or [])
        + " "
        + " ".join((detail.get("deep") or {}).get("blocked", []))
    )
    if run.status == "NO_WEBSITE":
        return "NO_WEBSITE", "no website is recorded for this company"
    if "ACCESS_DENIED" in notes or "HTTP 403" in notes:
        return (
            "SITE_REFUSES",
            "the site refuses automated access (HTTP 403); this is not worked around",
        )
    if "BLOCKED_BY_ROBOTS" in notes:
        return (
            "ROBOTS_DISALLOW",
            "the site's robots.txt disallows the report files; they are not fetched",
        )
    if run.status == "SITE_UNREACHABLE":
        return "SITE_UNREACHABLE", "the website could not be reached when the last search ran"
    if run.status == "NOTHING_FOUND" or not has_any:
        return (
            "NOT_IN_STATIC_PAGES",
            "no report files were found on the static pages (the list is probably loaded by "
            "script, or the reports are published elsewhere)",
        )
    return "NOT_LINKED", "the website's pages list no annual report for this year"


def _download_reason(detail: str) -> tuple[str, str]:
    """Why a report that was found could not be stored."""
    if "403" in detail or "access denied" in detail.lower():
        return (
            "DOWNLOAD_REFUSED",
            "the report is linked but the site refuses the download (HTTP 403); not worked around",
        )
    return (
        "DOWNLOAD_FAILED",
        f"the report is linked but the site did not deliver it ({detail[:60] or 'no detail'})",
    )


def company_coverage(session: Session, company: Company, today: date | None = None) -> dict:
    today = today or date.today()
    symbol = session.scalar(
        select(Security.symbol).where(Security.company_id == company.id).order_by(Security.symbol)
    )
    docs = list(
        session.scalars(
            select(Document).where(
                Document.company_id == company.id,
                Document.doc_type == "annual_report",
                Document.association_status != "MISMATCH",
            )
        )
    )
    extractions: dict[uuid.UUID, str] = {}
    if docs:
        for doc_id, status in session.execute(
            select(DocumentExtraction.document_id, DocumentExtraction.status).where(
                DocumentExtraction.document_id.in_([d.id for d in docs]),
                DocumentExtraction.superseded_at.is_(None),
            )
        ):
            extractions[doc_id] = getattr(status, "value", str(status))
    by_year: dict[str, list[Document]] = {}
    unassigned: list[Document] = []
    for d in docs:
        fy = fiscal_year_of(d)
        (by_year.setdefault(fy, []) if fy else unassigned).append(d)
    run = session.scalar(
        select(EvidenceFetchRun)
        .where(EvidenceFetchRun.company_id == company.id)
        .order_by(EvidenceFetchRun.started_at.desc())
        .limit(1)
    )
    targets = target_years(today)

    def describe(d: Document) -> dict:
        return {
            "document_id": str(d.id),
            "title": d.title,
            "source_url": d.url,
            "retrieved_at": d.retrieved_at.isoformat() if d.retrieved_at else None,
            "published_at": d.published_at.isoformat() if d.published_at else None,
            "financial_period": d.financial_period,
            "sha256": d.sha256,
            "extraction": extractions.get(d.id, "NOT_READ"),
        }

    code, why = _reason(run, bool(docs))
    # A report that WAS found but could not be downloaded has a more exact reason.
    refused: dict[str, str] = {}
    for o in (run.detail or {}).get("outcomes", []) if run else []:
        if (
            o.get("status") == "FAILED"
            and o.get("period_hint")
            and o.get("kind") == "annual_report"
        ):
            refused.setdefault(o["period_hint"], o.get("detail") or "")
    years = []
    for fy in targets:
        held = by_year.get(fy, [])
        entry: dict = {"fy": fy, "status": "HAVE" if held else "MISSING"}
        if held:
            entry["documents"] = [describe(d) for d in held]
        else:
            if fy in refused:
                entry["reason_code"], entry["reason"] = _download_reason(refused[fy])
            else:
                entry["reason_code"], entry["reason"] = code, why
            # Nobody is asked to fetch it by hand: a report that cannot be collected automatically
            # is requested from the user when a report is generated (the upload step).
            entry["user_supplied"] = (
                f"not collected automatically; the {fy} annual report can be uploaded by the user "
                "when a report is generated"
            )
        years.append(entry)
    have = sum(1 for y in years if y["status"] == "HAVE")
    return {
        "company": company.common_name,
        "symbol": symbol,
        "targets": targets,
        "have": have,
        "missing": len(targets) - have,
        "years": years,
        "older_held": sorted((fy for fy in by_year if fy not in targets), reverse=True),
        "period_unknown": [describe(d) for d in unassigned],
        "last_search": {
            "at": run.finished_at.isoformat() if run and run.finished_at else None,
            "status": run.status if run else None,
        },
    }
