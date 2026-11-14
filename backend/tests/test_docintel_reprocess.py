# ruff: noqa: F811
"""Reprocessing must never leave a document's stored evidence worse than it found it."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.docintel import reprocess as rp
from app.docintel.pipeline import PROVIDER_CODE
from app.models import FinancialFact
from app.providers.documents import store_document_bytes
from tests.test_calc_db import co  # noqa: F401
from tests.test_research_vault import make_pdf

pymupdf = pytest.importorskip("pymupdf")
D = Decimal


@pytest.fixture
def doc(session, co, tmp_path):  # noqa: F811
    body = make_pdf([(200, 25, "Reliance Industries Limited"), (60, 60, "results")])
    document, _ = store_document_bytes(
        session, tmp_path, body, url="https://x/r.pdf", doc_type="quarterly_result",
        source_code="COMPANY_IR", company_id=co.id,
    )  # fmt: skip
    return document


def fact(session, doc, item, value, end=date(2026, 9, 30)):
    session.add(
        FinancialFact(
            company_id=doc.company_id, provider_code=PROVIDER_CODE, statement="INCOME",
            line_item=item, period_end=end, period_type="QUARTERLY", basis="CONSOLIDATED",
            period_label="Q2", original_value=value, original_unit="crore", value_inr=value,
            source_url="x", retrieved_at=datetime.now(UTC), document_id=doc.id,
        )
    )  # fmt: skip
    session.flush()


def fake_extraction(monkeypatch, effect, status="COMPLETED"):
    def fake(session, document, data_dir, **_):
        effect(session, document)
        run = SimpleNamespace(status=SimpleNamespace(value=status), error="boom")
        return SimpleNamespace(extraction=run)

    monkeypatch.setattr(rp, "extract_document", fake)


def values(session, doc):
    rows = session.execute(
        select(FinancialFact.line_item, FinancialFact.value_inr).where(
            FinancialFact.document_id == doc.id
        )
    )
    return {item: value for item, value in rows}


def test_a_read_that_only_adds_facts_is_kept(session, doc, tmp_path, monkeypatch):
    fact(session, doc, "revenue", D(100))
    fake_extraction(monkeypatch, lambda s, d: fact(s, d, "other_income", D(5)))
    result = rp.reprocess_document(session, doc, tmp_path)
    assert result.status == rp.IMPROVED and len(result.gained) == 1
    assert values(session, doc) == {"revenue": D(100), "other_income": D(5)}


def test_a_read_that_loses_a_fact_is_rolled_back_to_the_previous_state(
    session, doc, tmp_path, monkeypatch
):
    fact(session, doc, "revenue", D(100))
    fact(session, doc, "other_income", D(5))

    def drop(s, d):  # what a re-run does: replace the document's promotions, here with fewer
        s.execute(delete(FinancialFact).where(FinancialFact.document_id == d.id))
        fact(s, d, "revenue", D(100))

    fake_extraction(monkeypatch, drop)
    result = rp.reprocess_document(session, doc, tmp_path)
    assert result.status == rp.KEPT_PREVIOUS and len(result.lost) == 1
    assert values(session, doc) == {"revenue": D(100), "other_income": D(5)}


def test_a_read_that_changes_a_value_is_rolled_back(session, doc, tmp_path, monkeypatch):
    fact(session, doc, "revenue", D(100))

    def change(s, d):
        s.execute(delete(FinancialFact).where(FinancialFact.document_id == d.id))
        fact(s, d, "revenue", D(101))

    fake_extraction(monkeypatch, change)
    result = rp.reprocess_document(session, doc, tmp_path)
    assert result.status == rp.KEPT_PREVIOUS and len(result.changed) == 1
    assert values(session, doc) == {"revenue": D(100)}


def test_a_failed_or_crashing_read_changes_nothing(session, doc, tmp_path, monkeypatch):
    fact(session, doc, "revenue", D(100))

    def wipe(s, d):
        s.execute(delete(FinancialFact).where(FinancialFact.document_id == d.id))

    fake_extraction(monkeypatch, wipe, status="FAILED")
    assert rp.reprocess_document(session, doc, tmp_path).status == rp.FAILED
    assert values(session, doc) == {"revenue": D(100)}

    def crash(s, d):
        wipe(s, d)
        raise RuntimeError("boom")

    fake_extraction(monkeypatch, crash)
    assert rp.reprocess_document(session, doc, tmp_path).status == rp.FAILED
    assert values(session, doc) == {"revenue": D(100)}


def test_an_identical_read_is_reported_unchanged(session, doc, tmp_path, monkeypatch):
    fact(session, doc, "revenue", D(100))

    def same(s, d):
        s.execute(delete(FinancialFact).where(FinancialFact.document_id == d.id))
        fact(s, d, "revenue", D(100))

    fake_extraction(monkeypatch, same)
    assert rp.reprocess_document(session, doc, tmp_path).status == rp.UNCHANGED


# ---- a fact withheld for a documented conflict is not a silent loss -------------------------------------


def withheld_extraction(monkeypatch, session, doc, reason, *, keep_row=True):
    """A re-read that drops `other_income`, optionally still reading it but withholding it."""
    from app.models import (
        DocumentExtraction,
        ExtractedFact,
        ExtractedStatement,
        ExtractionStatus,
    )

    run = DocumentExtraction(
        document_id=doc.id,
        extractor_version="t",
        status=ExtractionStatus.COMPLETED,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    stmt = ExtractedStatement(
        extraction_id=run.id, page_number=1, statement="INCOME", basis="CONSOLIDATED",
        unit="crore", detection="t", columns=[], rows_matched=1, rows_unmatched=0,
    )  # fmt: skip
    session.add(stmt)
    session.flush()
    if keep_row:
        session.add(
            ExtractedFact(
                extraction_id=run.id, statement_id=stmt.id, document_id=doc.id,
                company_id=doc.company_id, statement="INCOME", basis="CONSOLIDATED",
                canonical_item="other_income", raw_label="Other income", page_number=1,
                row_index=1, column_index=1, column_header="x", period_end=date(2026, 9, 30),
                duration_months=3, raw_value="5", original_value=D(5), original_unit="crore",
                value_inr=D(5), validation_status="PASSED", promoted=False,
                not_promoted_reason=reason,
            )
        )  # fmt: skip
        session.flush()

    def fake(s, document, data_dir, **_):
        s.execute(delete(FinancialFact).where(FinancialFact.document_id == document.id))
        fact(s, document, "revenue", D(100))
        fact(s, document, "other_new_line", D(7))
        return SimpleNamespace(extraction=run)

    monkeypatch.setattr(rp, "extract_document", fake)


def test_a_fact_withheld_because_the_document_conflicts_with_itself_does_not_block_the_gains(
    session, doc, tmp_path, monkeypatch
):
    fact(session, doc, "revenue", D(100))
    fact(session, doc, "other_income", D(5))
    withheld_extraction(
        monkeypatch, session, doc, "conflicts with another table in the same document"
    )
    result = rp.reprocess_document(session, doc, tmp_path)
    assert result.status == rp.IMPROVED and len(result.withheld) == 1 and not result.lost
    assert values(session, doc) == {"revenue": D(100), "other_new_line": D(7)}
    assert "withheld" in result.note


def test_a_fact_lost_for_any_other_reason_still_rolls_the_read_back(
    session, doc, tmp_path, monkeypatch
):
    fact(session, doc, "revenue", D(100))
    fact(session, doc, "other_income", D(5))
    withheld_extraction(
        monkeypatch, session, doc, "an arithmetic check involving this value failed"
    )
    result = rp.reprocess_document(session, doc, tmp_path)
    assert result.status == rp.KEPT_PREVIOUS and len(result.lost) == 1 and not result.withheld
    assert values(session, doc) == {"revenue": D(100), "other_income": D(5)}


def test_a_fact_that_is_simply_not_read_any_more_still_rolls_the_read_back(
    session, doc, tmp_path, monkeypatch
):
    fact(session, doc, "revenue", D(100))
    fact(session, doc, "other_income", D(5))
    withheld_extraction(
        monkeypatch,
        session,
        doc,
        "conflicts with another table in the same document",
        keep_row=False,
    )
    result = rp.reprocess_document(session, doc, tmp_path)
    assert result.status == rp.KEPT_PREVIOUS and len(result.lost) == 1


def test_a_fact_removed_because_the_table_is_another_entitys_does_not_block_the_re_read(
    session, doc, tmp_path, monkeypatch
):
    """The stricter attribution check rejects a subsidiary's table: that is a correction."""
    fact(session, doc, "revenue", D(100))
    fact(session, doc, "other_income", D(5))
    withheld_extraction(
        monkeypatch,
        session,
        doc,
        "entity not confirmed: the page header names a longer entity (a subsidiary or group company)",
    )
    result = rp.reprocess_document(session, doc, tmp_path)
    assert result.status == rp.IMPROVED and len(result.withheld) == 1 and not result.lost
