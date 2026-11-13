"""An accepted upload must reach the facts, the readiness verdict and the written report."""

# ruff: noqa: F811, F401
from datetime import date

from app.research import preferences as pf
from app.research import readiness as rd
from app.research import report as rep
from app.research import vault
from tests.test_research_vault import data_dir, reliance, results_pdf, upload  # noqa: F401

TODAY = date(2026, 11, 20)


def content(session, company):
    ready = rd.evaluate(session, company, today=TODAY, prefs=pf.Preferences.from_dict({}))
    return ready, rep.build_content(session, company, ready, "AVAILABLE_EVIDENCE", TODAY)


def series(content_, name):
    historical = next(s for s in content_["sections"] if s["key"] == "historical")
    return historical["series"][name]


def test_an_accepted_upload_becomes_facts_a_met_requirement_and_report_figures(
    session, reliance, data_dir
):
    before_ready, before = content(session, reliance)
    assert not any(row["fiscal_year"] == "FY2026" for row in series(before, "revenue"))

    entry = upload(session, reliance, "results.pdf", results_pdf(), data_dir)
    assert entry.status == vault.ACCEPTED, entry.reject_reason

    ready, after = content(session, reliance)
    row = next(r for r in series(after, "revenue") if r["fiscal_year"] == "FY2026")
    assert float(row["value"]) == 1900 * 10**7  # Rs 1,900 crore, stored in rupees
    assert row["unit"] and row["basis"] == "CONSOLIDATED"
    # the figure is traceable to the uploaded document and its page
    assert row["source"]["document_id"] == str(entry.document_id) and row["source"]["page"]
    # and the readiness verdict moved: the requirement it settles is now met
    met = {s.req.id for s in ready.statuses if s.ok}
    was_met = {s.req.id for s in before_ready.statuses if s.ok}
    assert {"revenue", "net_profit"} <= met and not {"revenue", "net_profit"} <= was_met
    assert ready.score > before_ready.score


def test_a_rejected_upload_changes_nothing_in_the_report(session, reliance, data_dir):
    _, before = content(session, reliance)
    other = results_pdf(issuer="Tata Consultancy Services Limited")
    entry = upload(session, reliance, "other.pdf", other, data_dir)
    assert entry.status == vault.REJECTED
    _, after = content(session, reliance)
    assert series(after, "revenue") == series(before, "revenue")


def test_a_private_upload_reaches_nobody_elses_report(session, reliance, data_dir):
    entry = upload(session, reliance, "mine.pdf", results_pdf(), data_dir, licence="USER_LICENSED")
    assert entry.visibility == "PRIVATE"
    _, after = content(session, reliance)
    assert not any(r["fiscal_year"] == "FY2026" for r in series(after, "revenue"))
