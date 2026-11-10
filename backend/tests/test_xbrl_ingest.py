# ruff: noqa: F811
"""Offline XBRL ingestion into the document / extraction / financial-fact pipeline."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.docintel import xbrl
from app.docintel.xbrl_ingest import EntityMismatch, ingest
from app.models import (
    Document,
    ExtractedFact,
    FinancialFact,
    Security,
    XbrlFact,
    XbrlFiling,
)
from tests.test_calc_db import co  # noqa: F401
from tests.test_sector import seeded  # noqa: F401
from tests.xbrl_fixtures import amount, bank_filing, indas_filing, indas_income

D = Decimal
SEP26 = date(2026, 9, 30)


def facts(session, company, basis=None):
    q = select(FinancialFact).where(
        FinancialFact.company_id == company.id, FinancialFact.provider_code == "DOCUMENT_EXTRACTION"
    )
    if basis:
        q = q.where(FinancialFact.basis == basis)
    return list(session.scalars(q))


def run(session, co, body, tmp_path, **kw):  # noqa: F811
    return ingest(session, co, body, tmp_path, filename="filing.xml", **kw)


def test_a_matching_filing_is_stored_raw_and_its_checked_values_are_promoted(session, co, tmp_path):  # noqa: F811
    rep = run(session, co, indas_filing(), tmp_path)
    assert rep.created and rep.document.doc_type == "xbrl"
    assert rep.document.association_status == "CONFIRMED"
    assert rep.filing.family == "IND_AS" and rep.filing.basis == "CONSOLIDATED"
    assert rep.filing.amount_unit == "lakhs" and rep.filing.entity_confirmed
    revenue = [f for f in facts(session, co) if f.line_item == "revenue_from_operations"]
    q2 = next(f for f in revenue if f.period_end == SEP26)
    assert q2.value_inr == D(1000) * 100_000 and q2.basis == "CONSOLIDATED"
    assert q2.period_type == "QUARTERLY" and q2.document_id == rep.document.id
    assert rep.promoted > 0


def test_every_fact_is_kept_raw_including_the_unmapped_ones(session, co, tmp_path):  # noqa: F811
    body = indas_filing(
        extra='<in-capmkt:SomethingNew contextRef="cur" unitRef="INR" decimals="2">9</in-capmkt:SomethingNew>'
    )
    rep = run(session, co, body, tmp_path)
    total = session.scalar(
        select(func.count()).select_from(XbrlFact).where(XbrlFact.filing_id == rep.filing.id)
    )
    assert total == rep.filing.facts_total == len(xbrl.parse(body).facts)
    odd = session.scalar(
        select(XbrlFact).where(
            XbrlFact.filing_id == rep.filing.id, XbrlFact.local_name == "SomethingNew"
        )
    )
    assert odd.mapped_item is None and odd.value_text == "9" and odd.unit_label == "INR"
    assert "in-capmkt:SomethingNew" in rep.filing.unmapped_concepts
    assert {c["id"] for c in rep.filing.contexts} == {"cur", "pri", "end"}


def test_the_comparative_period_is_extracted_alongside_the_current_one(session, co, tmp_path):  # noqa: F811
    run(session, co, indas_filing(), tmp_path)
    ends = {f.period_end for f in facts(session, co) if f.line_item == "revenue_from_operations"}
    assert ends == {SEP26, date(2025, 9, 30)}


def test_standalone_and_consolidated_filings_are_kept_independent(session, co, tmp_path):  # noqa: F811
    run(session, co, indas_filing(basis="Consolidated"), tmp_path)
    run(
        session,
        co,
        indas_filing(
            basis="Standalone",
            income=indas_income(
                "cur", revenue=600, other=50, total=650, expenses=400, pbt=250, tax=60, profit=190
            ),
        ),
        tmp_path,
    )
    cons = {
        f.line_item: f.value_inr
        for f in facts(session, co, "CONSOLIDATED")
        if f.period_end == SEP26
    }
    stand = {
        f.line_item: f.value_inr for f in facts(session, co, "STANDALONE") if f.period_end == SEP26
    }
    assert cons["revenue_from_operations"] == D(1000) * 100_000
    assert stand["revenue_from_operations"] == D(600) * 100_000


def test_a_filing_without_a_stated_basis_promotes_nothing(session, co, tmp_path):  # noqa: F811
    rep = run(session, co, indas_filing(basis=None), tmp_path)
    assert rep.promoted == 0 and facts(session, co) == []
    assert any("basis not stated" in k for k in rep.not_promoted)


def test_a_filing_without_a_unit_promotes_nothing_but_keeps_everything_raw(session, co, tmp_path):  # noqa: F811
    rep = run(session, co, indas_filing(rounding=None), tmp_path)
    assert facts(session, co) == [] and rep.filing.facts_total > 0
    assert any("unit not stated" in k for k in rep.not_promoted)


def test_a_wrong_total_fails_its_check_and_blocks_those_values(session, co, tmp_path):  # noqa: F811
    bad = indas_income("cur", total=9999)  # total income != revenue + other income
    rep = run(session, co, indas_filing(income=bad), tmp_path)
    assert any(i["code"] == "CHECK_FAILED" for i in rep.issues)
    cur = [f for f in facts(session, co) if f.period_end == SEP26 and f.line_item == "total_income"]
    assert cur == []  # the failed identity is not promoted


# ---- who filed it -------------------------------------------------------------------------------


def test_a_filing_of_another_listed_company_is_refused_and_nothing_is_stored(session, co, tmp_path):  # noqa: F811
    other = session.scalar(select(Security).where(Security.company_id != co.id))
    before = session.scalar(select(func.count()).select_from(Document))
    with pytest.raises(EntityMismatch, match="another company"):
        run(session, co, indas_filing(ident=other.symbol), tmp_path)
    assert session.scalar(select(func.count()).select_from(Document)) == before


def test_an_unknown_identifier_leaves_the_filing_unverified_and_unpromoted(session, co, tmp_path):  # noqa: F811
    rep = run(session, co, indas_filing(ident="ZZZZZZ"), tmp_path)
    assert rep.document.association_status == "UNVERIFIED" and not rep.filing.entity_confirmed
    assert rep.promoted == 0
    assert any(i["code"] == "ENTITY_UNCONFIRMED" for i in rep.issues)


def test_the_supplier_can_confirm_the_issuer_and_the_event_is_recorded_as_forced(
    session, co, tmp_path
):  # noqa: F811
    rep = run(session, co, indas_filing(ident="ZZZZZZ"), tmp_path, force_entity=True)
    assert rep.document.association_status == "FORCED" and rep.promoted > 0


# ---- scale guard and revisions -----------------------------------------------------------------


def test_a_value_a_power_of_ten_away_from_a_stored_one_is_a_scale_error(session, co, tmp_path):  # noqa: F811
    session.add(
        FinancialFact(
            company_id=co.id, provider_code="DOCUMENT_EXTRACTION", statement="INCOME",
            line_item="revenue_from_operations", period_end=SEP26, period_type="QUARTERLY",
            basis="CONSOLIDATED", period_label="Q2", original_value=D(1000),
            original_unit="lakhs", value_inr=D(1000) * 10_000_000,  # stored as crores
            source_url="x", retrieved_at=session.scalar(select(func.now())),
        )
    )  # fmt: skip
    session.flush()
    rep = run(session, co, indas_filing(), tmp_path)
    assert any("scale error" in k for k in rep.not_promoted)
    row = session.scalar(
        select(ExtractedFact).where(
            ExtractedFact.document_id == rep.document.id,
            ExtractedFact.canonical_item == "revenue_from_operations",
            ExtractedFact.period_end == SEP26,
        )
    )
    assert not row.promoted and "scale error" in row.not_promoted_reason


def test_ingesting_the_same_file_twice_stores_it_once_and_supersedes_the_first_run(
    session, co, tmp_path
):  # noqa: F811
    first = run(session, co, indas_filing(), tmp_path)
    second = run(session, co, indas_filing(), tmp_path)
    assert first.document.id == second.document.id and not second.created
    session.refresh(first.extraction)
    assert first.extraction.superseded_at is not None and second.extraction.superseded_at is None
    n = len(
        [
            f
            for f in facts(session, co)
            if f.line_item == "revenue_from_operations" and f.period_end == SEP26
        ]
    )
    assert n == 1  # no duplicate financial fact
    assert session.scalar(select(func.count()).select_from(XbrlFiling)) == 2  # both reads kept


def test_a_revised_filing_replaces_the_value_only_through_the_normal_conflict_rules(
    session, co, tmp_path
):  # noqa: F811
    run(session, co, indas_filing(), tmp_path)
    revised = indas_filing(
        income=indas_income("cur", revenue=1010, other=50, total=1060, expenses=810)
    )
    rep = run(session, co, revised, tmp_path)
    assert rep.document.sha256 != session.scalar(
        select(Document.sha256).where(Document.id != rep.document.id).limit(1)
    )
    assert (
        session.scalar(
            select(func.count()).select_from(Document).where(Document.doc_type == "xbrl")
        )
        == 2
    )
    # both documents remain; the earlier document's raw facts are untouched
    assert session.scalar(select(func.count()).select_from(XbrlFiling)) == 2


# ---- banking taxonomy --------------------------------------------------------------------------


def test_a_banking_filing_maps_through_the_banking_table_only(session, co, tmp_path):  # noqa: F811
    rep = run(session, co, bank_filing(), tmp_path)
    assert rep.filing.family == "BANK"
    items = {f.line_item for f in facts(session, co)}
    assert {"interest_earned", "interest_expended", "advances", "deposits"} & items
    assert "revenue_from_operations" not in items


def test_bank_ratios_are_stored_with_the_filings_basis_and_percent_unit(session, co, tmp_path):  # noqa: F811
    rep = run(session, co, bank_filing(), tmp_path)
    row = session.scalar(
        select(ExtractedFact).where(
            ExtractedFact.document_id == rep.document.id,
            ExtractedFact.canonical_item == "gross_npa_ratio",
        )
    )
    assert row.original_value == D("1.16") and row.original_unit == "percent"
    assert row.basis == "STANDALONE" and row.period_end == SEP26


def test_a_pure_ratio_is_not_promoted_without_confirmation(session, co, tmp_path):  # noqa: F811
    rep = run(session, co, bank_filing(ratios="pure"), tmp_path)
    ratio_rows = session.scalars(
        select(ExtractedFact).where(
            ExtractedFact.document_id == rep.document.id,
            ExtractedFact.canonical_item.in_(("gross_npa_ratio", "net_npa_ratio")),
        )
    ).all()
    assert ratio_rows and not any(r.promoted for r in ratio_rows)


def test_files_that_are_not_xbrl_store_nothing(session, co, tmp_path):  # noqa: F811
    before = session.scalar(select(func.count()).select_from(Document))
    with pytest.raises(xbrl.XbrlError):
        run(session, co, b"<html><body/></html>", tmp_path)
    assert session.scalar(select(func.count()).select_from(Document)) == before


def test_an_extra_amount_fact_does_not_break_ingestion(session, co, tmp_path):  # noqa: F811
    rep = run(session, co, indas_filing(extra=amount("OtherExpenses", "cur", 5)), tmp_path)
    assert rep.extraction.status.value == "COMPLETED"
