"""Upstox line names -> canonical keys, and the duplicate-event storage rule."""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import Company, CorporateAction
from app.providers.persistence import store_corporate_actions
from app.providers.upstox import ActionRow
from app.providers.upstox_map import DEFINITIONAL, EXACT, UPSTOX_CODE, canonical_line
from app.seeds.loader import seed_reference_data, seed_test_universe


def test_exact_lines_are_always_filed_under_the_canonical_key():
    for covered in (True, False):
        assert canonical_line(UPSTOX_CODE, "INCOME", "Profit Before Tax", covered) == (
            "profit_before_tax"
        )
    # Upstox's "Equity Capital" is total equity, never share capital
    assert canonical_line(UPSTOX_CODE, "BALANCE_SHEET", "Equity Capital", False) == "total_equity"
    assert "equity_share_capital" not in {*EXACT.values(), *DEFINITIONAL.values()}


def test_definitional_lines_are_only_canonical_where_no_filing_covers_the_statement():
    assert canonical_line(UPSTOX_CODE, "INCOME", "Revenue", False) == "revenue_from_operations"
    assert canonical_line(UPSTOX_CODE, "INCOME", "Revenue", True) == "Revenue"
    assert canonical_line(UPSTOX_CODE, "INCOME", "Total Revenue", True) == "Total Revenue"


def test_unknown_lines_and_other_providers_keep_their_names():
    assert canonical_line(UPSTOX_CODE, "INCOME", "Total Expenses", False) == "Total Expenses"
    assert canonical_line("DOCUMENT_EXTRACTION", "INCOME", "Revenue", False) == "Revenue"


def test_two_events_on_one_date_are_both_stored(session):
    seed_reference_data(session)
    seed_test_universe(session)
    company = session.scalars(select(Company)).first()
    rows = [
        ActionRow("Dividend", date(2026, 1, 16), Decimal("11"), "", None),
        ActionRow("Dividend", date(2026, 1, 16), Decimal("46"), "", None),
        ActionRow("Dividend", date(2026, 1, 16), Decimal("46"), "", None),
    ]
    kwargs = {"source_url": "https://x", "retrieved_at": datetime.now(UTC), "payload_id": None}
    store_corporate_actions(session, company.id, UPSTOX_CODE, rows, **kwargs)
    stored = session.scalars(
        select(CorporateAction).where(CorporateAction.company_id == company.id)
    ).all()
    assert sorted(a.amount for a in stored) == [Decimal("11"), Decimal("46"), Decimal("46")]
    # storing the same batch again changes nothing
    store_corporate_actions(session, company.id, UPSTOX_CODE, rows, **kwargs)
    assert len(session.scalars(select(CorporateAction)).all()) == 3
