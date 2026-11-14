"""Stale peer ranks, medians and scores are found and refreshed, and a run does it at its end."""

# ruff: noqa: F811
import pytest
from sqlalchemy import func, select

from app.evidence.integrity import audit
from app.models import EvidenceClass, EvidenceRecord, EvidenceStatus
from app.pipeline import peers, runner
from app.sector.engine import analyze_company
from tests.test_sector import NOW, TODAY, it_sector, seeded  # noqa: F401


def analyse_all(session, companies):
    for c in companies.values():
        analyze_company(session, c.id, today=TODAY, now=NOW)
    session.flush()


def peer_rows(session, company):
    return session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company.id,
            EvidenceRecord.evidence_class == EvidenceClass.CALCULATION,
            EvidenceRecord.created_by == "sector-engine",
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.metric.like("PEER%"),
        )
    ).all()


def replace_input(session, company, metric):
    """What a refresh does: the metric's evidence is replaced (the old version is withdrawn)."""
    old = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company.id,
            EvidenceRecord.metric == metric,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )
    old.status = EvidenceStatus.WITHDRAWN
    session.flush()
    return old


def test_nothing_is_stale_after_a_clean_analysis(seeded, it_sector):
    analyse_all(seeded, it_sector)
    assert peers.stale_peer_companies(seeded) == []
    assert peers.refresh_stale_peers(seeded)["stale"] == 0


def test_a_replaced_input_makes_the_ranks_that_used_it_stale(seeded, it_sector):
    analyse_all(seeded, it_sector)
    assert peer_rows(seeded, it_sector["A"])
    replace_input(seeded, it_sector["B"], "RETURN.roe_closing")
    stale = set(peers.stale_peer_companies(seeded))
    assert it_sector["A"].id in stale, "A's rank was computed with B's value"
    report = audit(seeded, today=TODAY)
    assert {c.code: c for c in report.checks}["STALE_CALCULATION"].count >= 1


def test_refreshing_clears_the_staleness_and_keeps_the_old_versions(seeded, it_sector):
    analyse_all(seeded, it_sector)
    before = seeded.scalar(select(func.count()).select_from(EvidenceRecord))
    replace_input(seeded, it_sector["B"], "RETURN.roe_closing")
    seeded.commit()
    result = peers.refresh_stale_peers(seeded, now=NOW)
    assert result["stale"] >= 1 and result["failed"] == {} and result["remaining"] == 0
    assert str(it_sector["A"].id) in result["refreshed"]
    assert peers.stale_peer_companies(seeded) == []
    after = seeded.scalar(select(func.count()).select_from(EvidenceRecord))
    assert after >= before, "versions are added, never removed"
    old_inputs = seeded.scalar(
        select(func.count())
        .select_from(EvidenceRecord)
        .where(EvidenceRecord.status == EvidenceStatus.WITHDRAWN)
    )
    assert old_inputs >= 1
    report = audit(seeded, today=TODAY)
    assert {c.code: c for c in report.checks}["STALE_CALCULATION"].clean


def test_the_limit_bounds_one_refresh_and_reports_what_remains(seeded, it_sector):
    analyse_all(seeded, it_sector)
    replace_input(seeded, it_sector["B"], "RETURN.roe_closing")
    found = len(peers.stale_peer_companies(seeded))
    assert found >= 2
    result = peers.refresh_stale_peers(seeded, limit=1, now=NOW)
    assert len(result["refreshed"]) == 1


def test_one_failing_company_does_not_stop_the_others(seeded, it_sector, monkeypatch):
    from app.sector import engine

    analyse_all(seeded, it_sector)
    replace_input(seeded, it_sector["B"], "RETURN.roe_closing")
    seeded.commit()  # as in production the setup is committed; a failed company rolls back only itself
    real = engine.analyze_company
    victim = it_sector["A"].id

    def flaky(session, company_id, **kw):
        if company_id == victim:
            raise RuntimeError("boom")
        return real(session, company_id, **kw)

    monkeypatch.setattr(engine, "analyze_company", flaky)
    result = peers.refresh_stale_peers(seeded, now=NOW)
    assert str(victim) in result["failed"] and "boom" in result["failed"][str(victim)]
    assert result["refreshed"], "the others were still refreshed"


def test_a_finished_run_refreshes_stale_peers_and_says_so(seeded, it_sector):
    analyse_all(seeded, it_sector)
    replace_input(seeded, it_sector["B"], "RETURN.roe_closing")
    seeded.commit()
    run = runner.create_run(seeded, stages=("readiness",), symbols=None, batch_size=1)
    run.companies = []
    seeded.commit()
    runner.claim_run(seeded, run.id)
    real_close = seeded.close
    seeded.close = lambda: None
    try:
        done = runner.execute(lambda: seeded, run.id, close_sessions=False)
    finally:
        seeded.close = real_close
    assert done.summary["peer_refresh"]["refreshed"] >= 1
    assert peers.stale_peer_companies(seeded) == []
    from app.models import PipelineEvent

    messages = [
        e.message
        for e in seeded.scalars(select(PipelineEvent).where(PipelineEvent.run_id == run.id))
    ]
    assert any(m.startswith("peer refresh:") for m in messages)


def test_a_run_can_skip_the_refresh(seeded, it_sector):
    analyse_all(seeded, it_sector)
    replace_input(seeded, it_sector["B"], "RETURN.roe_closing")
    seeded.commit()
    run = runner.create_run(
        seeded, stages=("readiness",), batch_size=1, options={"refresh_peers": False}
    )
    run.companies = []
    seeded.commit()
    runner.claim_run(seeded, run.id)
    done = runner.execute(lambda: seeded, run.id, close_sessions=False)
    assert "peer_refresh" not in done.summary
    assert peers.stale_peer_companies(seeded), "still stale: the run was told not to refresh"


@pytest.mark.parametrize("prefix", peers.PEER_PREFIXES)
def test_the_prefixes_cover_ranks_medians_and_scores(prefix):
    assert prefix.endswith(".") and prefix.split(".")[0] in {"PEERRANK", "PEERMEDIAN", "SCORE"}
