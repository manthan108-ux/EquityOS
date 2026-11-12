"""Suspected unadjusted splits/bonuses are carried into the report, never applied."""

from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models import (
    AdjustmentKind,
    AdjustmentOrigin,
    AdjustmentStatus,
    Company,
    PriceAdjustment,
    Security,
)
from app.research.pdf import render
from app.research.report import _price_notes
from app.seeds.loader import seed_reference_data, seed_test_universe


@pytest.fixture
def reliance(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


def add(session, company, confidence, ex=date(2024, 10, 28), status=AdjustmentStatus.REPORTED_ONLY):
    sec = session.scalars(select(Security).where(Security.company_id == company.id)).first()
    session.add(
        PriceAdjustment(
            security_id=sec.id,
            ex_date=ex,
            factor=D("1.99025743"),
            kind=AdjustmentKind.UNEXPLAINED,
            origin=AdjustmentOrigin.IMPLIED,
            status=status,
            confidence=confidence,
            prior_close=D("2655.7"),
            reference_close=D("2655.7"),
            detail="SUSPECTED unadjusted split/bonus: close fell from 2655.7 to 1334.35. NOT adjusted",
            built_at=datetime.now(UTC),
        )
    )
    session.flush()


def test_a_suspected_unadjusted_event_is_listed_for_the_report(session, reliance):
    add(session, reliance, "PRICE_GAP_ONLY")
    add(session, reliance, "NOT_CLASSIFIED", ex=date(2026, 2, 2))  # an ordinary reported gap
    notes = _price_notes(session, reliance.id)
    assert [n["date"] for n in notes] == ["2024-10-28"]
    assert "NOT adjusted" in notes[0]["note"]


def test_nothing_is_listed_when_there_is_no_suspicion(session, reliance):
    assert _price_notes(session, reliance.id) == []


def test_the_pdf_prints_the_price_history_note():
    content = {
        "meta": {"company": "Reliance", "banner": "b", "as_of": "2026-11-10", "complete": False, "preferences": {}},
        "readiness": {"score": 50, "eligibility": "ASSISTED", "confidence": "LOW", "note": "n", "components": {}},
        "sections": [],
        "limitations": {
            "missing_critical": [], "missing_important": [], "missing_optional": [],
            "unavailable_calculations": [], "incomplete_valuation": [], "unavailable_scores": None,
            "limits_on_conclusions": [],
            "price_history_notes": [{"symbol": "RELIANCE", "date": "2024-10-28", "factor": "1.99", "note": "SUSPECTED unadjusted split/bonus"}],
        },
    }  # fmt: skip
    import pymupdf

    data = render(
        content, version=1, mode="AVAILABLE_EVIDENCE", report_id="a" * 36, content_hash="b" * 64
    )
    text = "\n".join(p.get_text() for p in pymupdf.open(stream=data, filetype="pdf"))
    assert "Price history" in text and "28/10/2024" in text and "SUSPECTED" in text
