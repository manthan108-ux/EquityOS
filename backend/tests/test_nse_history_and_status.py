import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_session
from app.main import create_app
from app.models import AcquisitionLog, Company, CompanyAlias, FetchStatus, Source
from app.providers.nse_history import (
    apply_proposals,
    build_proposals,
    parse_name_changes,
    parse_symbol_changes,
)
from app.seeds.loader import seed_reference_data, seed_test_universe
from app.services.company_resolver import ResolutionStatus, resolve_company

NAME_CSV = """SYMBOL,OLD NAME OF THE COMPANY,NEW NAME OF THE COMPANY,EFFECTIVE DATE
INFY,Infosys Technologies Ltd (older spelling),Infosys Limited,22-JUN-2011
TCS,Tata Consultancy Services (India) Limited,Tata Consultancy Services Limited,01-JAN-2010
OLDTCSSYM,Pre-Rename TCS Name Limited,Tata Consultancy Services Limited,01-JAN-2005
HDFCBANK,ICICI Bank Limited,HDFC Bank Limited,01-JAN-2000
SOMEFUND1,Some Mutual Fund Series 1,Some Mutual Fund Series 1B,01-JAN-2016
RELIANCE,Re,Reliance Industries Limited,01-JAN-2001
"""
SYMBOL_CSV = """NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE
 Tata Consultancy Services Limited,OLDTCSSYM,MIDTCSSYM,01-JAN-2006
 Tata Consultancy Services Limited,MIDTCSSYM,TCS,01-JAN-2007
 Infosys Limited,INFYOLD,INFY,22-JUN-2011
 Reliance Industries Limited,TCS,RELIANCE,01-JAN-2012
 Some Fund,FUNDOLD,FUNDNEW,01-JAN-2016
"""


@pytest.fixture
def seeded(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session


def test_parsers_read_the_real_nse_layouts():
    names = parse_name_changes(NAME_CSV)
    assert names[0].symbol == "INFY" and names[0].effective.isoformat() == "2011-06-22"
    symbols = parse_symbol_changes(SYMBOL_CSV)
    assert (symbols[0].old_symbol, symbols[0].new_symbol) == ("OLDTCSSYM", "MIDTCSSYM")


def proposals_by_alias(report):
    return {p.alias: p for p in report.proposals}


def test_proposals_add_former_names_and_symbols_with_provenance(seeded):
    report = build_proposals(seeded, parse_name_changes(NAME_CSV), parse_symbol_changes(SYMBOL_CSV))
    by_alias = proposals_by_alias(report)
    assert by_alias["Infosys Technologies Ltd (older spelling)"].company_name == "Infosys"
    assert by_alias["INFYOLD"].kind == "former symbol"
    assert by_alias["INFYOLD"].source == "NSE symbolchange.csv (2011-06-22)"
    assert (
        "NSE namechange.csv (2011-06-22)"
        in by_alias["Infosys Technologies Ltd (older spelling)"].source
    )


def test_chained_symbol_changes_resolve_to_the_current_company(seeded):
    report = build_proposals(seeded, parse_name_changes(NAME_CSV), parse_symbol_changes(SYMBOL_CSV))
    by_alias = proposals_by_alias(report)
    # OLDTCSSYM -> MIDTCSSYM -> TCS
    assert by_alias["Pre-Rename TCS Name Limited"].company_name == "TCS"
    assert by_alias["OLDTCSSYM"].company_name == "TCS"
    assert by_alias["MIDTCSSYM"].company_name == "TCS"


def test_names_that_belong_to_another_company_are_never_taken(seeded):
    report = build_proposals(seeded, parse_name_changes(NAME_CSV), parse_symbol_changes(SYMBOL_CSV))
    aliases = proposals_by_alias(report)
    # "ICICI Bank Limited" is ICICI Bank's own name: HDFC Bank must not claim it as a former name.
    assert "ICICI Bank Limited" not in aliases
    # "TCS" is a live ticker of a different company than the one the row maps to.
    assert any("ICICI Bank Limited" in c for c in report.skipped_conflicts)
    assert "TCS" not in aliases


def test_too_short_names_and_unknown_securities_are_ignored(seeded):
    report = build_proposals(seeded, parse_name_changes(NAME_CSV), parse_symbol_changes(SYMBOL_CSV))
    aliases = proposals_by_alias(report)
    assert "Re" not in aliases  # too short to be a safe alias
    assert "Some Mutual Fund Series 1" not in aliases
    assert report.unmatched_rows >= 2  # the fund rows


def test_applying_makes_former_names_searchable_and_is_repeatable(seeded):
    report = build_proposals(seeded, parse_name_changes(NAME_CSV), parse_symbol_changes(SYMBOL_CSV))
    before = len(report.proposals)
    assert apply_proposals(seeded, report.proposals) == before
    row = seeded.scalar(select(CompanyAlias).where(CompanyAlias.alias == "INFYOLD"))
    assert row.alias_type.value == "FORMER_NAME" and row.source.startswith("NSE symbolchange.csv")
    from app.services.search_index import invalidate_search_index

    invalidate_search_index()
    assert resolve_company(seeded, "infyold").status is ResolutionStatus.RESOLVED
    again = build_proposals(seeded, parse_name_changes(NAME_CSV), parse_symbol_changes(SYMBOL_CSV))
    assert again.proposals == []  # nothing new the second time
    assert seeded.scalar(select(Company).where(Company.common_name == "Infosys"))


# --- provider status API -----------------------------------------------------------------------


def test_provider_status_reports_successes_and_failures(session):
    seed_reference_data(session)
    session.add_all(
        [
            AcquisitionLog(source_code="NSE_ARCHIVES", url="u1", status=FetchStatus.OK),
            AcquisitionLog(source_code="NSE_ARCHIVES", url="u2", status=FetchStatus.CACHED),
            AcquisitionLog(
                source_code="NSE_ARCHIVES",
                url="u3",
                status=FetchStatus.ACCESS_DENIED,
                error="HTTP 403",
            ),
        ]
    )
    session.flush()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    body = TestClient(app).get("/api/providers/status").json()
    nse = next(s for s in body if s["code"] == "NSE_ARCHIVES")
    assert nse["fetches"] == 3 and nse["successes"] == 2
    assert nse["failures_by_status"] == {"ACCESS_DENIED": 1}
    assert "ACCESS_DENIED" in nse["last_failure"] and nse["last_success_at"]
    assert nse["authority_tier"] == 1
    quiet = next(s for s in body if s["code"] == "SEBI_RSS")
    assert quiet["fetches"] == 0 and quiet["last_failure"] is None
    assert session.scalar(select(Source).where(Source.code == "SEBI_RSS"))
