"""Fixes from the post-execution audit: stale-basis readiness, website verification, document typing,
non-industry labels."""

# ruff: noqa: F811
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.acquisition import website_discovery as wd
from app.docintel import doctype
from app.models import (
    Company,
    CompanyClassification,
    CompanyWebsiteRegistry,
    Document,
    DocumentLinkEvent,
)
from app.providers.company_ir import extract_ir_links, not_a_financial_file
from app.research import industry
from app.research import readiness as rd
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.research_helpers import FY26, TODAY, evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

BANK = ("banking", "private_sector_bank")


# ---- readiness: the chosen basis stopped reporting (AVSL) ----------------------------------------------


def test_a_basis_that_stopped_reporting_is_explained_not_called_missing(seeded):
    """AVSL: consolidated figures end at FY2018, standalone ones are current."""
    from datetime import date

    c = mk_company(seeded, "Stopped consolidating", *BANK)
    old = date(2018, 3, 31)
    for metric in ("INCOME.total_income", "INCOME.net_profit"):
        evidence(seeded, c, metric, 5, end=old, basis="CONSOLIDATED")
        evidence(seeded, c, metric, 50, end=FY26, basis="STANDALONE")
    ready = rd.evaluate(seeded, c, today=TODAY)
    net = ready.by_id("net_profit")
    assert net.status == rd.STALE
    assert (
        "last reported for 31 Mar 2018" in net.reason and "standalone figures exist" in net.reason
    )
    assert net.other_basis["basis"] == "STANDALONE"
    assert ready.eligibility == rd.ASSISTED  # the data exists on the other basis: not BLOCKED
    assert any("only on the standalone basis" in r for r in ready.reasons)
    assert "no validated revenue or profit" not in " ".join(ready.reasons)


def test_a_stale_value_keeps_its_reason_when_a_held_document_is_unread(seeded):
    from datetime import date

    from tests.research_helpers import mk_document

    c = mk_company(seeded, "Old figures", *BANK)
    evidence(seeded, c, "INCOME.net_profit", 5, end=date(2018, 3, 31), basis="CONSOLIDATED")
    mk_document(seeded, c, "annual_report")
    reason = rd.evaluate(seeded, c, today=TODAY).by_id("net_profit").reason
    assert "last reported" not in reason or "older than" in reason
    assert "older than" in reason  # the specific reason survives
    assert "has not been processed yet" in reason  # and the pending document is still named


# ---- website verification ------------------------------------------------------------------------------

NAMES = {"acme technologies limited", "acme technologies"}
PROFILE = (
    "Acme Technologies Limited is an India-based company engaged in the manufacture of "
    "industrial pumps, valves, compressors, turbines and precision engineering components for "
    "refineries, power stations and chemical plants, with foundries, machining centres and "
    "assembly lines across several states."
)


def page(body, title="Acme Technologies Limited"):
    return f"<html><head><title>{title}</title></head><body><p>{body}</p></body></html>"


GOOD_BODY = (
    "Acme Technologies Limited manufactures industrial pumps, valves, compressors and turbines. "
    "Our foundries and machining centres serve refineries, power stations and chemical plants."
)


def test_a_genuine_site_that_describes_the_business_is_verified():
    outcome, signals = wd.judge_page(page(GOOD_BODY), NAMES, None, "acme.com", PROFILE)
    assert outcome == wd.VERIFIED and signals["profile_overlap"]["ratio"] >= 0.2


def test_a_name_match_on_an_unrelated_business_is_not_enough():
    """ACS Technologies: the name matched a church-software company."""
    body = (
        "Acme Technologies Limited. Church management, giving and accounting software for churches."
    )
    outcome, signals = wd.judge_page(page(body), NAMES, None, "acme.com", PROFILE)
    assert outcome == wd.AMBIGUOUS and "does not describe the business" in signals["veto"]


def test_a_printed_isin_overrides_a_thin_description_match():
    body = "Acme Technologies Limited ISIN INE123A01012. Welcome."
    outcome, _ = wd.judge_page(page(body), NAMES, "INE123A01012", "acme.com", PROFILE)
    assert outcome == wd.VERIFIED


@pytest.mark.parametrize(
    "body",
    [
        "Acme Technologies Limited, found guilty of Reverse Domain Name Hijacking. UDRP panel decision.",
        "Acme Technologies Limited. This domain is for sale. Buy this domain today.",
    ],
)
def test_a_domain_dispute_or_parked_page_is_not_the_company_site(body):
    """AGI Greenpac: hsil.com was a domain-dispute ruling."""
    outcome, signals = wd.judge_page(page(body), NAMES, None, "acme.com", None)
    assert outcome == wd.AMBIGUOUS and "dispute" in signals["veto"]


def test_a_page_in_a_script_the_check_cannot_read_goes_to_a_person():
    """AA Plus Tradelink: a Bengali page that only repeated the name."""
    body = (
        "কেজি টাইম লাইভ গেম কীভাবে কাজ করে গ্যালারি পরিচিতি রোডম্যাপ প্রশ্নোত্তর " * 5
        + " Acme Technologies Limited"
    )
    outcome, signals = wd.judge_page(
        page(body, "কেজি টাইম লাইভ গেম | Acme Technologies"), NAMES, None, "acme.com"
    )
    assert outcome == wd.AMBIGUOUS and "script" in signals["veto"]


def test_without_a_profile_the_existing_rules_apply_unchanged():
    outcome, _ = wd.judge_page(page("Acme Technologies Limited. Home."), NAMES, None, "acme.com")
    assert outcome == wd.VERIFIED


@pytest.fixture
def firm(session):
    seed_reference_data(session)
    seed_test_universe(session)
    co = session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))
    return co


def register(session, co, status=wd.VERIFIED, site="https://wrong.example.com"):
    co.website, co.investor_relations_url = site, site + "/investors"
    session.add(
        CompanyWebsiteRegistry(
            company_id=co.id,
            status=status,
            website=site,
            investor_relations_url=site + "/investors",
            discovery_source="name",
            verification={"tried": [], "reason": "name in title"},
            attempts=1,
        )
    )
    session.commit()


def test_a_withdrawn_website_keeps_its_history_and_is_not_deleted(session, firm):
    register(session, firm)
    row = wd.mark_unverified(session, firm, reason="church software", evidence={"overlap": 0.17})
    assert row.status == wd.AMBIGUOUS and row.website == "https://wrong.example.com"
    assert (
        firm.website is None and firm.investor_relations_url is None
    )  # nothing is fetched from it
    history = row.verification["review_history"][0]
    assert history["previous_status"] == wd.VERIFIED and history["reason"] == "church software"
    assert row.verification["rejected"][0]["domain"] == "example.com"


def test_a_website_set_by_hand_is_never_touched_by_a_review(session, firm):
    register(session, firm, status=wd.MANUAL)
    with pytest.raises(ValueError, match="set by hand"):
        wd.mark_unverified(session, firm, reason="x")
    assert firm.website == "https://wrong.example.com"


def test_a_rejected_domain_cannot_be_accepted_again_by_discovery(session, firm):
    register(session, firm)
    wd.mark_unverified(session, firm, reason="not theirs")
    verdict = wd.check_candidate(
        session, firm, "https://www.example.com", client=None, names=NAMES, isin=None
    )
    assert verdict.outcome == "REJECTED" and "rejected" in verdict.reason


def test_review_withdraws_a_site_that_no_longer_passes_and_confirms_one_that_does(session, firm):
    class Client:
        def __init__(self, html):
            self.html = html

        def fetch(self, url, **kw):
            from app.acquisition.http import FetchResult, FetchStatus

            r = FetchResult(url, FetchStatus.OK, 200, self.html.encode())
            return r

    register(session, firm, site="https://reliance.example.com")
    good = page(
        "Reliance Industries Limited. Refining, petrochemicals, retail and telecom.",
        "Reliance Industries Limited",
    )
    ok = wd.review_registered(session, firm, Client(good))
    assert ok["outcome"] == "CONFIRMED"
    assert (
        session.get(CompanyWebsiteRegistry, firm.id).verification["review"]["outcome"]
        == "CONFIRMED"
    )
    parked = page(
        "Reliance Industries Limited. This domain is for sale.", "Reliance Industries Limited"
    )
    gone = wd.review_registered(session, firm, Client(parked))
    assert gone["outcome"] == "WITHDRAWN" and firm.website is None


# ---- corporate actions whose details are a list (Upstox bonus / dividend) -------------------------------


def test_bonus_events_with_list_style_details_are_still_adjusted():
    """Two companies' calculation failed: details was a list of name/value pairs, not a dict."""
    from datetime import date
    from types import SimpleNamespace

    from app.calc.adjust import events_from_actions

    def action(details):
        return SimpleNamespace(
            id=1,
            action_type="Bonus",
            ratio_text="1:1",
            effective_date=date(2026, 9, 28),
            provider_code="UPSTOX_FUNDAMENTALS",
            source_url="https://provider.invalid",
            details=details,
        )

    listy = [{"name": "Ex Bonus date", "value": "28 Sep 2026"}, {"name": "Ratio", "value": "1:1"}]
    for details in (listy, None, {}, {"verification": {"checked": True}}, {"verification": "odd"}):
        events, skipped = events_from_actions([action(details)])
        assert len(events) == 1 and not skipped, details
        assert events[0].factor is not None
    assert events_from_actions([action({"verification": {"checked": True}})])[0][0].source[
        "checked"
    ]


# ---- provider label and description ---------------------------------------------------------------


@pytest.mark.parametrize("label", ["DVR", "Miscellaneous", "dvr", "Others"])
def test_a_label_that_is_not_an_industry_builds_no_group(session, firm, label):
    session.add(
        CompanyClassification(
            company_id=firm.id,
            scheme="UPSTOX_SECTOR",
            label=label,
            retrieved_at=datetime.now(UTC),
            source="test",
        )
    )
    session.commit()
    out = industry.industry_group(session, firm, None, "CONSOLIDATED", "x")
    assert out["available"] is False and "not an industry" in out["reason"]


def test_the_business_description_is_stored_with_the_label(session, firm):
    from app.providers import classification

    body = {"data": {"sector": "Refineries", "company_profile": "Reliance is a conglomerate. " * 4}}
    row = classification.store(session, firm, body)
    assert row.label == "Refineries" and row.description.startswith("Reliance is a conglomerate")
    assert len(row.description) <= classification.MAX_DESCRIPTION
    assert wd.profile_text(session, firm.id) == row.description


# ---- document typing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Secretarial Compliance Report FY26",
        "CG Report",
        "Corporate Brochure",
        "Annual Return 2025",
        "Code of Conduct",
        "Corporate Governance Report",
    ],
)
def test_links_to_compliance_files_are_never_financial_documents(text):
    assert not_a_financial_file(text)
    html = f'<a href="https://x.com/docs/{text.replace(" ", "-")}.pdf">{text}</a>'
    assert extract_ir_links(html, "https://x.com/investors") == []


@pytest.mark.parametrize(
    "text",
    [
        "Annual Report 2025-26",
        "Q2 FY27 Results",
        "Financial Results September 2026",
        "Investor Presentation",
    ],
)
def test_real_financial_files_are_still_recognised(text):
    html = f'<a href="https://x.com/docs/{text.replace(" ", "-")}.pdf">{text}</a>'
    assert len(extract_ir_links(html, "https://x.com/investors")) == 1


def test_a_short_file_claimed_as_an_annual_report_is_retyped_with_its_reason():
    d = doctype.judge(
        "annual_report", "Download Report", "https://x.com/a.pdf", 5, "Family Book of Records"
    )
    assert d.changed and d.doc_type == "other" and "5 pages" in d.reason


def test_a_long_file_or_one_titled_annual_report_keeps_its_type():
    assert not doctype.judge("annual_report", None, "https://x.com/a.pdf", 120, "Notice").changed
    assert not doctype.judge(
        "annual_report", None, "https://x.com/a.pdf", 6, "Annual Report 2025"
    ).changed


def test_a_scan_with_no_text_keeps_its_claimed_type_for_ocr():
    d = doctype.judge("annual_report", None, "https://x.com/a.pdf", 5, "")
    assert not d.changed and d.verified is False


def test_a_title_naming_a_compliance_report_overrides_the_page_it_came_from():
    d = doctype.judge(
        "annual_report", "Secretarial Compliance Report FY26", "https://x.com/s.pdf", 6, ""
    )
    assert d.changed and "secretarial" in d.reason


def test_quarterly_results_are_not_retyped_for_lacking_words(session):
    """BPCL's statements are pages of an annual report; absence of words proves nothing."""
    d = doctype.judge(
        "quarterly_result",
        "Standalone Financial Statements",
        "https://x.com/s.pdf",
        49,
        "Business Overview ESG",
    )
    assert not d.changed


def test_retyping_keeps_the_row_and_writes_an_audit_event(session, firm, tmp_path):
    from tests.test_research_vault import make_pdf

    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(make_pdf([(60, 60, "Family Book of Records")]))
    doc = Document(
        company_id=firm.id,
        url="https://x.com/s.pdf",
        title="Download",
        doc_type="annual_report",
        content_type="application/pdf",
        sha256="a" * 64,
        size_bytes=10,
        storage_path="documents/aa/x.pdf",
        association_status="UNVERIFIED",
    )
    session.add(doc)
    session.flush()
    decision = doctype.review_type(session, doc, pdf)
    assert decision.changed and doc.doc_type == "other" and doc.company_id == firm.id
    event = session.scalar(select(DocumentLinkEvent).where(DocumentLinkEvent.document_id == doc.id))
    assert event.action == "RETYPED" and event.evidence["from"] == "annual_report"
    # provenance columns are untouched by a retype
    assert doc.sha256 == "a" * 64 and doc.url == "https://x.com/s.pdf"
