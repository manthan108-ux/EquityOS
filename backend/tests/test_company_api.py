import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import get_session
from app.main import create_app
from app.models import Company
from app.seeds.loader import seed_reference_data, seed_test_universe


@pytest.fixture
def client(session):
    seed_reference_data(session)
    seed_test_universe(session)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_search_resolves_ticker_with_both_listings(client):
    body = client.get("/api/companies/search", params={"q": "TCS"}).json()
    assert body["status"] == "RESOLVED"
    match = body["matches"][0]
    assert match["match_type"] == "TICKER"
    company = match["company"]
    assert company["legal_name"] == "Tata Consultancy Services Limited"
    assert company["sector"] == "IT Services"
    assert [(x["exchange"], x["symbol"]) for x in company["listings"]] == [
        ("BSE", "TCS"),
        ("NSE", "TCS"),
    ]
    assert (
        next(x for x in company["listings"] if x["exchange"] == "BSE")["security_code"] == "532540"
    )


def test_search_typo_returns_suggestions(client):
    body = client.get("/api/companies/search", params={"q": "relience"}).json()
    assert body["status"] == "SUGGESTIONS"
    assert body["matches"][0]["company"]["common_name"] == "Reliance Industries"
    assert body["matches"][0]["match_type"] == "FUZZY"


def test_search_not_found_returns_200_with_empty_matches(client):
    res = client.get("/api/companies/search", params={"q": "zzzzqqq"})
    assert res.status_code == 200
    assert res.json()["status"] == "NOT_FOUND"
    assert res.json()["matches"] == []


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "a" * 101}])
def test_search_validates_input(client, params):
    assert client.get("/api/companies/search", params=params).status_code == 422


def test_search_is_safe_against_sql_injection_strings(client, session):
    before = session.scalar(select(func.count()).select_from(Company))
    res = client.get("/api/companies/search", params={"q": "'; DROP TABLE companies; --"})
    assert res.status_code == 200
    assert session.scalar(select(func.count()).select_from(Company)) == before


def test_profile_lists_unverified_fields_instead_of_inventing_values(client):
    search = client.get("/api/companies/search", params={"q": "RELIANCE"}).json()
    company_id = search["matches"][0]["company"]["id"]
    profile = client.get(f"/api/companies/{company_id}").json()
    assert profile["common_name"] == "Reliance Industries"
    assert profile["founding_year"] is None
    assert "founding_year" in profile["unverified_fields"]
    assert "investor_relations_url" in profile["unverified_fields"]
    assert profile["website"] == "https://www.ril.com"
    assert "pending primary NSE/BSE verification" in profile["master_data_source"]
    assert {"alias": "RIL", "alias_type": "ABBREVIATION"} in profile["aliases"]


def test_profile_unknown_company_is_404(client):
    assert client.get(f"/api/companies/{uuid.uuid4()}").status_code == 404


def test_profile_malformed_id_is_422(client):
    assert client.get("/api/companies/not-a-uuid").status_code == 422
