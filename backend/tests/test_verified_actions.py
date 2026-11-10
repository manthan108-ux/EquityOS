"""Bonus issues are recorded only when a stored document AND the official prices both back them."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.calc.adjust import rebuild_adjustments
from app.calc.engine import calculate_company
from app.calc.verified_actions import PROVIDER, find_bonus_in_documents, verify_company
from app.models import (
    AdjustedPrice,
    CorporateAction,
    DocumentExtraction,
    DocumentPage,
    EodPrice,
    ExtractionStatus,
    PriceAdjustment,
)
from tests.research_helpers import mk_document
from tests.test_calc_db import NOW, TODAY, add_prices, co, listing  # noqa: F401

D = Decimal


def with_halving(session, company, closes=None):
    sec = listing(session, company)
    add_prices(session, sec, closes or [200] * 15 + [100] * 15)
    first_low = session.scalar(
        select(EodPrice.trade_date)
        .where(EodPrice.security_id == sec.id, EodPrice.close == D(100))
        .order_by(EodPrice.trade_date)
    )
    return sec, first_low


def stated_in_document(session, company, text):
    doc = mk_document(session, company, doc_type="annual_report")
    ex = DocumentExtraction(
        document_id=doc.id, extractor_version="t", status=ExtractionStatus.COMPLETED, issues=[]
    )
    session.add(ex)
    session.flush()
    session.add(DocumentPage(extraction_id=ex.id, page_number=7, char_count=len(text), text=text,
                             text_method="text_layer"))  # fmt: skip
    session.flush()
    return doc


def sentence(ratio, record):
    return (
        f"During the year the Company issued bonus equity shares in the proportion of {ratio} "
        f"for every share held, the record date for which was {record}."
    )


def long_date(d):
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def test_a_bonus_stated_in_a_document_and_seen_in_the_prices_is_recorded(session, co):  # noqa: F811
    sec, ex_date = with_halving(session, co)
    doc = stated_in_document(session, co, sentence("1:1", long_date(ex_date + timedelta(days=1))))
    results = verify_company(session, co.id)
    assert [r.status for r in results] == ["VERIFIED"]
    action = session.scalar(select(CorporateAction).where(CorporateAction.company_id == co.id))
    assert action.provider_code == PROVIDER and action.action_type == "Bonus"
    assert action.effective_date == ex_date and action.ratio_text == "1:1"
    v = action.details["verification"]
    assert v["document_id"] == str(doc.id) and v["page"] == 7
    assert "bonus equity shares" in v["quote"] and v["record_date"] == str(
        ex_date + timedelta(days=1)
    )


def test_a_document_without_a_matching_price_gap_records_nothing(session, co):  # noqa: F811
    with_halving(session, co, [200] * 30)  # no gap in the prices
    stated_in_document(session, co, sentence("1:1", "October 5, 2026"))
    results = verify_company(session, co.id)
    assert [r.status for r in results] == ["NO_PRICE_EVIDENCE"]
    assert "nothing recorded" in results[0].reason
    assert (
        session.scalar(select(CorporateAction).where(CorporateAction.company_id == co.id)) is None
    )


def test_a_price_gap_without_a_document_records_nothing(session, co):  # noqa: F811
    with_halving(session, co)
    results = verify_company(session, co.id)
    assert [r.status for r in results] == ["NO_DOCUMENT"]
    assert (
        session.scalar(select(CorporateAction).where(CorporateAction.company_id == co.id)) is None
    )


def test_a_ratio_that_does_not_match_the_price_factor_is_not_recorded(session, co):  # noqa: F811
    _sec, ex_date = with_halving(session, co)  # prices halve: factor 2
    stated_in_document(session, co, sentence("1:2", long_date(ex_date + timedelta(days=1))))
    assert [r.status for r in verify_company(session, co.id)] == ["NO_PRICE_EVIDENCE"]


def test_a_sentence_without_a_record_date_is_not_evidence(session, co):  # noqa: F811
    with_halving(session, co)
    stated_in_document(
        session, co, "The Company issued bonus shares in the proportion of 1:1 last year."
    )
    assert find_bonus_in_documents(session, co.id) == []


def test_verifying_twice_does_not_duplicate_the_action(session, co):  # noqa: F811
    _sec, ex_date = with_halving(session, co)
    stated_in_document(session, co, sentence("1:1", long_date(ex_date + timedelta(days=1))))
    verify_company(session, co.id)
    assert [r.status for r in verify_company(session, co.id)] == ["ALREADY_RECORDED"]
    rows = session.scalars(select(CorporateAction).where(CorporateAction.company_id == co.id)).all()
    assert len(rows) == 1


def test_adjusted_prices_are_stored_separately_with_ratio_date_factor_and_source(session, co):  # noqa: F811
    sec, ex_date = with_halving(session, co)
    stated_in_document(session, co, sentence("1:1", long_date(ex_date + timedelta(days=1))))
    raw_before = [
        (p.trade_date, p.close) for p in session.scalars(select(EodPrice).order_by(EodPrice.id))
    ]
    calculate_company(session, co.id, today=TODAY, now=NOW)
    raw_after = [
        (p.trade_date, p.close) for p in session.scalars(select(EodPrice).order_by(EodPrice.id))
    ]
    assert raw_before == raw_after  # the official prices are untouched

    before_bar = session.scalar(
        select(AdjustedPrice).where(
            AdjustedPrice.security_id == sec.id, AdjustedPrice.trade_date < ex_date
        ).order_by(AdjustedPrice.trade_date.desc())
    )  # fmt: skip
    assert before_bar.raw_close == D(200) and before_bar.close == D(100)
    assert before_bar.cumulative_factor == D(2)
    ev = before_bar.events[0]
    assert ev["ex_date"] == str(ex_date) and ev["ratio_text"] == "1:1" and ev["factor"] == "2"
    assert ev["origin"] == "CORPORATE_ACTION"
    assert ev["source"]["provider"] == PROVIDER and ev["source"]["page"] == 7

    after_bar = session.scalar(
        select(AdjustedPrice).where(
            AdjustedPrice.security_id == sec.id, AdjustedPrice.trade_date == ex_date
        )
    )
    assert after_bar.close == after_bar.raw_close and after_bar.events == []

    applied = session.scalar(
        select(PriceAdjustment).where(
            PriceAdjustment.security_id == sec.id, PriceAdjustment.status == "APPLIED"
        )
    )
    assert applied.ratio_text == "1:1" and applied.source["page"] == 7


def test_the_explained_gap_is_no_longer_reported_as_unexplained(session, co):  # noqa: F811
    _sec, ex_date = with_halving(session, co)
    stated_in_document(session, co, sentence("1:1", long_date(ex_date + timedelta(days=1))))
    verify_company(session, co.id)
    report = rebuild_adjustments(session, co.id, now=NOW)
    assert report.unexplained() == []


def test_without_a_verified_action_the_gap_stays_suspected_and_nothing_is_adjusted(session, co):  # noqa: F811
    sec, _ = with_halving(session, co)
    report = rebuild_adjustments(session, co.id, now=NOW)
    assert report.applied() == [] and len(report.unexplained()) == 1
    bars = session.scalars(select(AdjustedPrice).where(AdjustedPrice.security_id == sec.id)).all()
    assert bars and all(b.close == b.raw_close and b.cumulative_factor == 1 for b in bars)
