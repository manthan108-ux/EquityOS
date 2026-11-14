"""The pipeline's stages. Each one calls the existing service for its job; nothing is reimplemented.

    classify   provider industry label + business description      providers.classification
    website    discover and verify the official website            acquisition.website_discovery
    upstox     refresh fundamentals                                 providers.upstox_refresh
    documents  fetch newly published public documents              acquisition.evidence_fetch
    extract    type-check, read and promote the stored documents   docintel (the `extract` command)
    evidence   sync provider facts + extractions into evidence,   evidence.engine.validate_company
               then validate
    calculate  derived metrics (growth, margins, valuation)         calc.engine.calculate_company
    enrich     verified quotes from the company's own pages         research.enrich
    readiness  evaluate research readiness and keep the snapshot    research.readiness
    report     regenerate an existing report when its inputs moved   research.report

A stage returns an `Outcome`. SUCCEEDED means the work was done (or was already current), SKIPPED
means there was nothing it could do and says why, FAILED means an error that is worth retrying.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Company,
    CompanyClassification,
    Document,
    DocumentExtraction,
    EvidenceRecord,
    FinancialFact,
    PipelineStageState,
    ProviderRefreshState,
    ResearchReport,
    ResearchSession,
    ShareholdingFact,
)
from app.models.pipeline import FAILED, SKIPPED, SUCCEEDED
from app.pipeline.state import Outcome

log = logging.getLogger("equityos.pipeline")

CLASSIFICATION_FRESH_DAYS = 90
WEBSITE_RECHECK_DAYS = 30  # a registered website is not re-fetched more often than this
MAX_EXTRACT_ATTEMPTS = 4  # a document that fails this many times in a row stops being retried


@dataclass
class Ctx:
    session: Session
    company: Company
    client: object = None  # PoliteClient
    llm: object = None
    upstox: object = None
    data_dir: Path | None = None
    now: datetime | None = None
    force: bool = False

    def __post_init__(self) -> None:
        self.now = self.now or datetime.now(UTC)


def _worked_at(prev) -> datetime | None:
    """When this stage last did real work (kept in its stored detail, so that skipping again
    does not push the date forward)."""
    raw = (getattr(prev, "detail", None) or {}).get("worked_at")
    try:
        when = datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return when if when is None or when.tzinfo else when.replace(tzinfo=UTC)


def _cooling(ctx: "Ctx", prev, days: int) -> tuple[bool, datetime | None]:
    """True when the stage did real work less than `days` ago (and nobody forced it)."""
    worked = _worked_at(prev)
    cooling = bool(
        worked
        and not ctx.force
        and getattr(prev, "status", None) == SUCCEEDED
        and (ctx.now - worked).days < days
    )
    return cooling, worked


def _hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


# ---- classify --------------------------------------------------------------------------------


def classify(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    from app.providers import classification

    upstox = ctx.upstox
    if upstox is None or not upstox.is_configured():
        return Outcome(SKIPPED, "skipped (Upstox not configured)")
    row = ctx.session.get(CompanyClassification, (ctx.company.id, classification.SCHEME))
    if (
        row is not None
        and row.description
        and (ctx.now - row.retrieved_at).days < CLASSIFICATION_FRESH_DAYS
    ):
        return Outcome(SKIPPED, f"skipped (label current: {row.label})")
    run = classification.backfill(ctx.session, upstox, [ctx.company])
    if run.stored:
        return Outcome(SUCCEEDED, "stored the provider's industry label and description")
    reason = ", ".join(run.skipped) or run.stopped or "nothing stored"
    return Outcome(SKIPPED, f"no label stored ({reason})")


# ---- website ---------------------------------------------------------------------------------


def website(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    from app.acquisition import website_discovery as wd

    cooling, worked = _cooling(ctx, prev, WEBSITE_RECHECK_DAYS)
    if cooling and ctx.company.website:
        return Outcome(
            SUCCEEDED,
            f"already registered; checked {worked:%d/%m/%Y}, not checked again for "
            f"{WEBSITE_RECHECK_DAYS} days",
            {"worked_at": worked.isoformat()},
        )
    res = wd.discover(ctx.session, ctx.company, ctx.client, ctx.llm, use_llm=ctx.llm is not None)
    message = f"{res.status}{(' - ' + res.note) if res.note else ''}"
    # A company with no findable site is a result, not a failure: the registry schedules the retry.
    registered = res.status in (wd.VERIFIED, wd.MANUAL)
    detail = {"worked_at": ctx.now.isoformat()} if registered else None
    return Outcome(SUCCEEDED if registered else SKIPPED, message, detail)


# ---- upstox ----------------------------------------------------------------------------------


def upstox(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    if ctx.upstox is None or not ctx.upstox.is_configured():
        return Outcome(SKIPPED, "skipped (Upstox not configured)")
    from app.providers import upstox_refresh as ur

    state = ctx.session.get(ProviderRefreshState, (ur.SOURCE_CODE, ctx.company.id))
    if state and state.next_due_at and state.next_due_at > datetime.now(UTC) and not ctx.force:
        return Outcome(SKIPPED, f"skipped (fresh until {state.next_due_at:%d/%m/%Y})")
    o = ur.refresh_company(ctx.session, ctx.upstox, ctx.company)
    status = FAILED if o.status == ur.ERROR else SUCCEEDED
    return Outcome(
        status, f"{o.status} {o.note}".strip(), error=o.note if status == FAILED else None
    )


# ---- documents -------------------------------------------------------------------------------


def documents(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    from app.acquisition import evidence_fetch as ef

    if not ctx.company.website:
        return Outcome(SKIPPED, "skipped (no verified website)")
    rep = ef.fetch_evidence(ctx.session, ctx.company, ctx.client, data_dir=ctx.data_dir)
    message = f"{rep.status} (candidates {rep.candidates}, downloaded {rep.downloaded})"
    detail = {"status": rep.status, "candidates": rep.candidates, "downloaded": rep.downloaded}
    if rep.status == ef.RECENT:  # inside the cool-down: nothing was attempted now; say why
        note = rep.notes[0] if rep.notes else "looked for recently"
        return Outcome(SKIPPED, f"{message} - {note}", detail)
    if rep.status in (ef.FETCHED, ef.UP_TO_DATE):
        return Outcome(SUCCEEDED, message, detail)
    if rep.status == ef.DOWNLOAD_FAILED:
        return Outcome(FAILED, message, detail, error=message)
    return Outcome(SKIPPED, message, detail)  # site unreachable / nothing found: reason in message


# ---- extract ---------------------------------------------------------------------------------


def extract(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    """Read every stored financial PDF that has not been read by the current extractor.

    The same code as `docintel.cli extract`: results kits are unpacked, a downloaded file is
    type-checked (a brochure typed as an annual report is retyped, with an audit event), then
    extracted and promoted. Documents already settled for this extractor version are not touched, so
    unchanged files are never read twice. Every outcome is recorded on the extraction row.
    """
    from app.core.storage import data_root, resolve_inside
    from app.docintel import doctype
    from app.docintel.archives import unpack_company_archives
    from app.docintel.pdf import ocr_available
    from app.docintel.pipeline import extract_document
    from app.docintel.provider import pending_documents
    from app.models import ExtractionStatus

    root = ctx.data_dir or data_root()
    session, company = ctx.session, ctx.company
    unpack_company_archives(session, company.id, root)
    session.commit()
    pending = pending_documents(session, company.id)
    if not pending:
        return Outcome(SKIPPED, "no document is waiting to be read")

    done: dict[str, int] = {}
    retyped, failed, skipped_failures = [], [], []
    for doc in pending:
        if doc.url.startswith("http"):  # fetched documents are type-checked; uploads are the user's
            verdict = doctype.review_type(session, doc, resolve_inside(root, doc.storage_path))
            session.commit()
            if verdict.changed:
                retyped.append(f"{doc.title or doc.url[-40:]}: {verdict.reason}")
                continue
        if _consecutive_failures(session, doc) >= MAX_EXTRACT_ATTEMPTS and not ctx.force:
            skipped_failures.append(str(doc.id))
            continue
        report = extract_document(session, doc, root, promote=True)
        session.commit()
        status = report.extraction.status
        done[status.value] = done.get(status.value, 0) + 1
        if status == ExtractionStatus.FAILED:
            failed.append(str(doc.id))
    detail = {
        "documents": len(pending),
        "by_status": done,
        "retyped": retyped,
        "failed_documents": failed,
        "gave_up_on": skipped_failures,
        "ocr_available": ocr_available(),
    }
    read = ", ".join(f"{n} {k.lower()}" for k, n in sorted(done.items())) or "none read"
    message = f"{len(pending)} document(s): {read}" + (
        f"; {len(retyped)} retyped as not financial" if retyped else ""
    )
    if failed:
        return Outcome(FAILED, message, detail, error=f"{len(failed)} extraction(s) failed")
    if done.get(ExtractionStatus.NEEDS_OCR.value):
        message += " (scanned pages need OCR, which is not installed here)"
    return Outcome(SUCCEEDED, message, detail)


def _consecutive_failures(session: Session, doc: Document) -> int:
    rows = list(
        session.scalars(
            select(DocumentExtraction.status)
            .where(DocumentExtraction.document_id == doc.id)
            .order_by(DocumentExtraction.id.desc())
            .limit(MAX_EXTRACT_ATTEMPTS)
        )
    )
    n = 0
    for status in rows:
        if getattr(status, "value", status) != "FAILED":
            break
        n += 1
    return n


# ---- evidence --------------------------------------------------------------------------------


def evidence_inputs(session: Session, company_id) -> dict:
    """What the evidence store is built from. If none of it changed, validation need not rerun."""
    facts = session.execute(
        select(func.count(), func.max(FinancialFact.retrieved_at)).where(
            FinancialFact.company_id == company_id
        )
    ).one()
    shares = session.execute(
        select(func.count(), func.max(ShareholdingFact.retrieved_at)).where(
            ShareholdingFact.company_id == company_id
        )
    ).one()
    extractions = session.execute(
        select(func.count(), func.max(DocumentExtraction.id))
        .join(Document, Document.id == DocumentExtraction.document_id)
        .where(Document.company_id == company_id, DocumentExtraction.superseded_at.is_(None))
    ).one()
    docs = session.scalar(
        select(func.count()).select_from(Document).where(Document.company_id == company_id)
    )
    week = date.today().isocalendar()[:2]  # staleness rules move with time: revalidate weekly
    return {
        "financial_facts": facts[0],
        "facts_at": str(facts[1]),
        "shareholding": shares[0],
        "extractions": extractions[0],
        "last_extraction": extractions[1],
        "documents": docs,
        "week": list(week),
    }


def statement_evidence_count(session: Session, company_id) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(EvidenceRecord)
            .where(
                EvidenceRecord.company_id == company_id,
                EvidenceRecord.kind == "FINANCIAL_STATEMENT",
                EvidenceRecord.status == "ACTIVE",
            )
        )
        or 0
    )


def evidence(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    from app.evidence.engine import validate_company

    inputs = evidence_inputs(ctx.session, ctx.company.id)
    fingerprint = _hash(sorted(inputs.items()))
    have = statement_evidence_count(ctx.session, ctx.company.id)
    # unchanged inputs AND the evidence is actually there: nothing to do
    if (
        prev is not None
        and prev.status == SUCCEEDED
        and prev.input_fingerprint == fingerprint
        and not ctx.force
        and (have > 0 or inputs["financial_facts"] == 0)
    ):
        when = prev.finished_at.strftime("%d/%m/%Y") if prev.finished_at else "earlier"
        return Outcome(
            SKIPPED, f"unchanged since {when}: nothing new to validate", fingerprint=fingerprint
        )
    validate_company(ctx.session, ctx.company.id)
    ctx.session.commit()
    after = statement_evidence_count(ctx.session, ctx.company.id)
    detail = {
        "provider_facts": inputs["financial_facts"],
        "statement_evidence": after,
        "statement_evidence_before": have,
        "extractions": inputs["extractions"],
    }
    # "validated" is the stage's result; what it made of the facts is in the detail
    return Outcome(SUCCEEDED, "validated", detail, fingerprint)


# ---- calculate -------------------------------------------------------------------------------


def calculate(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    """Derived metrics (growth, margins, returns, valuation) from the validated evidence.

    The same engine call as the research workflow's CALCULATION step. Without it a company
    processed in a batch would show "inputs are present but the calculation has not produced it".
    """
    from app.calc.engine import calculate_company

    have = statement_evidence_count(ctx.session, ctx.company.id)
    if have == 0:
        return Outcome(SKIPPED, "no validated statement evidence to calculate from")
    ev = ctx.session.get(PipelineStageState, (ctx.company.id, "evidence"))
    fingerprint = _hash(ev.input_fingerprint if ev else None, have, date.today().isocalendar()[:2])
    if (
        prev is not None
        and prev.status == SUCCEEDED
        and prev.input_fingerprint == fingerprint
        and not ctx.force
    ):
        return Outcome(
            SKIPPED, "inputs unchanged since the last calculation", fingerprint=fingerprint
        )
    rep = calculate_company(ctx.session, ctx.company.id, today=date.today(), validate_first=False)
    ctx.session.commit()
    return Outcome(
        SUCCEEDED,
        f"calculated {rep.calculated} metric(s), {len(rep.gaps)} gap(s)",
        {"calculated": rep.calculated, "gaps": len(rep.gaps)},
        fingerprint,
    )


# ---- enrich ----------------------------------------------------------------------------------


def enrich(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    from app.research import enrich as enrich_mod

    if ctx.llm is None or not ctx.company.website:
        return Outcome(SKIPPED, "skipped (no website or no language model)")
    # enrich_company only remembers a read when it stored something; a page that yielded nothing
    # verifiable would otherwise be fetched and sent to the model again on every run.
    cooling, worked = _cooling(ctx, prev, enrich_mod.REFRESH_DAYS)
    if cooling:
        return Outcome(
            SUCCEEDED,
            f"pages read {worked:%d/%m/%Y}; not read again for {enrich_mod.REFRESH_DAYS} days",
            {"worked_at": worked.isoformat()},
        )
    rep = enrich_mod.enrich_company(ctx.session, ctx.company, ctx.client, ctx.llm)
    message = f"stored {rep.stored}, discarded {rep.discarded}" + (
        f"; {rep.notes[0]}" if rep.notes else ""
    )
    return Outcome(SUCCEEDED, message, {"worked_at": ctx.now.isoformat()})


# ---- readiness -------------------------------------------------------------------------------


def readiness(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    from app.research import readiness as rd

    ready = rd.evaluate(ctx.session, ctx.company)
    counts: dict[str, int] = {}
    for s in ready.statuses:
        counts[s.status] = counts.get(s.status, 0) + 1
    missing = [
        {"requirement": s.req.id, "status": s.status, "reason": (s.reason or "")[:200]}
        for s in ready.statuses
        if s.status != rd.AVAILABLE
    ]
    detail = {
        "eligibility": ready.eligibility,
        "score": float(ready.score),
        "confidence": ready.confidence,
        "counts": counts,
        "reasons": ready.reasons[:6],
        "missing": missing[:25],
        "open_errors": ready.open_errors,
        "provider_backed": sum(1 for s in ready.statuses if s.ok and s.provider),
        "document_backed": sum(1 for s in ready.statuses if s.ok and s.document_id),
        "evaluated_at": ctx.now.isoformat(),
    }
    fingerprint = _hash(ready.eligibility, ready.score, sorted(counts.items()), ready.open_errors)
    return Outcome(
        SUCCEEDED,
        f"{ready.eligibility} {ready.score}/100 ({counts.get(rd.AVAILABLE, 0)} of "
        f"{len(ready.statuses)} available)",
        detail,
        fingerprint,
    )


# ---- report ----------------------------------------------------------------------------------


def report(ctx: Ctx, prev: PipelineStageState | None) -> Outcome:
    """Regenerate a report that already exists when the readiness behind it has changed.

    A report is a user's saved research; the pipeline never starts one. A regenerated report is a
    new version of the same session (the earlier one is kept); if nothing in the content changed,
    no new version is written.
    """
    from app.research import report as rp
    from app.research.report import ReportNotAllowed

    sess = ctx.session.scalar(
        select(ResearchSession)
        .join(ResearchReport, ResearchReport.session_id == ResearchSession.id)
        .where(ResearchSession.company_id == ctx.company.id)
        .order_by(ResearchReport.created_at.desc())
        .limit(1)
    )
    if sess is None:
        return Outcome(SKIPPED, "no report has been requested for this company")
    latest = ctx.session.scalar(
        select(ResearchReport)
        .where(ResearchReport.session_id == sess.id)
        .order_by(ResearchReport.version.desc())
        .limit(1)
    )
    ready_state = ctx.session.get(PipelineStageState, (ctx.company.id, "readiness"))
    fingerprint = ready_state.input_fingerprint if ready_state else None
    if (
        prev is not None
        and prev.status == SUCCEEDED
        and fingerprint is not None
        and prev.input_fingerprint == fingerprint
        and not ctx.force
    ):
        return Outcome(
            SKIPPED, "readiness unchanged since the last regeneration", fingerprint=fingerprint
        )
    try:
        new = rp.generate(ctx.session, sess, latest.mode, "pipeline")
    except ReportNotAllowed as exc:
        return Outcome(SKIPPED, f"not regenerated: {'; '.join(exc.reasons)[:200]}")
    ctx.session.commit()
    changed = new.id != latest.id
    return Outcome(
        SUCCEEDED,
        f"report v{new.version} ({'new version' if changed else 'content unchanged'})",
        {"version": new.version, "new_version": changed},
        fingerprint,
    )


STAGE_FUNCS = {
    "classify": classify,
    "website": website,
    "upstox": upstox,
    "documents": documents,
    "extract": extract,
    "evidence": evidence,
    "calculate": calculate,
    "enrich": enrich,
    "readiness": readiness,
    "report": report,
}
# which shared resource a stage uses; the runner limits concurrent use of each
GATES = {
    "classify": "upstox",
    "upstox": "upstox",
    "website": "web",
    "documents": "web",
    "enrich": "web",
    "extract": "cpu",
    "evidence": "cpu",
    "calculate": "cpu",
    "readiness": "cpu",
    "report": "cpu",
}
