"""P/B pairs the share count with owners' equity on the SAME accounting basis (never mixed)."""

from datetime import date

import pytest
from sqlalchemy import select

from app.calc.engine import calculate_company
from app.models import EvidenceRecord
from tests.test_calc_db import NOW, TODAY, add_prices, co, listing  # noqa: F401
from tests.test_sector import _record

FY26 = date(2026, 3, 31)


def stored(session, company, metric, value, unit, basis, *, ptype="ANNUAL", end=FY26):
    return _record(
        session, company, metric, value, unit, end=end, ptype=ptype, basis=basis,
        kind="CALCULATION",
    )  # fmt: skip


def run(session, company):
    return calculate_company(session, company.id, today=TODAY, now=NOW)


def metric(session, company, name):
    return session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company.id,
            EvidenceRecord.metric == name,
            EvidenceRecord.status == "ACTIVE",
        )
    )


@pytest.fixture
def priced(session, co):  # noqa: F811
    add_prices(session, listing(session, co), [300] * 30)
    return co


def test_a_consolidated_share_count_is_not_paired_with_standalone_equity(session, priced):
    stored(session, priced, "BALANCE.shares_outstanding", 10_000_000, "shares", "CONSOLIDATED")
    stored(session, priced, "BALANCE.owners_equity", 6_000_000_000, "inr", "STANDALONE")
    report = run(session, priced)
    assert metric(session, priced, "VALUATION.market_cap") is not None
    assert metric(session, priced, "VALUATION.price_to_book") is None  # unavailable, not estimated
    gap = next(g for g in report.gaps if g.metric == "VALUATION.price_to_book")
    assert "basis mismatch" in gap.reason and "CONSOLIDATED basis" in gap.reason
    assert (
        "only STANDALONE equity" in gap.reason and "unavailable rather than estimated" in gap.reason
    )


def test_the_matching_basis_is_found_even_when_the_other_basis_is_newer(session, priced):
    stored(session, priced, "BALANCE.shares_outstanding", 10_000_000, "shares", "CONSOLIDATED")
    stored(
        session,
        priced,
        "BALANCE.owners_equity",
        6_000_000_000,
        "inr",
        "CONSOLIDATED",
        end=date(2025, 3, 31),
    )
    stored(session, priced, "BALANCE.owners_equity", 9_000_000_000, "inr", "STANDALONE")  # newer
    run(session, priced)
    pb = metric(session, priced, "VALUATION.price_to_book")
    assert pb is not None and pb.basis == "CONSOLIDATED"
    assert str(pb.value.normalize()) == "0.5"  # 3,000,000,000 / 6,000,000,000
    assert any("CONSOLIDATED" in n for n in pb.details["notes"])


def test_a_standalone_share_count_pairs_with_standalone_equity(session, priced):
    stored(session, priced, "BALANCE.shares_outstanding", 10_000_000, "shares", "STANDALONE")
    stored(session, priced, "BALANCE.owners_equity", 6_000_000_000, "inr", "STANDALONE")
    stored(session, priced, "BALANCE.owners_equity", 5_000_000_000, "inr", "CONSOLIDATED")
    run(session, priced)
    pb = metric(session, priced, "VALUATION.price_to_book")
    assert pb.basis == "STANDALONE" and str(pb.value.normalize()) == "0.5"


def test_the_share_count_date_and_basis_are_recorded_on_the_market_cap(session, priced):
    stored(session, priced, "BALANCE.shares_outstanding", 10_000_000, "shares", "CONSOLIDATED")
    stored(session, priced, "BALANCE.owners_equity", 6_000_000_000, "inr", "CONSOLIDATED")
    run(session, priced)
    cap = metric(session, priced, "VALUATION.market_cap")
    note = next(n for n in cap.details["notes"] if "shares outstanding as at" in n)
    assert "2026-03-31" in note and "share-count basis: CONSOLIDATED" in note


def test_a_shareholding_pattern_count_states_no_basis_and_says_so(session, priced):
    _record(
        session, priced, "SHAREHOLDING.total.shares", 10_000_000, "shares",
        end=date(2026, 9, 30), ptype=None, basis=None, kind="SHAREHOLDING",
    )  # fmt: skip
    stored(session, priced, "BALANCE.owners_equity", 6_000_000_000, "inr", "CONSOLIDATED")
    run(session, priced)
    cap = metric(session, priced, "VALUATION.market_cap")
    assert any("UNSTATED (shareholding pattern)" in n for n in cap.details["notes"])
    pb = metric(session, priced, "VALUATION.price_to_book")
    assert pb is not None and pb.basis == "CONSOLIDATED"  # labelled with the equity's basis


def test_no_equity_at_all_is_still_a_plain_missing_input(session, priced):
    stored(session, priced, "BALANCE.shares_outstanding", 10_000_000, "shares", "CONSOLIDATED")
    report = run(session, priced)
    gap = next(g for g in report.gaps if g.metric == "VALUATION.price_to_book")
    assert "no CONSOLIDATED owners' equity" in gap.reason
