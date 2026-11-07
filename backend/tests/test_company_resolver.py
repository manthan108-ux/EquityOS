import pytest
from sqlalchemy import select

from app.core.text import normalize_text
from app.models import AliasType, Company, CompanyAlias
from app.seeds.loader import seed_reference_data, seed_test_universe
from app.services.company_resolver import MatchType, ResolutionStatus, resolve_company


@pytest.fixture
def seeded(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session


def _names(session, resolution):
    ids = [c.company_id for c in resolution.candidates]
    rows = session.scalars(select(Company).where(Company.id.in_(ids))).all()
    by_id = {c.id: c.common_name for c in rows}
    return [by_id[i] for i in ids]


# --- exact stages ----------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["INE002A01018", "ine002a01018", "  INE002A01018  "])
def test_resolves_by_isin(seeded, query):
    res = resolve_company(seeded, query)
    assert res.status is ResolutionStatus.RESOLVED
    assert res.match_type is MatchType.ISIN
    assert _names(seeded, res) == ["Reliance Industries"]


def test_isin_with_bad_check_digit_is_not_resolved_and_explained(seeded):
    res = resolve_company(seeded, "INE002A01019")
    assert res.status is ResolutionStatus.NOT_FOUND
    assert any("check digit" in n for n in res.notes)


def test_resolves_by_bse_code(seeded):
    res = resolve_company(seeded, "532540")
    assert res.status is ResolutionStatus.RESOLVED
    assert res.match_type is MatchType.BSE_CODE
    assert _names(seeded, res) == ["TCS"]


@pytest.mark.parametrize("query", ["TCS", "tcs", " Tcs "])
def test_ticker_on_two_exchanges_is_one_company_not_ambiguous(seeded, query):
    res = resolve_company(seeded, query)
    assert res.status is ResolutionStatus.RESOLVED
    assert res.match_type is MatchType.TICKER
    assert len(res.candidates) == 1  # NSE and BSE listings collapse to one company


@pytest.mark.parametrize(
    ("query", "expected", "match_type"),
    [
        ("Reliance Industries Limited", "Reliance Industries", MatchType.NAME),
        ("Reliance Industries Ltd.", "Reliance Industries", MatchType.NAME),
        # "reliance" equals the ticker RELIANCE, and the ticker stage runs before names.
        ("reliance", "Reliance Industries", MatchType.TICKER),
        ("HDFC Bank", "HDFC Bank", MatchType.NAME),
        ("hdfc  bank ", "HDFC Bank", MatchType.NAME),
        ("RIL", "Reliance Industries", MatchType.ALIAS),
        ("Infosys Technologies Limited", "Infosys", MatchType.ALIAS),
        ("Tata Consultancy Services", "TCS", MatchType.NAME),
    ],
)
def test_resolves_by_name_or_alias(seeded, query, expected, match_type):
    res = resolve_company(seeded, query)
    assert res.status is ResolutionStatus.RESOLVED, query
    assert res.match_type is match_type
    assert _names(seeded, res) == [expected]


def test_every_seeded_company_is_findable_by_legal_and_common_name(seeded):
    # Invariant the resolver relies on: legal and common names exist as aliases.
    for company in seeded.scalars(select(Company)):
        normalized = {a.normalized_alias for a in company.aliases}
        assert normalize_text(company.legal_name) in normalized, company.legal_name
        assert normalize_text(company.common_name) in normalized, company.common_name


# --- ambiguity -------------------------------------------------------------------------------


def test_shared_alias_across_companies_is_ambiguous(seeded):
    infosys = seeded.scalar(select(Company).where(Company.common_name == "Infosys"))
    tcs = seeded.scalar(select(Company).where(Company.common_name == "TCS"))
    for company in (infosys, tcs):
        seeded.add(
            CompanyAlias(
                company_id=company.id,
                alias="IT Major",
                normalized_alias=normalize_text("IT Major"),
                alias_type=AliasType.OTHER,
            )
        )
    seeded.flush()
    res = resolve_company(seeded, "it major")
    assert res.status is ResolutionStatus.AMBIGUOUS
    assert sorted(_names(seeded, res)) == ["Infosys", "TCS"]


# --- fuzzy suggestions -----------------------------------------------------------------------


def test_typo_gives_suggestions_not_a_silent_pick(seeded):
    res = resolve_company(seeded, "relience industries")
    assert res.status is ResolutionStatus.SUGGESTIONS  # user must confirm
    assert res.match_type is MatchType.FUZZY
    assert _names(seeded, res)[0] == "Reliance Industries"


def test_prefix_gives_suggestions(seeded):
    res = resolve_company(seeded, "reli")
    assert res.status is ResolutionStatus.SUGGESTIONS
    assert _names(seeded, res)[0] == "Reliance Industries"


def test_partial_name_suggests_matching_company(seeded):
    res = resolve_company(seeded, "hdfc")
    assert res.status is ResolutionStatus.SUGGESTIONS
    assert "HDFC Bank" in _names(seeded, res)


def test_suggestions_are_distinct_companies_and_capped(seeded):
    res = resolve_company(seeded, "bank")
    ids = [c.company_id for c in res.candidates]
    assert len(ids) == len(set(ids))
    assert len(ids) <= 5
    assert set(_names(seeded, res)) <= {"HDFC Bank", "ICICI Bank"}


# --- no match / bad input --------------------------------------------------------------------


@pytest.mark.parametrize("query", ["zzzzqqq", "999999", "INE000000000", "   ", "", "!!!"])
def test_unknown_or_empty_input_is_not_found(seeded, query):
    res = resolve_company(seeded, query)
    assert res.status is ResolutionStatus.NOT_FOUND
    assert res.candidates == []


def test_oversized_query_is_rejected(seeded):
    assert resolve_company(seeded, "a" * 101).status is ResolutionStatus.NOT_FOUND


def test_empty_database_is_not_found(session):
    assert resolve_company(session, "TCS").status is ResolutionStatus.NOT_FOUND
