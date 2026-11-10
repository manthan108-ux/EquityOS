"""Research workflow: requirements, readiness, orchestration and reports (Phase 11)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import ResearchReport, ResearchSession, ResearchStep
from app.research import orchestrator
from app.research import preferences as pf
from app.research import readiness as rd
from app.research import report as rp
from app.research import requirements as rq
from tests.research_helpers import (
    FY25,
    NOW,  # noqa: F401
    TODAY,
    complete_company,
    evidence,
    mk_document,
    satisfy,
)
from tests.test_sector import mk_company, seeded  # noqa: F401

IT = ("it_services", "it_services_and_consulting")
BANK = ("banking", "private_sector_bank")
FMCG = ("fmcg", "diversified_fmcg")
AUTO = ("automobiles", "passenger_vehicles")
ENERGY = ("oil_and_gas", "refineries_and_marketing")
CONGLOMERATE = ("oil_and_gas", "diversified_conglomerate")


# ---- the registry ---------------------------------------------------------------------------------


def test_every_profile_has_a_consistent_requirement_set():
    for profile in rq.PROFILE_DEFS:
        reqs = rq.requirements_for(profile)
        ids = [r.id for r in reqs]
        assert len(ids) == len(set(ids))
        section_keys = {k for k, _ in rq.SECTIONS}
        for r in reqs:
            assert r.importance in rq.IMPORTANCE_ORDER and r.metrics and r.why and r.document
            assert set(r.sections) <= section_keys, r.id
            if r.extractor == rq.DERIVED:
                assert r.inputs, f"{profile}.{r.id}: a derived metric must name its inputs"
        assert any(r.importance == rq.CRITICAL for r in reqs)


def test_requirements_differ_by_industry_instead_of_one_list_for_all():
    ids = {p: {r.id for r in rq.requirements_for(p)} for p in rq.PROFILE_DEFS}
    # a bank is judged on bank measures and never on industrial ones
    assert {"nim", "gross_npa", "net_npa", "capital_adequacy", "deposits", "advances"} <= ids[
        "BANK"
    ]
    assert not ids["BANK"] & {"operating_margin", "debt_to_equity", "capex", "roce"}
    # automobiles need volumes; IT needs attrition/utilisation; energy needs segments and capex
    assert "vehicle_volumes" in ids["AUTOMOBILES"] and "vehicle_volumes" not in ids["IT_SERVICES"]
    assert {"attrition", "utilization"} <= ids["IT_SERVICES"]
    assert {"segment_revenue", "capex"} <= ids["ENERGY"]
    assert "volume_growth" in ids["FMCG"]
    # the importance of one metric changes with the business
    imp = {p: {r.id: r.importance for r in rq.requirements_for(p)} for p in rq.PROFILE_DEFS}
    assert imp["AUTOMOBILES"]["debt_to_equity"] == rq.CRITICAL
    assert imp["IT_SERVICES"]["debt_to_equity"] == rq.OPTIONAL
    assert imp["CONGLOMERATE"]["segment_revenue"] == rq.CRITICAL
    assert imp["ENERGY"]["segment_revenue"] == rq.IMPORTANT
    assert imp["BANK"]["pb"] == rq.CRITICAL and imp["FMCG"]["pb"] == rq.IMPORTANT


def test_only_figures_no_extractor_can_read_are_excluded_from_upload_requests():
    for profile in rq.PROFILE_DEFS:
        for r in rq.requirements_for(profile):
            if r.id == "volume_growth":
                assert r.extractor == rq.NONE and not r.upload_helps
            if r.id in ("attrition", "utilization", "vehicle_volumes"):
                assert r.extractor == rq.OPERATING_TEXT and r.upload_helps
                assert r.importance != rq.CRITICAL  # never holds research back on its own


def test_profile_selection_splits_energy_from_the_generic_scoring_profile():
    assert rq.profile_for(*BANK) == "BANK"
    assert rq.profile_for(*IT) == "IT_SERVICES"
    assert rq.profile_for(*ENERGY) == "ENERGY"
    assert rq.profile_for(*CONGLOMERATE) == "CONGLOMERATE"
    assert rq.profile_for("pharma", "x") == "GENERIC_NONFIN"


def test_thresholds_are_per_profile_and_not_a_single_percentage():
    th = rq.thresholds_for("BANK")
    assert th.weights["sector_metrics"] > rq.thresholds_for("FMCG").weights["sector_metrics"]
    for p in rq.PROFILE_DEFS:
        t = rq.thresholds_for(p)
        assert abs(sum(t.weights.values()) - 1.0) < 1e-9
        assert t.min_component > 0 and t.max_open_errors == 0


# ---- readiness ------------------------------------------------------------------------------------


def test_a_company_with_everything_is_researched_automatically(seeded):  # noqa: F811
    c = complete_company(seeded, "Full IT", *IT)
    r = rd.evaluate(seeded, c, today=TODAY)
    assert r.eligibility == rd.AUTOMATIC, r.reasons
    assert not r.missing(rq.CRITICAL) and r.score >= rq.thresholds_for("IT_SERVICES").min_score
    assert r.confidence in ("HIGH", "MEDIUM")


def test_a_missing_critical_metric_forces_assisted_research_however_high_the_score(seeded):  # noqa: F811
    c = complete_company(seeded, "Almost IT", *IT, skip=("operating_margin",))
    r = rd.evaluate(seeded, c, today=TODAY)
    assert r.score > 80  # the average is excellent...
    assert r.eligibility == rd.ASSISTED  # ...and one critical gap still decides
    missing = [s.req.id for s in r.missing(rq.CRITICAL)]
    assert missing == ["operating_margin"]
    s = r.by_id("operating_margin")
    assert s.status == rd.MISSING and s.value is None  # missing is never zero
    assert s.action == "UPLOAD" and "depreciation_and_amortisation" in " ".join(s.missing_inputs)


def test_a_missing_optional_metric_does_not_block_and_is_not_requested_by_default(seeded):  # noqa: F811
    c = complete_company(
        seeded, "No capex IT", *IT, skip=("capex", "free_cash_flow", "debt_to_equity")
    )
    r = rd.evaluate(seeded, c, today=TODAY)
    assert r.eligibility == rd.AUTOMATIC
    assert r.by_id("capex") is None and not r.missing(rq.OPTIONAL)  # optional items are off
    asked = rd.evaluate(seeded, c, today=TODAY, prefs=pf.Preferences(include_optional=True))
    assert asked.eligibility == rd.AUTOMATIC  # still never blocks
    assert {"capex", "debt_to_equity"} <= {s.req.id for s in asked.missing(rq.OPTIONAL)}


def test_stale_annual_figures_do_not_count(seeded):  # noqa: F811
    c = complete_company(seeded, "Old IT", *IT, skip=("revenue",))
    old = evidence(seeded, c, "INCOME.revenue_from_operations", 50, end=FY25 - timedelta(days=400))
    r = rd.evaluate(seeded, c, today=TODAY, prefs=pf.Preferences(period_mode="LATEST_ANNUAL"))
    s = r.by_id("revenue")
    assert s.status == rd.STALE and s.evidence_id == old.id and "older" in s.reason
    assert r.eligibility == rd.ASSISTED


def test_values_blocked_by_validation_are_reported_as_such(seeded):  # noqa: F811
    c = complete_company(seeded, "Blocked IT", *IT, skip=("net_profit",))
    evidence(seeded, c, "INCOME.net_profit", 5, blocked=True)
    r = rd.evaluate(seeded, c, today=TODAY)
    s = r.by_id("net_profit")
    assert (
        s.status == rd.MISSING and "failed validation" in s.reason and s.blocked_by == ["TEST_RULE"]
    )
    assert r.open_errors == 1 and r.eligibility == rd.ASSISTED


def test_a_company_with_no_data_is_blocked_not_scored_zero(seeded):  # noqa: F811
    c = mk_company(seeded, "Empty", *IT)
    r = rd.evaluate(seeded, c, today=TODAY)
    assert r.eligibility == rd.BLOCKED and r.confidence == "NONE"
    assert all(s.value is None for s in r.statuses)


def test_a_bank_is_never_asked_for_industrial_metrics(seeded):  # noqa: F811
    c = complete_company(seeded, "Full Bank", *BANK)
    r = rd.evaluate(seeded, c, today=TODAY)
    assert r.profile == "BANK" and r.eligibility == rd.AUTOMATIC, r.reasons
    assert r.by_id("debt_to_equity") is None and r.by_id("operating_margin") is None


def test_bank_gaps_name_the_banking_figures_and_the_documents_that_hold_them(seeded):  # noqa: F811
    c = complete_company(
        seeded, "Gappy Bank", *BANK, skip=("nim", "gross_npa", "net_npa", "advances", "deposits")
    )
    r = rd.evaluate(seeded, c, today=TODAY)
    assert r.eligibility == rd.ASSISTED
    missing = r.as_dict()["missing"]["CRITICAL"]
    assert {m["id"] for m in missing} == {"nim", "gross_npa", "net_npa", "advances", "deposits"}
    nim = next(m for m in missing if m["id"] == "nim")
    assert nim["why_it_matters"] and nim["affects"] and nim["document"]
    assert nim["basis_required"].startswith("not applicable") and nim["preferred_period"]
    assert nim["upload_helps"] and "substitute" in nim["alternative"]
    assert "sector_analysis" in nim["sections"]


def test_high_document_coverage_cannot_hide_a_missing_critical_metric(seeded):  # noqa: F811
    c = complete_company(seeded, "Covered", *FMCG, skip=("roce",))
    r = rd.evaluate(seeded, c, today=TODAY)
    assert r.components["document_coverage"]["score"] >= 60
    assert r.eligibility == rd.ASSISTED and r.by_id("roce").req.importance == rq.CRITICAL


def test_figures_that_cannot_be_read_from_any_document_say_so(seeded):  # noqa: F811
    c = complete_company(seeded, "Food", *FMCG)
    r = rd.evaluate(seeded, c, today=TODAY, prefs=pf.Preferences(include_optional=True))
    vol = r.by_id("volume_growth")
    assert vol.status == rd.MISSING and vol.action == "NOT_RESOLVABLE_BY_UPLOAD"
    assert not vol.req.upload_helps and "no extractor" in vol.reason
    assert r.eligibility == rd.AUTOMATIC  # an unreadable optional figure never blocks


def test_an_important_operating_figure_is_requested_but_does_not_block(seeded):  # noqa: F811
    c = complete_company(seeded, "Auto", *AUTO, skip=("vehicle_volumes",))
    r = rd.evaluate(seeded, c, today=TODAY)
    vol = r.by_id("vehicle_volumes")
    assert vol.status == rd.MISSING and vol.action == "UPLOAD" and vol.req.upload_helps
    assert r.components["sector_metrics"]["score"] == 0.0
    assert r.components["sector_metrics"]["gating"] is False
    assert r.eligibility == rd.AUTOMATIC  # important, not critical: disclosed, not blocking


def test_a_stored_unprocessed_document_is_extracted_not_requested_again(seeded):  # noqa: F811
    c = complete_company(seeded, "Held", *BANK, skip=("gross_npa",))
    mk_document(seeded, c, doc_type="quarterly_result")
    r = rd.evaluate(seeded, c, today=TODAY)
    s = r.by_id("gross_npa")
    assert s.action == "EXTRACT_HELD" and s.held_documents
    assert "not been processed" in s.reason


def test_a_held_document_that_lacks_the_figure_is_not_asked_for_blindly(seeded):  # noqa: F811
    from app.models import DocumentExtraction, ExtractionStatus

    c = complete_company(seeded, "Held2", *BANK, skip=("gross_npa",))
    doc = mk_document(seeded, c, doc_type="quarterly_result")
    seeded.add(
        DocumentExtraction(
            document_id=doc.id, extractor_version="t", status=ExtractionStatus.COMPLETED, issues=[]
        )
    )
    seeded.flush()
    s = rd.evaluate(seeded, c, today=TODAY).by_id("gross_npa")
    assert s.action == "UPLOAD" and "already holds a matching document" in s.reason


def test_the_basis_is_the_users_and_is_never_switched_silently(seeded):  # noqa: F811
    c = complete_company(seeded, "Standalone IT", *IT, skip=("net_profit",))
    evidence(seeded, c, "INCOME.net_profit", 5, basis="STANDALONE")
    r = rd.evaluate(seeded, c, today=TODAY)  # default: consolidated
    s = r.by_id("net_profit")
    assert s.status == rd.MISSING and s.value is None
    assert "not available on the consolidated basis you chose" in s.reason
    assert s.other_basis["basis"] == "STANDALONE" and s.action == "CHANGE_BASIS_OR_UPLOAD"
    assert r.eligibility == rd.ASSISTED
    own = rd.evaluate(seeded, c, today=TODAY, prefs=pf.Preferences(basis="STANDALONE"))
    assert own.by_id("net_profit").basis == "STANDALONE"  # honoured when the user asks for it
    assert (
        own.by_id("revenue").status == rd.MISSING
    )  # ...and revenue (consolidated only) is not borrowed


def test_sections_report_which_parts_of_the_report_are_affected(seeded):  # noqa: F811
    c = complete_company(seeded, "Sections", *IT, skip=("operating_margin",))
    sections = {s["key"]: s for s in rd.evaluate(seeded, c, today=TODAY).as_dict()["sections"]}
    assert sections["profitability"]["status"] == "PARTIAL"
    assert "operating_margin" in sections["profitability"]["critical_missing"]
    assert sections["financial_performance"]["status"] == "COMPLETE"


def test_readiness_is_independent_of_the_financial_score(seeded):  # noqa: F811
    weak = complete_company(seeded, "Weak but documented", *IT)
    # make the company's *financials* terrible: the readiness score must not move
    before = rd.evaluate(seeded, weak, today=TODAY).score
    evidence(seeded, weak, "GROWTH.eps_yoy", -90, kind="CALCULATION", unit="percent")
    after = rd.evaluate(seeded, weak, today=TODAY).score
    assert before == after


# ---- orchestration --------------------------------------------------------------------------------


def run_session(session, company, **kw):
    sess = orchestrator.create_session(session, company, "alice", company.common_name)
    return orchestrator.run(session, sess, today=TODAY, **kw)


def steps(session, sess):
    return {
        s.step: s
        for s in session.scalars(select(ResearchStep).where(ResearchStep.session_id == sess.id))
    }


def test_sufficient_evidence_completes_automatically_with_a_complete_report(seeded):  # noqa: F811
    c = complete_company(seeded, "Auto IT", *IT)
    sess = run_session(seeded, c)
    assert sess.status == "COMPLETED" and sess.workflow == rd.AUTOMATIC, sess.last_error
    report = seeded.scalar(select(ResearchReport).where(ResearchReport.session_id == sess.id))
    assert report.mode == rp.COMPLETE and report.missing_critical == 0
    assert report.content["meta"]["complete"] is True
    done = steps(seeded, sess)
    assert [done[s].status for s in orchestrator.STEPS if s != "SOURCES"] == ["DONE"] * 9
    assert done["SOURCES"].status == "SKIPPED"  # network refresh was not requested


def test_missing_critical_data_defers_and_asks_instead_of_producing_a_report(seeded):  # noqa: F811
    c = complete_company(seeded, "Needs docs", *BANK, skip=("nim", "gross_npa"))
    sess = run_session(seeded, c)
    assert sess.status == "AWAITING_DOCUMENTS" and sess.workflow == rd.ASSISTED
    assert seeded.scalar(select(ResearchReport).where(ResearchReport.session_id == sess.id)) is None
    assert {m["id"] for m in sess.readiness["missing"]["CRITICAL"]} >= {"nim", "gross_npa"}
    assert steps(seeded, sess)["REPORT"].detail["deferred"] is True


def test_continuing_with_available_evidence_produces_a_disclosed_partial_report(seeded):  # noqa: F811
    c = complete_company(seeded, "Partial", *BANK, skip=("nim", "gross_npa"))
    sess = run_session(seeded, c)
    orchestrator.choose_available_evidence(seeded, sess)
    orchestrator.run(seeded, sess, today=TODAY)
    report = seeded.scalar(select(ResearchReport).where(ResearchReport.session_id == sess.id))
    assert report.mode == rp.AVAILABLE_EVIDENCE and report.content["meta"]["complete"] is False
    lim = report.content["limitations"]
    assert {g["requirement"] for g in lim["missing_critical"]} == {"nim", "gross_npa"}
    assert any("net interest margin" in t.lower() for t in lim["limits_on_conclusions"])
    assert "not available" in report.content["meta"]["banner"]
    # the same section structure as a complete report
    assert [s["key"] for s in report.content["sections"]] == [k for k, _ in rq.SECTIONS]


def test_a_complete_report_is_refused_while_critical_data_is_missing(seeded):  # noqa: F811
    c = complete_company(seeded, "Refused", *IT, skip=("operating_margin",))
    sess = run_session(seeded, c)
    with pytest.raises(rp.ReportNotAllowed) as exc:
        rp.generate(seeded, sess, rp.COMPLETE, "alice", today=TODAY)
    assert "critical" in str(exc.value)


def test_nothing_is_reported_for_a_company_without_evidence(seeded):  # noqa: F811
    c = mk_company(seeded, "Nothing", *IT)
    sess = run_session(seeded, c)
    orchestrator.choose_available_evidence(seeded, sess)
    orchestrator.run(seeded, sess, today=TODAY)
    assert sess.workflow == rd.BLOCKED
    with pytest.raises(rp.ReportNotAllowed):
        rp.generate(seeded, sess, rp.AVAILABLE_EVIDENCE, "alice", today=TODAY)


def test_resuming_repeats_no_step_whose_inputs_are_unchanged(seeded):  # noqa: F811
    c = complete_company(seeded, "Resume", *BANK, skip=("nim",))
    sess = run_session(seeded, c)
    first = {k: (v.attempts, v.finished_at) for k, v in steps(seeded, sess).items()}
    orchestrator.run(seeded, sess, today=TODAY)
    second = steps(seeded, sess)
    for name in ("DOCUMENTS", "EXTRACTION", "EVIDENCE", "CALCULATION", "PEERS", "SCORING"):
        assert second[name].attempts == first[name][0], f"{name} was repeated"
        assert second[name].detail.get("resumed_without_rerun") is True
    assert second["READINESS"].attempts == first["READINESS"][0] + 1  # decisions are always fresh


def test_new_evidence_makes_the_steps_run_again_and_resolves_the_gap(seeded):  # noqa: F811
    c = complete_company(seeded, "Grows", *BANK, skip=("nim",))
    sess = run_session(seeded, c)
    assert sess.status == "AWAITING_DOCUMENTS"
    before = steps(seeded, sess)["EVIDENCE"].attempts
    nim = next(r for r in rq.requirements_for("BANK") if r.id == "nim")
    satisfy(seeded, c, nim, document=mk_document(seeded, c))
    orchestrator.run(seeded, sess, today=TODAY)
    assert steps(seeded, sess)["EVIDENCE"].attempts == before + 1
    assert sess.status == "COMPLETED" and sess.workflow == rd.AUTOMATIC


def test_a_failing_step_is_recorded_and_retried_on_resume(seeded, monkeypatch):  # noqa: F811
    c = complete_company(seeded, "Flaky", *IT)
    calls = {"n": 0}
    real = orchestrator.RUNNERS["CALCULATION"]

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom with secret-token-123")
        return real(ctx)

    monkeypatch.setitem(orchestrator.RUNNERS, "CALCULATION", flaky)
    sess = run_session(seeded, c)
    assert sess.status == "FAILED"
    step = steps(seeded, sess)["CALCULATION"]
    assert step.status == "FAILED" and "RuntimeError" in step.error
    assert "secret-token" not in step.error and "secret-token" not in sess.last_error
    assert steps(seeded, sess)["EVIDENCE"].status == "DONE"  # earlier work is kept
    orchestrator.run(seeded, sess, today=TODAY)
    assert sess.status == "COMPLETED" and steps(seeded, sess)["CALCULATION"].attempts == 2


def test_a_stage_left_running_by_a_crash_is_retaken(seeded):  # noqa: F811
    c = complete_company(seeded, "Crashed", *IT)
    sess = orchestrator.create_session(seeded, c, "alice", "Crashed")
    seeded.add(
        ResearchStep(
            session_id=sess.id,
            step="IDENTITY",
            status="RUNNING",
            attempts=1,
            started_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    seeded.flush()
    orchestrator.run(seeded, sess, today=TODAY)
    assert sess.status == "COMPLETED" and steps(seeded, sess)["IDENTITY"].attempts == 2


def test_an_open_session_is_reused_for_the_same_user_and_company(seeded):  # noqa: F811
    c = complete_company(seeded, "Once", *BANK, skip=("nim",))
    a = run_session(seeded, c)
    b = orchestrator.create_session(seeded, c, "alice", "again")
    assert b.id == a.id
    other = orchestrator.create_session(seeded, c, "bob", "again")
    assert other.id != a.id
    assert len(list(seeded.scalars(select(ResearchSession)))) == 2


def test_reports_are_versioned_and_unchanged_content_is_not_duplicated(seeded):  # noqa: F811
    c = complete_company(seeded, "Versions", *BANK, skip=("nim",))
    sess = run_session(seeded, c)
    first = rp.generate(seeded, sess, rp.AVAILABLE_EVIDENCE, "alice", today=TODAY)
    again = rp.generate(seeded, sess, rp.AVAILABLE_EVIDENCE, "alice", today=TODAY)
    assert again.id == first.id and first.version == 1
    satisfy(
        seeded, c, next(r for r in rq.requirements_for("BANK") if r.id == "nim"),
        document=mk_document(seeded, c),
    )  # fmt: skip
    newer = rp.generate(seeded, sess, rp.AVAILABLE_EVIDENCE, "alice", today=TODAY)
    assert newer.version == 2 and newer.id != first.id
    assert first.content["limitations"]["missing_critical"]  # the old version is untouched


def test_report_figures_carry_their_evidence_and_never_invent_values(seeded):  # noqa: F811
    c = complete_company(seeded, "Traced", *IT, skip=("operating_margin",))
    sess = run_session(seeded, c)
    report = rp.generate(seeded, sess, rp.AVAILABLE_EVIDENCE, "alice", today=TODAY)
    fp = next(s for s in report.content["sections"] if s["key"] == "financial_performance")
    item = next(i for i in fp["items"] if i["requirement"] == "revenue")
    assert item["source"]["evidence_id"] and item["source"]["document_id"]
    prof = next(s for s in report.content["sections"] if s["key"] == "profitability")
    assert all(i["requirement"] != "operating_margin" for i in prof["items"])
    gap = next(g for g in prof["gaps"] if g["requirement"] == "operating_margin")
    assert gap["reason"] and gap["importance"] == "CRITICAL"
    hist = next(s for s in report.content["sections"] if s["key"] == "historical")
    assert hist["series"]["revenue"][0]["value"] == "10.000000"
    assert report.content["readiness"]["note"].startswith(
        "research readiness measures the evidence"
    )


def test_fingerprint_changes_when_evidence_changes(seeded):  # noqa: F811
    c = complete_company(seeded, "Fingerprint", *IT)
    a = rd.evidence_fingerprint(seeded, c.id)
    assert a == rd.evidence_fingerprint(seeded, c.id)
    evidence(seeded, c, "GROWTH.eps_yoy", 3, kind="CALCULATION", unit="percent")
    assert rd.evidence_fingerprint(seeded, c.id) != a
