"""Vehicle volumes, employee attrition and utilisation stated in prose (Phase 11)."""

from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.docintel.operating import clean, extract_page_operating
from app.models import Company, EvidenceRecord
from app.research import vault
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.test_research_vault import make_pdf

pymupdf = pytest.importorskip("pymupdf")


def read(text):
    out = {}
    for block in extract_page_operating(1, text):
        period = block.column(0).period
        for f in block.facts:
            out[(f.key, f.dimension or "", period.period_end, period.duration_months)] = (
                f.value,
                f.duplicate,
            )
    return out


# ---- vehicle volumes ------------------------------------------------------------------------------


def test_quarterly_total_sales_volume_takes_its_period_from_the_heading():
    text = (
        "Maruti Suzuki India Limited announced financial results for the period Q2 (July-September) "
        "FY 2026-27. Highlights: Q2 (July-September), FY 2026-27 The Company achieved its "
        "highest-ever quarterly total sales volume of 696,654 units, registering a growth of 26.5% "
        "over the same period previous year."
    )
    found = read(text)
    assert found == {("vehicle_volumes", "", date(2026, 9, 30), 3): (D("696654"), False)}


def test_a_monthly_release_names_the_month():
    text = "Total sales of 1,89,123 units in October 2026, a growth of 12% over October 2025."
    got = read(text)
    # two different months are named, so the period is not certain: nothing is read
    assert got == {}
    text = "The company reported total sales of 1,89,123 units in October 2026."
    assert read(text) == {
        ("vehicle_volumes", "month", date(2026, 10, 31), None): (D("189123"), False)
    }


def test_a_volume_with_no_stated_period_is_never_given_one():
    assert read("The Company achieved total sales volume of 696,654 units.") == {}


def test_only_total_volumes_are_read_not_segments_or_other_counts():
    assert read("Domestic sales were 5,00,000 units in October 2026.") == {}
    assert read("Passenger vehicles sold 4,00,000 units in Q2 FY27.") == {}
    assert read("The plant has an installed capacity of 1,500,000 units in Q2 FY27") == {}


def test_two_different_totals_for_one_period_are_flagged_not_chosen():
    text = (
        "In Q2 FY27 total sales volume of 100,000 units. Later: total sales volume of 120,000 "
        "units in Q2 FY27."
    )
    got = read(text)
    assert list(got.values())[0][1] is True  # marked duplicate: the pipeline refuses it


# ---- attrition ------------------------------------------------------------------------------------


def test_voluntary_attrition_for_a_financial_year_is_read_across_pdf_spacing_artefacts():
    text = (
        "helped the Company to maintain a high talent retention rate, with voluntary IT services’ "
        "attrition at 13. 7 % for FY 2026. At TCS, three months’ notice is required."
    )
    assert read(text) == {("attrition", "voluntary", date(2026, 3, 31), 12): (D("13.7"), False)}


def test_ltm_attrition_for_a_quarter_keeps_its_own_dimension():
    text = "In Q2 FY27, IT services attrition (LTM) was 12.1%."
    assert read(text) == {("attrition", "ltm", date(2026, 9, 30), 3): (D("12.1"), False)}


def test_actuarial_attrition_assumptions_are_not_employee_attrition():
    text = (
        "Attrition rate (For Gratuity) was 3.0% in FY 2026. The discount rate for pension and "
        "attrition rate of 0.0% were assumed for FY 2026."
    )
    assert read(text) == {}


def test_attrition_without_a_period_or_with_two_periods_is_not_read():
    assert read("Voluntary attrition at 13.7%.") == {}
    assert read("Attrition at 13.7% in FY 2026 versus 14.1% in FY 2025.") == {}


# ---- utilisation ----------------------------------------------------------------------------------


def test_utilisation_variants_are_kept_apart():
    text = (
        "Utilization (excluding trainees) was 85.2% in Q2 FY27. "
        "Utilization (including trainees) was 83.9% in Q2 FY27."
    )
    got = read(text)
    q2 = date(2026, 9, 30)
    assert got[("utilization", "excluding_trainees", q2, 3)][0] == D("85.2")
    assert got[("utilization", "including_trainees", q2, 3)][0] == D("83.9")


def test_other_utilisation_is_refused():
    for sentence in (
        "Capacity utilisation was 85% in Q2 FY27.",
        "Utilisation of funds was 60% for FY 2026.",
        "Utilization of reserves stood at 40% in FY 2026.",
        "Asset utilization is at 70% in FY 2026.",
    ):
        assert read(sentence) == {}, sentence


def test_percentages_must_be_percentages():
    assert read("Attrition at 140% for FY 2026.") == {}


def test_text_cleaning_removes_extraction_artefacts():
    assert clean("13. 7 %  and\n 5 %") == "13.7% and 5%"


# ---- end to end through the upload pipeline -------------------------------------------------------


@pytest.fixture
def reliance(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


def test_a_stated_figure_becomes_page_cited_evidence(session, reliance, tmp_path):
    (tmp_path / "documents").mkdir()
    body = make_pdf(
        [
            (60, 40, "Reliance Industries Limited"),
            (60, 60, "Press release for Q2 FY27"),
            (60, 80, "The Company reported total sales volume of 696,654 units in Q2 FY27."),
            (60, 100, "Voluntary attrition was 13.7% in Q2 FY27."),
        ]
    )
    entry = vault.receive_upload(
        session, reliance, owner="alice", filename="release.pdf", body=body,
        licence=vault.PUBLIC_FILING, data_dir=tmp_path,
    )  # fmt: skip
    entry = vault.process_entry(session, entry.id, data_dir=tmp_path)
    assert entry.status == vault.ACCEPTED, (entry.reject_reason, entry.detected)
    rows = session.scalars(
        select(EvidenceRecord).where(EvidenceRecord.metric.like("OPERATING.%"))
    ).all()
    by = {r.metric: r for r in rows}
    assert by["OPERATING.vehicle_volumes"].value == D("696654")
    assert by["OPERATING.vehicle_volumes"].unit == "units"
    assert (
        by["OPERATING.attrition"].value == D("13.7") and by["OPERATING.attrition"].unit == "percent"
    )
    assert by["OPERATING.attrition"].dimension == "voluntary"
    assert all(r.document_id and r.source_page for r in rows)
