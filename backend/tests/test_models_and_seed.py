import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import AliasType, Base, Company, CompanyAlias, Exchange, Security
from app.models.base import include_object
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.conftest import alembic_config


def _seed(session: Session) -> None:
    seed_reference_data(session)
    seed_test_universe(session)


def _count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --- seeding ---------------------------------------------------------------------------------


def test_seed_creates_test_universe(session):
    _seed(session)
    assert _count(session, Company) == 5
    assert _count(session, Security) == 10  # every company is listed on both NSE and BSE
    assert {e.code for e in session.scalars(select(Exchange))} == {"NSE", "BSE"}


def test_seed_is_idempotent(session):
    _seed(session)
    _seed(session)
    assert _count(session, Company) == 5
    assert _count(session, Security) == 10
    assert _count(session, CompanyAlias) == 16


def test_dual_listing_is_one_company_two_securities(session):
    _seed(session)
    tcs = session.scalar(select(Company).where(Company.common_name == "TCS"))
    assert {s.exchange.code for s in tcs.securities} == {"NSE", "BSE"}
    assert {s.isin for s in tcs.securities} == {"INE467B01029"}  # same ISIN on both listings
    bse = next(s for s in tcs.securities if s.exchange.code == "BSE")
    assert bse.security_code == "532540"


def test_seed_marks_provenance_and_leaves_unverified_fields_null(session):
    _seed(session)
    reliance = session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))
    assert "pending primary NSE/BSE verification" in reliance.master_data_source
    assert reliance.founding_year is None
    assert reliance.investor_relations_url is None


def test_aliases_are_normalised(session):
    _seed(session)
    row = session.scalar(select(CompanyAlias).where(CompanyAlias.alias == "RIL"))
    assert row.normalized_alias == "ril"
    assert row.alias_type is AliasType.ABBREVIATION


# --- constraints -----------------------------------------------------------------------------


def _first_company(session: Session) -> Company:
    _seed(session)
    return session.scalar(select(Company).where(Company.common_name == "Infosys"))


def _nse(session: Session) -> Exchange:
    return session.scalar(select(Exchange).where(Exchange.code == "NSE"))


def test_rejects_malformed_isin(session):
    company = _first_company(session)
    session.add(
        Security(company_id=company.id, exchange_id=_nse(session).id, symbol="X1", isin="BAD")
    )
    with pytest.raises(IntegrityError, match="isin_format"):
        session.flush()


def test_rejects_malformed_bse_code(session):
    company = _first_company(session)
    bse = session.scalar(select(Exchange).where(Exchange.code == "BSE"))
    session.add(
        Security(
            company_id=company.id,
            exchange_id=bse.id,
            symbol="X2",
            security_code="12AB",
            isin="INE009A01021",
        )
    )
    with pytest.raises(IntegrityError, match="bse_code_format"):
        session.flush()


def test_rejects_duplicate_symbol_on_same_exchange(session):
    company = _first_company(session)
    session.add(
        Security(
            company_id=company.id, exchange_id=_nse(session).id, symbol="INFY", isin="INE009A01021"
        )
    )
    with pytest.raises(IntegrityError, match="uq_securities_exchange_id_symbol"):
        session.flush()


def test_two_issuers_may_share_a_legal_name(session):
    # Names are not identities: different issuers can legitimately share one.
    for key in ("INE111A", "INE222B"):
        session.add(
            Company(
                issuer_key=key,
                legal_name="Same Name Limited",
                common_name="Same Name",
                master_data_source="t",
            )
        )
    session.flush()


def test_rejects_duplicate_issuer_key(session):
    _seed(session)
    session.add(
        Company(
            issuer_key="INE009A", legal_name="Other", common_name="Other", master_data_source="t"
        )
    )
    with pytest.raises(IntegrityError, match="uq_companies_issuer_key"):
        session.flush()


def test_rejects_implausible_founding_year(session):
    session.add(
        Company(
            legal_name="Time Traveller Ltd",
            common_name="TT",
            founding_year=3000,
            master_data_source="t",
        )
    )
    with pytest.raises(IntegrityError, match="founding_year_range"):
        session.flush()


def test_rejects_unknown_alias_type_string(session):
    company = _first_company(session)
    with pytest.raises(StatementError, match="not among the defined enum values"):
        session.add(
            CompanyAlias(company_id=company.id, alias="x", normalized_alias="x", alias_type="NOPE")
        )
        session.flush()


def test_deleting_company_cascades_to_securities_and_aliases(session):
    company = _first_company(session)
    securities_before, aliases_before = _count(session, Security), _count(session, CompanyAlias)
    n_sec, n_alias = len(company.securities), len(company.aliases)
    session.delete(company)
    session.flush()
    assert _count(session, Security) == securities_before - n_sec
    assert _count(session, CompanyAlias) == aliases_before - n_alias


# --- migrations ------------------------------------------------------------------------------


def test_models_match_migrated_schema(db_engine):
    """No drift: Alembic autogenerate against the migrated DB must find nothing to change."""
    with db_engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "include_object": include_object}
        )
        assert compare_metadata(ctx, Base.metadata) == []


def test_migration_downgrade_then_upgrade(test_db_url):
    cfg = alembic_config(test_db_url)
    command.downgrade(cfg, "base")
    engine = create_engine(test_db_url)
    try:
        with engine.connect() as conn:
            tables = {
                r[0]
                for r in conn.exec_driver_sql(
                    "select table_name from information_schema.tables where table_schema='public'"
                )
            }
        assert tables == {"alembic_version"}
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")
