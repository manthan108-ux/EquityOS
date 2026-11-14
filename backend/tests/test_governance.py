# ruff: noqa: F811, E501
"""Management, governance and capital allocation."""

import json
from datetime import date
from decimal import Decimal as D

from app.models import CorporateAction
from app.research import governance as gv
from app.research import readiness as rd
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

PAD = " The year saw steady operations across the group and its many offices."
CRORE = 10_000_000
Y = [date(2023, 3, 31), date(2024, 3, 31), date(2025, 3, 31), date(2026, 3, 31)]


def page(*sentences, number=9):
    return [(number, " ".join(s + PAD for s in sentences))]


def topic(pages, key):
    return [i["text"] for i in gv.extract(pages)[key]]


# ---- quoted topics --------------------------------------------------------------------------------------------


def test_background_and_tenure_are_quoted_not_invented():
    pages = page(
        "Mr Ravi Kumar is the Managing Director and CEO of the Company and has over 30 years of experience in banking.",
        "Mr Ravi Kumar was appointed as Managing Director and CEO of the Company with effect from 1 April 2021.",
    )
    assert any("over 30 years of experience" in s for s in topic(pages, "background"))
    assert any("was appointed as Managing Director" in s for s in topic(pages, "tenure"))
    assert gv.extract(pages)["tenure"][0]["source"] == "DOCUMENT_QUOTE"


def test_remuneration_and_attendance_sentences_are_not_taken_as_background_or_tenure():
    pages = page(
        "Mr Ravi Kumar, Managing Director, was paid remuneration of Rs 5 crore and attended all meetings of the Board in the year.",
    )
    assert topic(pages, "background") == [] and topic(pages, "tenure") == []


def test_related_party_and_dividend_policy_statements_are_filed_under_their_topics():
    pages = page(
        "All related party transactions entered into during the year were at arm's length and in the ordinary course of business.",
        "The Board recommended a final dividend of Rs 12 per equity share and approved a buyback of equity shares.",
        "The Company invested in a subsidiary company to expand its platform in the United States during the year.",
    )
    assert topic(pages, "related_party")
    assert topic(pages, "dividend_buyback")
    assert topic(pages, "acquisitions")


def test_forward_looking_disclaimers_are_not_quoted():
    pages = page(
        "Statements on the corporate governance outlook are forward-looking statements and projections based on current expectations."
    )
    assert topic(pages, "governance") == []


# ---- board composition -----------------------------------------------------------------------------------------


def test_board_size_and_independent_directors_are_parsed_only_from_one_sentence_that_states_both():
    pages = page(
        "The Board of the Company comprises 10 Directors, of which 6 are Independent Directors including two Woman Directors."
    )
    b = gv.parse_board(pages)
    assert b["source"] == "PARSED" and b["board_size"] == 10 and b["independent"] == 6
    assert D(b["independent_share"]) == D("60.0") and b["woman_director_stated"] and b["page"] == 9


def test_number_words_are_read_and_implausible_numbers_are_refused():
    ok = gv.parse_board(
        page("The Board comprises nine Directors of whom five are Independent Directors.")
    )
    assert ok["board_size"] == 9 and ok["independent"] == 5
    assert (
        gv.parse_board(page("The Board comprises 4 Directors of whom 9 are Independent Directors."))
        is None
    )
    assert (
        gv.parse_board(page("The Board has several Independent Directors and many members."))
        is None
    )


def test_committees_are_checked_against_the_report_text():
    pages = [
        (5, "The Audit Committee met six times."),
        (8, "The Nomination and Remuneration Committee reviewed pay."),
        (11, "The Risk Management Committee oversees risk."),
    ]
    c = {x["committee"]: x for x in gv.committees(pages)}
    assert (
        c["Audit Committee"]["pages"] == [5]
        and not c["Stakeholders Relationship Committee"]["mentioned"]
    )


# ---- promoter holding ------------------------------------------------------------------------------------------


def holdings(session, values, name="Promoter Co"):
    co = mk_company(session, name)
    qs = [
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
        date(2026, 3, 31),
    ]
    for end, v in zip(qs, values, strict=False):
        evidence(
            session,
            co,
            "SHAREHOLDING.promoters.percent",
            v,
            end=end,
            basis=None,
            unit="percent",
            ptype="QUARTERLY",
        )
    return rd.load_store(session, co.id, set(gv.NAMES))


def test_promoter_holding_shows_the_series_the_change_and_the_direction(seeded):
    p = gv.promoter_holding(holdings(seeded, [52.0, 51.5, 50.0, 48.0, 46.0]))
    assert p["available"] and D(p["change_pp"]) == D("-6.00") and p["direction"] == "FALLING"
    assert len(p["series"]) == 5 and len(p["change_evidence_ids"]) == 2
    assert p["pledge"]["available"] is False and "not stored" in p["pledge"]["reason"]


def test_no_promoter_holding_means_widely_held_not_missing(seeded):
    p = gv.promoter_holding(holdings(seeded, [0.0, 0.0, 0.0], "Widely Held Co"))
    assert p["available"] and "widely held" in p["note"]


def test_a_company_with_no_shareholding_pattern_says_so(seeded):
    p = gv.promoter_holding(rd.load_store(seeded, mk_company(seeded, "Bare Co").id, set(gv.NAMES)))
    assert p["available"] is False and "no promoter shareholding" in p["reason"]


# ---- capital allocation ----------------------------------------------------------------------------------------


def alloc_store(session, name="Alloc Co", with_buyback=False):
    co = mk_company(session, name)
    series = {
        "CASH_FLOW.net_cash_from_operating_activities": [1000, 1200, 1500, 1800],
        "CASH_FLOW.capital_expenditure": [-300, -400, -500, -600],
        "CASH_FLOW.dividends_paid": [-200, -300, -900, -1000],
        "INCOME.attributable_profit": [800, 900, 1000, 1100],
    }
    if with_buyback:
        series["CASH_FLOW.buyback_of_shares"] = [None, None, -500, None]
    for metric, values in series.items():
        for end, v in zip(Y, values, strict=True):
            if v is not None:
                evidence(
                    session,
                    co,
                    metric,
                    v * CRORE,
                    end=end,
                    basis="CONSOLIDATED",
                    unit="inr",
                    ptype="ANNUAL",
                )
    return rd.load_store(session, co.id, set(gv.NAMES))


def test_uses_of_cash_are_shares_of_operating_cash_flow_with_payout(seeded):
    a = gv.allocation(alloc_store(seeded), "CONSOLIDATED")
    last = a["years"][-1]
    assert a["available"] and last["fy"] == "FY2026"
    assert D(last["uses"]["capex"]["pct_of_cfo"]) == D("33.3") and D(
        last["uses"]["dividends"]["pct_of_cfo"]
    ) == D("55.6")
    assert last["uses"]["buybacks"] is None and "buybacks" in a["not_stored"]
    assert D(last["payout_pct"]) == D("90.9") and last["evidence_ids"]


def test_returns_that_exceed_free_cash_flow_are_flagged_by_year(seeded):
    a = gv.allocation(alloc_store(seeded, with_buyback=True), "CONSOLIDATED")
    by = {y["fy"]: y.get("returned_vs_fcf") for y in a["years"]}
    # FY2025: dividends 900 + buyback 500 = 1400 vs FCF 1500-500 = 1000
    assert by["FY2025"] == "EXCEEDS_FCF" and by["FY2024"] == "COVERED" and by["FY2026"] == "COVERED"


def test_no_uses_of_cash_means_not_available_with_a_reason(seeded):
    a = gv.allocation(
        rd.load_store(seeded, mk_company(seeded, "No Cash Co").id, set(gv.NAMES)), "CONSOLIDATED"
    )
    assert a["available"] is False and "not stored" in a["reason"]


def test_dividend_announcements_come_from_the_exchange_feed_with_their_window(seeded):
    co = mk_company(seeded, "Dividend Co")
    for ex, amount, kind in ((date(2026, 6, 5), 6, "Final"), (date(2026, 1, 20), 12, "Interim")):
        seeded.add(CorporateAction(company_id=co.id, provider_code="UPSTOX_FUNDAMENTALS", action_type="Dividend", effective_date=ex, amount=D(amount), ratio_text=kind, details=[{"name": "Dividend type", "value": kind}, {"name": "Details", "value": f"Rs.{amount} per share"}], source_url="https://example.test/x", retrieved_at=date(2026, 11, 1)))  # fmt: skip
    seeded.flush()
    h = gv.dividend_history(seeded, co.id)
    assert h["available"] and h["count"] == 2 and h["events"][0]["ex_date"] == "2026-06-05"
    assert h["events"][0]["kind"] == "Final" and D(h["events"][0]["amount_per_share"]) == D(6)
    assert "limited window" in h["note"] and not h["buybacks_stored"]


def test_no_dividend_announcement_is_stated_not_assumed_zero(seeded):
    h = gv.dividend_history(seeded, mk_company(seeded, "No Dividend Co").id)
    assert h["available"] is False and "no dividend" in h["reason"]


# ---- capex and acquisitions ------------------------------------------------------------------------------------


def test_capex_against_depreciation_and_goodwill_growth_are_observed_without_claiming_causes(
    seeded,
):
    co = mk_company(seeded, "Invest Co")
    for metric, vals in {"CASH_FLOW.capital_expenditure": [-300, -400], "INCOME.depreciation_and_amortisation": [200, 250], "BALANCE_SHEET.goodwill": [100, 160], "BALANCE.owners_equity": [800, 1000], "CASH_FLOW.acquisition_of_subsidiaries_and_businesses": [None, -90]}.items():  # fmt: skip
        for end, v in zip(Y[2:], vals, strict=True):
            if v is not None:
                evidence(
                    seeded,
                    co,
                    metric,
                    v * CRORE,
                    end=end,
                    basis="CONSOLIDATED",
                    unit="inr",
                    ptype="ANNUAL",
                )
    store = rd.load_store(seeded, co.id, set(gv.NAMES))
    out = gv.capex_and_acquisitions(store, "CONSOLIDATED", {"health": {"returns": {"metrics": {"roce": {"available": True, "series": [{"fy": "FY2025", "value": "14", "evidence_id": 1}, {"fy": "FY2026", "value": "16", "evidence_id": 2}]}}}}})  # fmt: skip
    assert out["available"] and D(out["capex"][-1]["capex_to_depreciation"]) == D("1.60")
    assert D(out["goodwill"]["change_pct"]) == D("60.0") and D(
        out["goodwill"]["share_of_equity_pct"]
    ) == D("16.0")
    assert out["acquisitions"][0]["fy"] == "FY2026"
    text = " ".join(o["text"] for o in out["observations"])
    assert "growing the asset base" in text and "no causal link is claimed" in text


def test_nothing_stored_means_not_available(seeded):
    out = gv.capex_and_acquisitions(
        rd.load_store(seeded, mk_company(seeded, "Empty Co").id, set(gv.NAMES)), "CONSOLIDATED", {}
    )
    assert out["available"] is False and "no capital-expenditure" in out["reason"]


# ---- the checklist ---------------------------------------------------------------------------------------------


def run_checklist(**kw):
    base = {"promoter": {"available": False, "reason": "none", "pledge": {"reason": "not stored"}}, "board": None, "comms": [], "quotes": {}, "auditor_flags": None, "alloc": {"available": False}, "pages_available": False}  # fmt: skip
    base.update(kw)
    return {i["key"]: i for i in gv.checklist(**base)}


def test_independence_rule_is_one_third():
    ok = run_checklist(
        board={
            "independent_share": "33.4",
            "independent": 4,
            "board_size": 12,
            "woman_director_stated": True,
            "page": 3,
        }
    )
    low = run_checklist(
        board={
            "independent_share": "30.0",
            "independent": 3,
            "board_size": 10,
            "woman_director_stated": False,
            "page": 3,
        }
    )
    assert ok["independence"]["status"] == "PASS" and ok["women"]["status"] == "PASS"
    assert low["independence"]["status"] == "FLAG" and low["women"]["status"] == "UNKNOWN"


def test_unknown_is_not_a_failure_when_the_documents_do_not_show_it():
    c = run_checklist()
    assert (
        c["independence"]["status"] == "UNKNOWN"
        and "no annual report" in c["independence"]["detail"]
    )
    assert c["auditor"]["status"] == "UNKNOWN" and c["related_party"]["status"] == "UNKNOWN"
    assert c["promoter_pledge"]["status"] == "UNKNOWN"


def test_auditor_flags_and_a_large_promoter_fall_are_flagged_with_their_evidence():
    c = run_checklist(auditor_flags=[{"label": "Emphasis of matter"}], promoter={"available": True, "change_pp": "-6.2", "from": "2025-03-31", "to": "2026-03-31", "latest": {"value": "44.0"}, "change_evidence_ids": [1, 2], "pledge": {"reason": "x"}})  # fmt: skip
    assert c["auditor"]["status"] == "FLAG" and "Emphasis of matter" in c["auditor"]["detail"]
    assert c["promoter_holding"]["status"] == "FLAG" and c["promoter_holding"]["evidence_ids"] == [
        1,
        2,
    ]
    clean = run_checklist(auditor_flags=[])
    assert clean["auditor"]["status"] == "PASS"


def test_related_party_passes_only_when_the_arms_length_statement_is_present():
    yes = run_checklist(
        quotes={
            "related_party": [
                {"text": "All related party transactions were at arm's length.", "page": 5}
            ]
        }
    )
    no = run_checklist(
        quotes={
            "related_party": [
                {
                    "text": "Related party transactions are disclosed in the notes to accounts.",
                    "page": 5,
                }
            ]
        }
    )
    assert yes["related_party"]["status"] == "PASS" and no["related_party"]["status"] == "UNKNOWN"


# ---- the whole -------------------------------------------------------------------------------------------------


def test_the_whole_analysis_has_no_score_and_keeps_quotes_apart_from_evidence(seeded):
    co = mk_company(seeded, "Whole Co")
    out = gv.build(seeded, co.id, alloc_store(seeded, "Whole Alloc"), "CONSOLIDATED", {})
    assert out["available"] and "score" not in json.dumps(out["checklist"]["counts"]).lower()
    assert set(out["checklist"]["counts"]) == {"PASS", "FLAG", "UNKNOWN"}
    assert (
        out["capital_allocation"]["uses_of_cash"]["available"]
        and out["management"]["track_record"]["available"] is False
    )
    assert any("composite" in d or "no composite score" in d for d in out["definitions"])


def test_a_company_with_nothing_stored_is_not_available(seeded):
    co = mk_company(seeded, "Nothing Co")
    out = gv.build(seeded, co.id, rd.load_store(seeded, co.id, set(gv.NAMES)), "CONSOLIDATED", {})
    assert out["available"] is False


def test_the_pdf_shows_the_governance_section_and_omits_it_when_empty(seeded):
    from tests.test_report_context import _axis, _flat

    co = mk_company(seeded, "PDF Gov Co")
    content = _axis()
    content["analysis"]["governance"] = json.loads(
        json.dumps(
            gv.build(
                seeded,
                co.id,
                alloc_store(seeded, "PDF Alloc"),
                "CONSOLIDATED",
                {},
                auditor_flags=[],
            )
        )
    )
    flat = _flat(content)
    assert (
        "Management, governance and capital allocation" in flat
        and "Corporate governance assessment" in flat
    )
    assert "Capital allocation" in flat and "Not available" in flat
    empty = _axis()
    empty["analysis"]["governance"] = {"available": False}
    assert "Management, governance and capital allocation" not in _flat(empty)


def test_a_denial_that_transactions_were_at_arms_length_is_not_a_pass():
    no = run_checklist(
        quotes={
            "related_party": [
                {
                    "text": "Details of contracts or transactions not at arm's length basis are given below.",
                    "page": 7,
                }
            ]
        }
    )
    assert no["related_party"]["status"] == "UNKNOWN"
    yes = run_checklist(
        quotes={
            "related_party": [
                {
                    "text": "All related party transactions were on an arm's length basis and in the ordinary course of business.",
                    "page": 7,
                }
            ]
        }
    )
    assert yes["related_party"]["status"] == "PASS"


def test_a_dividend_declaration_is_not_filed_as_board_composition():
    pages = page(
        "The Board of Directors has declared two interim dividends during the year totalling Rs 17.50 per share."
    )
    assert topic(pages, "board") == []
