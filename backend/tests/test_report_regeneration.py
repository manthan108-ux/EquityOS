"""A saved report is regenerated as a NEW version when new evidence arrives, never overwritten."""

# ruff: noqa: F811
from sqlalchemy import func, select

from app.models import ResearchReport, ResearchSession
from app.research import readiness as rd
from app.research import report as rp
from app.research import requirements as rq
from app.research.pdf_pro import render
from tests.research_helpers import complete_company, satisfy
from tests.test_pipeline_stages import IT, go
from tests.test_sector import seeded  # noqa: F401


def versions(session, sess):
    return list(
        session.scalars(
            select(ResearchReport)
            .where(ResearchReport.session_id == sess.id)
            .order_by(ResearchReport.version)
        )
    )


def pick_requirement(profile):
    """A requirement that is not one of the document-backed headline figures."""
    for req in rq.requirements_for(profile):
        if req.extractor != rq.NONE and req.id not in ("revenue", "net_profit", "latest_quarter"):
            if req.period == "ANNUAL" and req.basis != "NA":
                return req
    raise AssertionError("no suitable requirement in the profile")


def test_new_evidence_makes_a_new_version_and_the_old_one_is_untouched(seeded):
    profile = rq.profile_for(*IT)
    missing = pick_requirement(profile)
    co = complete_company(seeded, "Regen Co", *IT, skip=(missing.id,))
    sess = ResearchSession(company_id=co.id, owner="t", query="Regen Co", status="READY")
    seeded.add(sess)
    seeded.flush()
    rp.generate(seeded, sess, "AVAILABLE_EVIDENCE", "t")
    seeded.commit()
    v1 = versions(seeded, sess)[0]
    frozen = (v1.id, v1.content_hash, v1.readiness_score, v1.created_at, str(v1.content))
    before_score = rd.evaluate(seeded, co).score

    # a stage run with nothing new: no new version
    go(seeded, co, ("readiness", "report"))
    assert len(versions(seeded, sess)) in (1, 2)  # a first regeneration may refresh once
    settled = len(versions(seeded, sess))
    again = go(seeded, co, ("readiness", "report"))
    assert again.steps["report"].startswith("readiness unchanged")
    forced = go(seeded, co, ("report",), force=True)
    assert "content unchanged" in forced.steps["report"]
    assert len(versions(seeded, sess)) == settled

    # the missing figure arrives
    satisfy(seeded, co, missing)
    seeded.commit()
    res = go(seeded, co, ("readiness", "report"))
    assert res.statuses["report"] == "SUCCEEDED" and "new version" in res.steps["report"]
    rows = versions(seeded, sess)
    assert len(rows) == settled + 1
    newest = rows[-1]
    assert newest.version == rows[-2].version + 1 and newest.created_by == "pipeline"
    assert newest.content_hash != rows[-2].content_hash
    assert rd.evaluate(seeded, co).score > before_score
    assert newest.readiness_score is not None and newest.readiness_score > v1.readiness_score

    # the original version is exactly as it was
    first = seeded.get(ResearchReport, frozen[0])
    assert (first.id, first.content_hash, first.readiness_score, first.created_at) == frozen[:4]
    assert str(first.content) == frozen[4]

    # and the new one renders
    pdf = render(
        newest.content,
        version=newest.version,
        mode=newest.mode,
        report_id=str(newest.id),
        content_hash=newest.content_hash,
    )
    assert pdf.startswith(b"%PDF") and len(pdf) > 5000


def test_a_report_that_became_blocked_is_not_overwritten(seeded):
    """If readiness drops to BLOCKED the stage skips with a reason; the saved report stays."""
    co = complete_company(seeded, "Blocked Co", *IT)
    sess = ResearchSession(company_id=co.id, owner="t", query="Blocked Co", status="READY")
    seeded.add(sess)
    seeded.flush()
    rp.generate(seeded, sess, "COMPLETE", "t")
    seeded.commit()
    n = seeded.scalar(select(func.count()).select_from(ResearchReport))
    # a COMPLETE report needs an AUTOMATIC company; remove one critical requirement's evidence
    from app.models import EvidenceRecord

    critical = [r for r in rq.requirements_for(rq.profile_for(*IT)) if r.id == "revenue"]
    assert critical
    for rec in seeded.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == co.id, EvidenceRecord.metric.in_(critical[0].metrics)
        )
    ):
        seeded.delete(rec)
    seeded.commit()
    res = go(seeded, co, ("readiness", "report"))
    assert res.statuses["report"] == "SKIPPED"
    message = res.steps["report"]
    assert message.startswith("not regenerated: a complete report needs every critical")
    assert "; ;" not in message and "a;  ;" not in message  # a readable reason, not characters
    assert seeded.scalar(select(func.count()).select_from(ResearchReport)) == n  # nothing lost
