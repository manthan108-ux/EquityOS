"""Regression: the Inventory documents stage must survive a fetcher that commits.

The real fetcher (`evidence_fetch.fetch_evidence`) commits after each stored file. The orchestrator
runs a stage inside a SAVEPOINT; committing from inside it ended that savepoint and the stage failed
with sqlalchemy `InvalidRequestError` (seen live for Axis Bank, 2026-11-11).
"""

from sqlalchemy import select

from app.acquisition import evidence_fetch as ef
from app.models import ResearchStep
from app.research import orchestrator
from tests.research_helpers import complete_company
from tests.test_evidence_fetch import IT, _session
from tests.test_sector import seeded  # noqa: F401


def _step(session, sess, name):
    return session.scalar(
        select(ResearchStep).where(ResearchStep.session_id == sess.id, ResearchStep.step == name)
    )


def test_documents_stage_survives_a_fetcher_that_commits(seeded, tmp_path):  # noqa: F811
    company = complete_company(seeded, "Commit Co", *IT)

    def committing(session, comp, *, data_dir=None):
        session.commit()  # what fetch_evidence does after each stored document
        return ef.FetchReport(ef.NO_WEBSITE, None, notes=["no website"])

    sess = orchestrator.run(
        seeded, _session(seeded, company), data_dir=tmp_path, document_fetcher=committing
    )
    step = _step(seeded, sess, "DOCUMENTS")
    assert step.status == "DONE", step.error
    assert sess.status != "FAILED"
    # the pipeline went on to the next stage
    assert _step(seeded, sess, "EXTRACTION") is not None


def test_documents_stage_survives_a_fetcher_that_rolls_back(seeded, tmp_path):  # noqa: F811
    company = complete_company(seeded, "Rollback Co", *IT)

    def rolling(session, comp, *, data_dir=None):
        session.rollback()  # what fetch_evidence does when one file is refused
        return ef.FetchReport(ef.NO_WEBSITE, None, notes=["no website"])

    sess = orchestrator.run(
        seeded, _session(seeded, company), data_dir=tmp_path, document_fetcher=rolling
    )
    assert _step(seeded, sess, "DOCUMENTS").status == "DONE"


def test_a_failed_documents_stage_can_be_retried_and_keeps_earlier_steps(
    seeded,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    company = complete_company(seeded, "Retry Co", *IT)
    real = orchestrator.RUNNERS["DOCUMENTS"]
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider unavailable")
        return real(ctx)

    monkeypatch.setitem(orchestrator.RUNNERS, "DOCUMENTS", flaky)
    sess = orchestrator.run(seeded, _session(seeded, company), data_dir=tmp_path)
    assert sess.status == "FAILED"
    assert _step(seeded, sess, "DOCUMENTS").status == "FAILED"
    assert _step(seeded, sess, "IDENTITY").status == "DONE"  # earlier work is intact
    assert "RuntimeError" in sess.last_error

    sess = orchestrator.run(seeded, sess, data_dir=tmp_path)  # retry resumes at the failed stage
    assert _step(seeded, sess, "DOCUMENTS").status == "DONE"
    assert _step(seeded, sess, "DOCUMENTS").attempts == 2
    assert _step(seeded, sess, "EXTRACTION") is not None
    assert sess.status != "FAILED"
