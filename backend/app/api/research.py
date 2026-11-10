"""Research workflow API (Phase 11).

Every endpoint needs an API key (see `app.research.security`). Sessions, uploads and reports belong
to the client that created them. Uploading and report generation are rate limited, uploads are size
limited and checked by content. Heavy work (document extraction, the whole research run) happens in
background jobs; the browser polls the session / upload status.
"""

import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.models import Company, ResearchReport, ResearchSession, VaultEntry
from app.research import jobs, orchestrator, vault
from app.research import preferences as pf
from app.research import readiness as rd
from app.research import report as rp
from app.research import requirements as rq
from app.research.pdf import render as render_pdf
from app.research.security import Principal, current_principal, rate_limited
from app.services.company_resolver import MAX_QUERY_LENGTH, ResolutionStatus, resolve_company

router = APIRouter(prefix="/api/research", tags=["research"])
SessionDep = Annotated[Session, Depends(get_session)]
Caller = Annotated[Principal, Depends(current_principal)]
LimitedCaller = Annotated[Principal, Depends(rate_limited("write"))]
UploadCaller = Annotated[Principal, Depends(rate_limited("upload", per_hour=True))]

# Tests (and a single-process dev setup) can run jobs inline instead of on the worker pool.
INLINE = False
MAX_FILES_PER_REQUEST = 10
CHUNK = 1024 * 1024


class PreferencesIn(BaseModel):
    """What the user asked for. Omitted fields keep their defaults: consolidated basis, the latest
    available quarter, optional items off."""

    basis: str = "CONSOLIDATED"
    period_mode: str = "LATEST_QUARTER"
    fiscal_year: int | None = None
    quarter: str | None = None
    include_optional: bool = False

    def to_preferences(self) -> pf.Preferences:
        try:
            return pf.Preferences.from_dict(self.model_dump())
        except pf.PreferenceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


class StartRequest(BaseModel):
    query: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH)
    company_id: uuid.UUID | None = None
    refresh_sources: bool = False
    preferences: PreferencesIn | None = None


class ModeRequest(BaseModel):
    mode: str = Field(pattern="^(AVAILABLE_EVIDENCE|COMPLETE)$")


class ClarifyRequest(BaseModel):
    doc_type: str | None = None
    confirm_issuer: bool | None = None
    source_reference: str | None = Field(default=None, max_length=300)


def _own(session: Session, session_id: uuid.UUID, who: Principal) -> ResearchSession:
    sess = session.get(ResearchSession, session_id)
    if sess is None or (sess.owner != who.name and not who.is_admin):
        raise HTTPException(status_code=404, detail="no such research session")
    return sess


def _company(session: Session, company_id: uuid.UUID) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="no such company")
    return company


def _launch(session: Session, sess: ResearchSession, refresh: bool = False) -> None:
    if INLINE:
        orchestrator.run(session, sess, refresh=refresh)
    else:
        session.commit()
        jobs.submit_research(sess.id, refresh)


# ---------------------------------------------------------------------------- sessions


@router.post("/sessions", status_code=201)
def start_session(body: StartRequest, session: SessionDep, who: LimitedCaller) -> dict:
    """Resolve the company and start (or continue) its research. Never guesses between companies."""
    if body.company_id is not None:
        company = _company(session, body.company_id)
        query = company.common_name
    elif body.query and body.query.strip():
        resolution = resolve_company(session, body.query)
        if resolution.status is not ResolutionStatus.RESOLVED:
            names = []
            for c in resolution.candidates:
                co = session.get(Company, c.company_id)
                if co is not None:
                    names.append({"company_id": str(co.id), "name": co.common_name})
            raise HTTPException(
                status_code=409 if names else 404,
                detail={
                    "message": f"'{body.query}' did not resolve to exactly one company",
                    "status": resolution.status.value,
                    "candidates": names,
                    "notes": resolution.notes,
                },
            )
        company = session.get(Company, resolution.candidates[0].company_id)
        query = body.query.strip()
    else:
        raise HTTPException(status_code=422, detail="give a company name/ticker or a company_id")
    prefs = body.preferences.to_preferences() if body.preferences else None
    sess = orchestrator.create_session(session, company, who.name, query, prefs=prefs)
    session.commit()
    _launch(session, sess, body.refresh_sources)
    return orchestrator.status(session, sess)


@router.get("/sessions")
def list_sessions(
    session: SessionDep,
    who: Caller,
    company_id: uuid.UUID | None = None,
    only_open: bool = False,
    limit: int = Query(default=50, le=200),
) -> list[dict]:
    """The caller's research history (saved incomplete sessions included)."""
    stmt = select(ResearchSession).order_by(ResearchSession.updated_at.desc()).limit(limit)
    if not who.is_admin:
        stmt = stmt.where(ResearchSession.owner == who.name)
    if company_id:
        stmt = stmt.where(ResearchSession.company_id == company_id)
    if only_open:
        stmt = stmt.where(ResearchSession.status.in_(("AWAITING_DOCUMENTS", "RUNNING", "READY")))
    out = []
    for s in session.scalars(stmt):
        company = session.get(Company, s.company_id)
        reports = session.scalar(
            select(ResearchReport.version)
            .where(ResearchReport.session_id == s.id)
            .order_by(ResearchReport.version.desc())
            .limit(1)
        )
        out.append(
            {
                "id": str(s.id),
                "company_id": str(s.company_id),
                "company": company.common_name if company else None,
                "query": s.query,
                "status": s.status,
                "eligibility": s.eligibility,
                "readiness_score": None if s.readiness_score is None else float(s.readiness_score),
                "latest_report_version": reports,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
        )
    return out


@router.get("/sessions/{session_id}")
def get_session_status(session_id: uuid.UUID, session: SessionDep, who: Caller) -> dict:
    return orchestrator.status(session, _own(session, session_id, who))


@router.get("/sessions/{session_id}/readiness")
def get_readiness(session_id: uuid.UUID, session: SessionDep, who: Caller) -> dict:
    """The live research-readiness assessment (distinct from the financial score)."""
    sess = _own(session, session_id, who)
    return rd.evaluate(
        session, session.get(Company, sess.company_id), prefs=orchestrator.session_prefs(sess)
    ).as_dict()


@router.get("/sessions/{session_id}/missing")
def get_missing(session_id: uuid.UUID, session: SessionDep, who: Caller) -> dict:
    """The Missing Data Assessment: what is missing, why it matters and what to supply."""
    sess = _own(session, session_id, who)
    ready = rd.evaluate(
        session, session.get(Company, sess.company_id), prefs=orchestrator.session_prefs(sess)
    ).as_dict()
    resolved = [r for r in ready["requirements"] if r["status"] == "AVAILABLE"]
    return {
        "eligibility": ready["eligibility"],
        "reasons": ready["reasons"],
        "missing": ready["missing"],
        "resolved": [
            {"id": r["id"], "label": r["label"], "importance": r["importance"], "found": r["found"]}
            for r in resolved
        ],
        "documents_held": ready["documents_held"],
        "options": {
            "upload": "upload the documents listed under 'document' for each missing item",
            "continue_with_available": ready["eligibility"] != rd.BLOCKED,
        },
    }


@router.get("/sessions/{session_id}/preferences")
def get_preferences(session_id: uuid.UUID, session: SessionDep, who: Caller) -> dict:
    """The session's current preferences and the log of every earlier choice."""
    sess = _own(session, session_id, who)
    prefs = orchestrator.session_prefs(sess)
    return {
        "current": {**prefs.to_dict(), "period_label": prefs.period_label()},
        "log": sess.preference_log or [],
        "options": {
            "basis": list(pf.BASES),
            "period_mode": list(pf.PERIOD_MODES),
            "quarter": list(pf.QUARTERS),
        },
    }


@router.put("/sessions/{session_id}/preferences")
def put_preferences(
    session_id: uuid.UUID, body: PreferencesIn, session: SessionDep, who: LimitedCaller
) -> dict:
    """Change the basis, period or optional items. The change is logged, never silent, and the
    research is re-evaluated under it (stored work is reused, not repeated)."""
    sess = _own(session, session_id, who)
    changed = orchestrator.set_preferences(session, sess, body.to_preferences(), who.name)
    session.commit()
    if changed:
        _launch(session, sess)
    return orchestrator.status(session, sess)


@router.post("/sessions/{session_id}/resume")
def resume(
    session_id: uuid.UUID, session: SessionDep, who: LimitedCaller, refresh_sources: bool = False
) -> dict:
    sess = _own(session, session_id, who)
    _launch(session, sess, refresh_sources)
    return orchestrator.status(session, sess)


@router.post("/sessions/{session_id}/continue")
def continue_with_available(session_id: uuid.UUID, session: SessionDep, who: LimitedCaller) -> dict:
    """Proceed with what is available and accept the disclosed limitations."""
    sess = _own(session, session_id, who)
    orchestrator.choose_available_evidence(session, sess)
    _launch(session, sess)
    return orchestrator.status(session, sess)


@router.get("/template")
def table_template(who: Caller) -> dict:
    """The format a CSV/XLSX financials upload must follow."""
    return vault.template()


@router.get("/requirements/{company_id}")
def company_requirements(company_id: uuid.UUID, session: SessionDep, who: Caller) -> dict:
    """The data requirements that apply to this company (for transparency)."""
    company = _company(session, company_id)
    profile = rq.profile_for(
        company.sector.code if company.sector else None,
        company.industry.code if company.industry else None,
    )
    th = rq.thresholds_for(profile)
    return {
        "profile": profile,
        "version": rq.VERSION,
        "thresholds": {"min_score": th.min_score, "min_component": th.min_component},
        "requirements": [
            {
                "id": r.id,
                "label": r.label,
                "importance": r.importance,
                "pillar": r.pillar,
                "metrics": list(r.metrics),
                "period": r.period,
                "max_age_days": r.max_age_days,
                "sections": list(r.sections),
                "extractor": r.extractor,
                "upload_helps": r.upload_helps,
            }
            for r in rq.requirements_for(profile)
        ],
    }


# ---------------------------------------------------------------------------- uploads


async def _read_limited(upload: UploadFile) -> bytes:
    limit = vault.max_upload_bytes()
    data = bytearray()
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(
                status_code=413, detail=f"'{upload.filename}' is larger than the upload limit"
            )
    return bytes(data)


@router.post("/sessions/{session_id}/uploads", status_code=202)
async def upload_documents(
    session_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    who: UploadCaller,
    files: Annotated[list[UploadFile], File()],
    licence: Annotated[str, Form()] = "UNDECLARED",
    doc_type: Annotated[str | None, Form()] = None,
) -> dict:
    """Upload one or several PDF/XLSX/CSV files. Each is checked at once (format, size, duplicate,
    issuer) and then processed in the background; poll `GET /uploads/{id}` for the outcome."""
    sess = _own(session, session_id, who)
    if not files:
        raise HTTPException(status_code=422, detail="no file was sent")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=422, detail=f"send at most {MAX_FILES_PER_REQUEST} files at a time"
        )
    declared = request.headers.get("content-length")
    if (
        declared
        and declared.isdigit()
        and int(declared) > vault.max_upload_bytes() * MAX_FILES_PER_REQUEST
    ):
        raise HTTPException(status_code=413, detail="the request is too large")
    company = session.get(Company, sess.company_id)
    entries = []
    for f in files:
        body = await _read_limited(f)
        entry = vault.receive_upload(
            session,
            company,
            owner=who.name,
            filename=f.filename or "upload",
            body=body,
            licence=licence,
            doc_type=doc_type,
            session_id=sess.id,
        )
        entries.append(entry)
    session.commit()
    for entry in entries:
        if entry.status == vault.RECEIVED:
            if INLINE:
                vault.process_entry(session, entry.id)
                session.commit()
                if entry.status in (vault.ACCEPTED, vault.HELD_PRIVATE):
                    orchestrator.run(session, sess)
            else:
                jobs.submit_upload(entry.id)
    return {"uploads": [vault.entry_out(e, who.name, who.is_admin) for e in entries]}


@router.get("/sessions/{session_id}/uploads")
def session_uploads(session_id: uuid.UUID, session: SessionDep, who: Caller) -> list[dict]:
    sess = _own(session, session_id, who)
    rows = session.scalars(
        select(VaultEntry)
        .where(VaultEntry.company_id == sess.company_id, VaultEntry.uploaded_by == who.name)
        .order_by(VaultEntry.uploaded_at.desc())
    )
    return [vault.entry_out(e, who.name, who.is_admin) for e in rows]


def _entry(session: Session, entry_id: uuid.UUID, who: Principal) -> VaultEntry:
    entry = session.get(VaultEntry, entry_id)
    if entry is None or not vault.can_view(entry, who.name, who.is_admin):
        raise HTTPException(status_code=404, detail="no such upload")
    return entry


@router.get("/uploads/{entry_id}")
def get_upload(entry_id: uuid.UUID, session: SessionDep, who: Caller) -> dict:
    """Validation status of one upload."""
    return vault.entry_out(_entry(session, entry_id, who), who.name, who.is_admin)


@router.post("/uploads/{entry_id}/clarify")
def clarify_upload(
    entry_id: uuid.UUID, body: ClarifyRequest, session: SessionDep, who: LimitedCaller
) -> dict:
    """Answer the questions an ambiguous upload raised; it is then processed again."""
    entry = _entry(session, entry_id, who)
    if entry.uploaded_by != who.name and not who.is_admin:
        raise HTTPException(status_code=404, detail="no such upload")
    answers = body.model_dump(exclude_none=True)
    try:
        entry = vault.clarify(session, entry_id, answers)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    if entry.status in (vault.ACCEPTED, vault.HELD_PRIVATE) and entry.session_id:
        sess = session.get(ResearchSession, entry.session_id)
        if sess is not None:
            _launch(session, sess)
    return vault.entry_out(entry, who.name, who.is_admin)


@router.post("/uploads/{entry_id}/reprocess")
def reprocess_upload(entry_id: uuid.UUID, session: SessionDep, who: LimitedCaller) -> dict:
    """Run an upload through the checks and extraction again (for example after a failure)."""
    entry = _entry(session, entry_id, who)
    if entry.uploaded_by != who.name and not who.is_admin:
        raise HTTPException(status_code=404, detail="no such upload")
    try:
        entry = vault.reprocess(session, entry_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    if entry.status in (vault.ACCEPTED, vault.HELD_PRIVATE) and entry.session_id:
        sess = session.get(ResearchSession, entry.session_id)
        if sess is not None:
            _launch(session, sess)
    return vault.entry_out(entry, who.name, who.is_admin)


@router.get("/companies/{company_id}/vault")
def company_vault(company_id: uuid.UUID, session: SessionDep, who: Caller) -> list[dict]:
    """The company's evidence vault: validated public filings, and your own uploads."""
    _company(session, company_id)
    return [
        vault.entry_out(e, who.name, who.is_admin)
        for e in vault.list_vault(session, company_id, who.name, who.is_admin)
    ]


# ---------------------------------------------------------------------------- reports


def _report_out(r: ResearchReport, with_content: bool = False) -> dict:
    out = {
        "id": str(r.id),
        "session_id": str(r.session_id),
        "company_id": str(r.company_id),
        "version": r.version,
        "mode": r.mode,
        "readiness_score": None if r.readiness_score is None else float(r.readiness_score),
        "missing_critical": r.missing_critical,
        "missing_important": r.missing_important,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "complete": r.mode == rp.COMPLETE,
    }
    if with_content:
        out["content"] = r.content
    return out


@router.post("/sessions/{session_id}/reports", status_code=201)
def generate_report(
    session_id: uuid.UUID, body: ModeRequest, session: SessionDep, who: LimitedCaller
) -> dict:
    """AVAILABLE_EVIDENCE: now, with every gap disclosed. COMPLETE: only if the evidence allows."""
    sess = _own(session, session_id, who)
    try:
        report = rp.generate(session, sess, body.mode, who.name)
    except rp.ReportNotAllowed as exc:
        raise HTTPException(
            status_code=409, detail={"message": "report not allowed", "reasons": exc.reasons}
        ) from exc
    if report.mode == rp.COMPLETE:
        sess.status = "COMPLETED"  # an available-evidence report leaves the session open
    session.commit()
    return _report_out(report, with_content=True)


@router.get("/sessions/{session_id}/reports")
def report_history(session_id: uuid.UUID, session: SessionDep, who: Caller) -> list[dict]:
    sess = _own(session, session_id, who)
    rows = session.scalars(
        select(ResearchReport)
        .where(ResearchReport.session_id == sess.id)
        .order_by(ResearchReport.version.desc())
    )
    return [_report_out(r) for r in rows]


@router.get("/reports")
def all_reports(
    session: SessionDep, who: Caller, company_id: uuid.UUID | None = None, limit: int = 50
) -> list[dict]:
    """Report version history across the caller's sessions."""
    stmt = (
        select(ResearchReport)
        .join(ResearchSession, ResearchSession.id == ResearchReport.session_id)
        .order_by(ResearchReport.created_at.desc())
        .limit(min(limit, 200))
    )
    if not who.is_admin:
        stmt = stmt.where(ResearchSession.owner == who.name)
    if company_id:
        stmt = stmt.where(ResearchReport.company_id == company_id)
    return [_report_out(r) for r in session.scalars(stmt)]


@router.get("/reports/{report_id}/pdf")
def get_report_pdf(
    report_id: uuid.UUID,
    session: SessionDep,
    who: Annotated[Principal, Depends(rate_limited("pdf"))],
) -> Response:
    """The stored report rendered as a PDF (a rendering of the stored content, nothing added)."""
    report = session.get(ResearchReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="no such report")
    _own(session, report.session_id, who)
    body = render_pdf(
        report.content,
        version=report.version,
        mode=report.mode,
        report_id=str(report.id),
        content_hash=report.content_hash,
    )
    name = f"equityos-report-v{report.version}-{str(report.id)[:8]}.pdf"
    return Response(
        content=body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/reports/{report_id}")
def get_report(report_id: uuid.UUID, session: SessionDep, who: Caller) -> dict:
    report = session.get(ResearchReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="no such report")
    _own(session, report.session_id, who)
    return _report_out(report, with_content=True)
