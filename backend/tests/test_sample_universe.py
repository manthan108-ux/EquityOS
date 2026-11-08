"""The 15-company research sample: definition and the additive, audited loader."""

from collections import Counter

import pytest
from sqlalchemy import select

from app.models import Company, Exchange, Industry, MasterDataImport, Sector, Security
from app.seeds import sample_loader as loader
from app.seeds.loader import seed_reference_data, seed_test_universe
from app.seeds.reference_data import INDUSTRIES, SECTORS
from app.seeds.sample_universe import BY_SYMBOL, NEW_INDUSTRIES, SAMPLE


def test_the_sample_is_fifteen_companies_with_real_peer_groups():
    assert len(SAMPLE) == 15 and len(BY_SYMBOL) == 15
    cohorts = Counter(c.cohort for c in SAMPLE)
    assert cohorts["banks"] == 5 and cohorts["it_services"] == 5
    assert cohorts["fmcg"] == 2 and cohorts["automobiles"] == 2 and cohorts["energy"] == 1


def test_every_sector_and_industry_exists_in_the_reference_data():
    known = {**INDUSTRIES, **NEW_INDUSTRIES}
    for c in SAMPLE:
        assert c.sector in SECTORS, c.symbol
        assert (c.sector, c.industry) in known, c.symbol
        assert c.documents in {"AUTOMATIC", "MANUAL", "STORED"}
        assert c.website.startswith("https://") and "/" not in c.website[8:]


def test_manual_companies_are_the_ones_whose_sites_refuse_us():
    manual = {c.symbol for c in SAMPLE if c.documents == "MANUAL"}
    assert manual == {
        "HDFCBANK",
        "AXISBANK",
        "SBIN",
        "TCS",
        "INFY",
        "HCLTECH",
        "HINDUNILVR",
        "MARUTI",
    }
    assert {c.symbol for c in SAMPLE if c.documents == "STORED"} == {"ICICIBANK", "RELIANCE"}


@pytest.fixture
def session_with_itc(session):
    seed_reference_data(session)
    seed_test_universe(session)
    company = Company(
        legal_name="ITC Limited", common_name="ITC", master_data_source="test", issuer_key="INE154A"
    )
    session.add(company)
    session.flush()
    nse = session.scalar(select(Exchange).where(Exchange.code == "NSE"))
    session.add(
        Security(company_id=company.id, exchange_id=nse.id, symbol="ITC", isin="INE154A01025")
    )
    session.flush()
    return session, company


def test_plan_changes_nothing_and_reports_missing_companies(session_with_itc):
    session, company = session_with_itc
    rows = {r.spec.symbol: r for r in loader.plan(session)}
    assert rows["ITC"].set_fields.keys() == {"sector", "industry", "website"}
    assert rows["SBIN"].company is None  # not in this test database
    assert rows["ICICIBANK"].company is not None and rows["ICICIBANK"].set_fields == {}  # seeded
    assert rows["RELIANCE"].set_fields == {} and rows["RELIANCE"].conflicts == []
    session.refresh(company)
    assert company.sector_id is None and company.website is None


def test_apply_fills_only_empty_fields_adds_industries_and_writes_an_audit_row(session_with_itc):
    session, company = session_with_itc
    changed, log = loader.apply(session)
    session.refresh(company)
    assert changed == 1 and any("ITC: set industry, sector, website" in x for x in log)
    assert company.website == "https://www.itcportal.com"
    assert session.get(Sector, company.sector_id).code == "fmcg"
    assert session.get(Industry, company.industry_id).code == "diversified_fmcg"
    industries = {i.code for i in session.scalars(select(Industry))}
    assert set(c for _, c in NEW_INDUSTRIES) <= industries
    audit = session.scalar(select(MasterDataImport).order_by(MasterDataImport.id.desc()))
    assert audit.filename == "app/seeds/sample_universe.py" and audit.rows_imported == 1
    assert audit.rows_total == 15 and "ITC" in audit.notes and len(audit.sha256) == 64
    # applying again changes nothing
    again, _ = loader.apply(session)
    assert again == 0


def test_a_value_already_set_is_never_overwritten(session_with_itc):
    session, company = session_with_itc
    company.website = "https://itc.example.invalid"
    session.flush()
    row = next(r for r in loader.plan(session) if r.spec.symbol == "ITC")
    assert "website" not in row.set_fields
    assert any("website is already" in c for c in row.conflicts)
    loader.apply(session)
    session.refresh(company)
    assert company.website == "https://itc.example.invalid"  # kept
    assert company.sector_id is not None  # the empty fields were still filled


def test_a_sector_set_to_something_else_is_a_reported_conflict_not_a_change(session_with_itc):
    session, company = session_with_itc
    other = session.scalar(select(Sector).where(Sector.code == "banking"))
    company.sector_id = other.id
    session.flush()
    row = next(r for r in loader.plan(session) if r.spec.symbol == "ITC")
    assert any("sector is already set" in c for c in row.conflicts)
    loader.apply(session)
    session.refresh(company)
    assert company.sector_id == other.id


def test_the_command_line_refuses_to_apply_without_confirmation():
    with pytest.raises(SystemExit, match="--yes"):
        loader.main(["apply"])


def test_pymupdf_is_imported_by_exactly_one_module():
    """Keeps the AGPL dependency swappable in one file (docs/LICENCES.md, decision 4)."""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    users = [
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if any(
            line.strip().startswith(("import pymupdf", "import fitz", "from pymupdf", "from fitz"))
            for line in p.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert users == ["docintel\pdf.py"] or users == ["docintel/pdf.py"]
