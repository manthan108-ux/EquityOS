"""The research orchestrator: one entry point that takes a company to a report, or to an honest
request for what is missing.

It composes the engines that already exist; it never recalculates, re-scores or re-extracts on its
own. The ten stages follow the product specification:

 1 IDENTITY    resolve the company and check it against the stored identity registry
 2 SOURCES     (optional) ask the permitted providers for statements, corporate actions, holdings
 3 DOCUMENTS   inventory of stored documents; unpack results kits, ingest the inbox folder
 4 EXTRACTION  run document intelligence on documents not processed yet
 5 EVIDENCE    synchronise and validate the evidence store
 6 CALCULATION run the calculation engine
 7 PEERS       sector classification, peer selection and peer statistics
 8 SCORING     the scoring engine (relative / absolute / unavailable, as the evidence allows)
 9 READINESS   evaluate the data requirements; decide AUTOMATIC / ASSISTED / BLOCKED
10 REPORT      generate the report, or defer and ask

State: each stage is a row in `research_steps` holding the fingerprint of the evidence AFTER it ran.
On resume a stage whose fingerprint still matches the current evidence is skipped, so an interrupted
or repeated run does only the work whose inputs have changed. A stage that failed is retried; a
stage found RUNNING after a crash (older than `STALE_RUNNING`) is treated as failed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, ResearchSession, ResearchStep, Security
from app.research import clock
from app.research import preferences as pf
from app.research import readiness as rd
from app.research import report as rp
from app.research import requirements as rq

STEPS = (
    "IDENTITY",
    "SOURCES",
    "DOCUMENTS",
    "EXTRACTION",
    "EVIDENCE",
    "CALCULATION",
    "PEERS",
    "SCORING",
    "READINESS",
    "REPORT",
)
STEP_LABELS = {
    "IDENTITY": "Resolve company identity",
    "SOURCES": "Retrieve data from permitted sources",
    "DOCUMENTS": "Inventory documents",
    "EXTRACTION": "Extract figures from documents",
    "EVIDENCE": "Validate evidence",
    "CALCULATION": "Calculate financial metrics",
    "PEERS": "Select peers and compare",
    "SCORING": "Score the company",
    "READINESS": "Evaluate research readiness",
    "REPORT": "Generate or defer the report",
}
STALE_RUNNING = timedelta(minutes=30)
SOURCE_DATASETS = (
    "INCOME_ANNUAL",
    "INCOME_QUARTERLY",
    "BALANCE_SHEET",
    "CASH_FLOW",
    "SHAREHOLDING",
    "CORPORATE_ACTIONS",
)
CONTINUE_WITH_AVAILABLE = "CONTINUE_WITH_AVAILABLE"


@dataclass
class Context:
    session: Session
    sess: ResearchSession
    company: Company
    refresh: bool
    today: date
    router_factory: Callable | None = None
    data_dir: object | None = None
    llm_router: object | None = None
    document_fetcher: Callable | None = None


def session_prefs(sess: ResearchSession) -> pf.Preferences:
    """The preferences a session was started with (or last changed to)."""
    return pf.Preferences.from_dict(sess.preferences or {})


def set_preferences(
    session: Session, sess: ResearchSession, prefs: pf.Preferences, by: str
) -> bool:
    """Record a preference choice. Every change is appended to the session's log, so a saved
    session can always say which basis and period each stage of it was researched under."""
    current = sess.preferences or None
    if current and pf.Preferences.from_dict(current) == prefs:
        return False
    sess.preference_log = [
        *(sess.preference_log or []),
        {
            "at": datetime.now(UTC).isoformat(),
            "by": by,
            "from": current,
            "to": prefs.to_dict(),
        },
    ]
    sess.preferences = prefs.to_dict()
    session.flush()
    return True


def create_session(
    session: Session,
    company: Company,
    owner: str,
    query: str,
    *,
    resume_open: bool = True,
    prefs: pf.Preferences | None = None,
) -> ResearchSession:
    """A new research session; an unfinished one by the same owner for the same company is reused
    so the user's progress is not duplicated. Explicit preferences are recorded either way."""
    if resume_open:
        existing = session.scalar(
            select(ResearchSession)
            .where(
                ResearchSession.company_id == company.id,
                ResearchSession.owner == owner,
                ResearchSession.status.in_(("CREATED", "RUNNING", "AWAITING_DOCUMENTS", "READY")),
            )
            .order_by(ResearchSession.updated_at.desc())
            .limit(1)
        )
        if existing is not None:
            if prefs is not None:
                set_preferences(session, existing, prefs, owner)
            return existing
    sess = ResearchSession(company_id=company.id, owner=owner, query=query[:200], status="CREATED")
    session.add(sess)
    session.flush()
    set_preferences(session, sess, prefs or pf.DEFAULT, owner)
    return sess


def _step(session: Session, sess: ResearchSession, name: str) -> ResearchStep:
    step = session.scalar(
        select(ResearchStep).where(ResearchStep.session_id == sess.id, ResearchStep.step == name)
    )
    if step is None:
        step = ResearchStep(session_id=sess.id, step=name, status="PENDING", attempts=0)
        session.add(step)
        session.flush()
    return step


# ---------------------------------------------------------------------------- the stages


def _identity(ctx: Context) -> dict:
    from app.seeds.identity import verify_identity

    securities = list(
        ctx.session.scalars(select(Security).where(Security.company_id == ctx.company.id))
    )
    problems = [p for p in verify_identity(ctx.session) if ctx.company.common_name in str(p)]
    return {
        "company": ctx.company.common_name,
        "symbols": sorted({s.symbol for s in securities}),
        "isins": sorted({s.isin for s in securities}),
        "identity_problems": [str(p) for p in problems],
    }


def _sources(ctx: Context) -> dict:
    if not ctx.refresh:
        return {"skipped": "source refresh was not requested; stored data is used"}
    if ctx.router_factory is None:
        return {"skipped": "no provider router is configured"}
    from app.providers.base import Dataset
    from app.providers.cli import company_ref

    router = ctx.router_factory()
    ref = company_ref(ctx.session, ctx.company)
    outcomes = {}
    for name in SOURCE_DATASETS:
        route = router.acquire(ctx.session, ref, Dataset[name])
        outcomes[name] = {
            "obtained": route.ok,
            "provider": route.chosen.provider if route.chosen else None,
            "trail": route.explain(),
        }
        ctx.session.commit()
    return {"datasets": outcomes}


def _fetch_public_documents(ctx: Context, root) -> dict | None:
    """Ask the document fetcher (if one is configured) for the company's public documents.

    A site or network problem never fails the research: it is recorded as the reason evidence is
    missing, and the run carries on with what is already stored.
    """
    if ctx.document_fetcher is None:
        return None
    try:
        return ctx.document_fetcher(ctx.session, ctx.company, data_dir=root).as_dict()
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        return {"status": "ERROR", "notes": [f"{type(exc).__name__} while fetching documents"]}


def _documents(ctx: Context) -> dict:
    from app.core.config import get_settings, resolve_data_dir
    from app.docintel.archives import unpack_company_archives
    from app.docintel.inbox import ingest_inbox

    root = ctx.data_dir or resolve_data_dir(get_settings())
    symbol = next((s.symbol for s in ctx.company.securities if s.security_code is None), None) or (
        ctx.company.securities[0].symbol if ctx.company.securities else None
    )
    unpacked = 0
    for _archive, outcomes in unpack_company_archives(ctx.session, ctx.company.id, root):
        unpacked += sum(1 for o in outcomes if o.status == "STORED")
    fetched = _fetch_public_documents(ctx, root)
    inbox = ingest_inbox(ctx.session, ctx.company.id, root, symbol) if symbol else []
    held = rd._held_documents(ctx.session, ctx.company.id)
    return {
        "evidence_fetch": fetched,
        "documents_held": len(held),
        "by_type": {t: sum(1 for d in held if d["doc_type"] == t) for t in rd.HELD_TYPES},
        "unpacked_from_archives": unpacked,
        "inbox": [{"file": o.file, "status": o.status, "detail": o.detail} for o in inbox],
    }


def _extraction(ctx: Context) -> dict:
    from app.core.config import get_settings, resolve_data_dir
    from app.docintel.pipeline import extract_document
    from app.docintel.provider import pending_documents

    root = ctx.data_dir or resolve_data_dir(get_settings())
    pending = pending_documents(ctx.session, ctx.company.id)
    results = []
    for doc in pending:
        run = extract_document(ctx.session, doc, root, llm_router=ctx.llm_router).extraction
        ctx.session.flush()
        results.append(
            {
                "document_id": str(doc.id),
                "type": doc.doc_type,
                "status": run.status.value,
                "facts_promoted": run.facts_promoted,
            }
        )
    return {"processed": len(results), "documents": results}


def _evidence(ctx: Context) -> dict:
    from app.evidence.engine import validate_company

    report = validate_company(ctx.session, ctx.company.id, today=ctx.today)
    return {
        "errors": getattr(report, "errors", None),
        "warnings": getattr(report, "warnings", None),
    }


def _calculation(ctx: Context) -> dict:
    from app.calc.engine import calculate_company

    report = calculate_company(ctx.session, ctx.company.id, today=ctx.today, validate_first=False)
    return {"report": str(getattr(report, "summary", "") or "")[:300] or "calculated"}


def _peers(ctx: Context) -> dict:
    from app.sector.engine import analyze_company

    report = analyze_company(ctx.session, ctx.company.id, today=ctx.today)
    peer_set = getattr(report, "peer_set", None)
    return {"peers": getattr(peer_set, "peer_count", None)}


def _scoring(ctx: Context) -> dict:
    from app.scoring import engine as scoring

    out = scoring.score_company(ctx.session, ctx.company, as_of=ctx.today)
    return {
        "methodology": out.methodology,
        "overall": None if out.overall is None else str(out.overall),
        "reason": out.reason,
    }


def _readiness(ctx: Context) -> dict:
    ready = rd.evaluate(ctx.session, ctx.company, today=ctx.today, prefs=session_prefs(ctx.sess))
    ctx.sess.readiness = ready.as_dict()
    ctx.sess.readiness_score = ready.score
    ctx.sess.eligibility = ready.eligibility
    ctx.sess.workflow = ready.eligibility
    return {
        "score": ready.score,
        "eligibility": ready.eligibility,
        "critical_missing": len(ready.missing(rq.CRITICAL)),
    }


def _report(ctx: Context) -> dict:
    ready = ctx.sess.readiness or {}
    eligibility = ready.get("eligibility")
    if eligibility == rd.AUTOMATIC:
        report = rp.generate(ctx.session, ctx.sess, rp.COMPLETE, ctx.sess.owner, today=ctx.today)
        ctx.sess.status = "COMPLETED"
        return {"mode": rp.COMPLETE, "report_id": str(report.id), "version": report.version}
    if ctx.sess.choice == CONTINUE_WITH_AVAILABLE and eligibility != rd.BLOCKED:
        report = rp.generate(
            ctx.session, ctx.sess, rp.AVAILABLE_EVIDENCE, ctx.sess.owner, today=ctx.today
        )
        ctx.sess.status = "COMPLETED"
        return {
            "mode": rp.AVAILABLE_EVIDENCE,
            "report_id": str(report.id),
            "version": report.version,
        }
    ctx.sess.status = "AWAITING_DOCUMENTS"
    return {
        "deferred": True,
        "reason": "; ".join(ready.get("reasons", [])) or "evidence is not sufficient",
    }


RUNNERS: dict[str, Callable[[Context], dict]] = {
    "IDENTITY": _identity,
    "SOURCES": _sources,
    "DOCUMENTS": _documents,
    "EXTRACTION": _extraction,
    "EVIDENCE": _evidence,
    "CALCULATION": _calculation,
    "PEERS": _peers,
    "SCORING": _scoring,
    "READINESS": _readiness,
    "REPORT": _report,
}
# IDENTITY, READINESS and REPORT have no stage fingerprint: they are cheap and always decide from
# the CURRENT evidence, so they run every time.


def run(
    session: Session,
    sess: ResearchSession,
    *,
    refresh: bool = False,
    force: bool = False,
    today: date | None = None,
    router_factory: Callable | None = None,
    data_dir=None,
    llm_router=None,
    document_fetcher: Callable | None = None,
) -> ResearchSession:
    """Run (or resume) every stage. Returns the session with its final status."""
    today = today or clock.today()
    company = session.get(Company, sess.company_id)
    ctx = Context(
        session,
        sess,
        company,
        refresh,
        today,
        router_factory,
        data_dir,
        llm_router,
        document_fetcher,
    )
    sess.status = "RUNNING"
    sess.last_error = None
    session.commit()

    for name in STEPS:
        step = _step(session, sess, name)
        current = rd.stage_fingerprints(session, company.id).get(name)
        crashed = (
            step.status == "RUNNING"
            and step.started_at is not None
            and datetime.now(UTC) - step.started_at > STALE_RUNNING
        )
        unchanged = (
            step.status in ("DONE", "SKIPPED")
            and current is not None
            and step.fingerprint == current
            and not force
            and not (name == "SOURCES" and refresh)
        )
        if unchanged:
            detail = dict(step.detail or {})
            detail["resumed_without_rerun"] = True
            step.detail = detail
            session.commit()
            continue
        if step.status == "RUNNING" and not crashed and not force:
            # another worker is on it; do not start a second copy
            sess.status = "RUNNING"
            session.commit()
            return sess
        step.status = "RUNNING"
        step.started_at = datetime.now(UTC)
        step.finished_at = None
        step.attempts = (step.attempts or 0) + 1
        step.error = None
        session.commit()
        try:
            with session.begin_nested():
                detail = RUNNERS[name](ctx)
        except Exception as exc:  # noqa: BLE001 - recorded; later runs retry this stage
            step.status = "FAILED"
            step.error = f"{type(exc).__name__} while running this stage"
            step.finished_at = datetime.now(UTC)
            sess.status = "FAILED"
            sess.last_error = f"{STEP_LABELS[name]}: {step.error}"
            session.commit()
            return sess
        skipped = isinstance(detail, dict) and "skipped" in detail
        step.status = "SKIPPED" if skipped else "DONE"
        step.detail = detail
        step.finished_at = datetime.now(UTC)
        step.fingerprint = rd.stage_fingerprints(session, company.id).get(name)
        sess.evidence_fingerprint = rd.evidence_fingerprint(session, company.id)
        session.commit()

    if sess.status == "RUNNING":
        sess.status = "READY" if sess.eligibility == rd.AUTOMATIC else "AWAITING_DOCUMENTS"
    session.commit()
    return sess


def choose_available_evidence(session: Session, sess: ResearchSession) -> None:
    """The user decided to proceed with what exists (and accept the disclosed limitations)."""
    sess.choice = CONTINUE_WITH_AVAILABLE
    session.commit()


def status(session: Session, sess: ResearchSession) -> dict:
    steps = {
        s.step: s
        for s in session.scalars(select(ResearchStep).where(ResearchStep.session_id == sess.id))
    }
    out = []
    for name in STEPS:
        s = steps.get(name)
        out.append(
            {
                "step": name,
                "label": STEP_LABELS[name],
                "status": s.status if s else "PENDING",
                "attempts": s.attempts if s else 0,
                "error": s.error if s else None,
                "resumed_without_rerun": bool(s and (s.detail or {}).get("resumed_without_rerun")),
                "finished_at": s.finished_at.isoformat() if s and s.finished_at else None,
                "detail": s.detail if s else None,
            }
        )
    return {
        "id": str(sess.id),
        "company_id": str(sess.company_id),
        "query": sess.query,
        "status": sess.status,
        "workflow": sess.workflow,
        "eligibility": sess.eligibility,
        "readiness_score": None if sess.readiness_score is None else float(sess.readiness_score),
        "choice": sess.choice,
        "preferences": {
            **session_prefs(sess).to_dict(),
            "period_label": session_prefs(sess).period_label(),
        },
        "preference_log": sess.preference_log or [],
        "last_error": sess.last_error,
        "created_at": sess.created_at.isoformat() if sess.created_at else None,
        "updated_at": sess.updated_at.isoformat() if sess.updated_at else None,
        "steps": out,
    }
