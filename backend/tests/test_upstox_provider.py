"""Upstox adapter tests.

IMPORTANT: the response fixtures are reproduced from Upstox's DOCUMENTATION examples
(https://upstox.com/developer/api-documentation/). They prove our parsing, normalization and
storage logic. They do NOT prove that Upstox serves a dataset: that requires a recorded live
retrieval (see `dataset_verifications`), which needs a real token.
"""

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app.acquisition.http import ClientConfig, PoliteClient
from app.models import (
    Basis,
    Company,
    CorporateAction,
    FinancialFact,
    PeriodType,
    RatioSnapshot,
    RawPayload,
    ShareholdingFact,
    Statement,
)
from app.providers.base import CompanyRef, Dataset, Outcome
from app.providers.upstox import (
    UpstoxError,
    UpstoxFundamentalsProvider,
    parse_corporate_actions,
    parse_key_ratios,
    parse_shareholding,
    parse_statement,
)
from app.seeds.loader import seed_reference_data, seed_test_universe

TOKEN = "test-token-DO-NOT-LEAK-12345"

INCOME_YEARLY = {
    "status": "success",
    "data": {
        "type": "consolidated",
        "time_period": "yearly",
        "units_in": "crore",
        "income_statement": [
            {
                "category": "revenue",
                "history": [
                    {"value": 1086181, "period": "Mar 2026", "change": "+10.53%"},
                    {"value": 982671, "period": "Mar 2025", "change": "+7.15%"},
                    {"value": 917121, "period": "Mar 2024"},
                ],
            },
            {"category": "net_profit", "history": [{"value": -250.5, "period": "Mar 2026"}]},
        ],
        "full_statement": [
            {
                "particular": "EPS - Basic",
                "history": [
                    {"period": "Mar 2025", "value": 51.47},
                    {"period": "Mar 2024", "value": 51.45},
                ],
            },
            {"particular": "Profit After Tax", "history": [{"period": "Mar 2025", "value": 79020}]},
        ],
    },
}
BALANCE = {
    "status": "success",
    "data": {
        "type": "consolidated",
        "time_period": "yearly",
        "units_in": "crore",
        "history": [
            {"total_asset": 1950121, "total_liability": 940495, "period": "Mar 2025"},
            {"total_asset": 1755986, "total_liability": 830198, "period": "Mar 2024"},
        ],
        "full_statement": [
            {"particular": "Total Assets", "history": [{"period": "Mar 2025", "value": 1950121}]}
        ],
    },
}
CASH_FLOW = {
    "status": "success",
    "data": {
        "type": "consolidated",
        "time_period": "yearly",
        "units_in": "crore",
        "cash_flow": [
            {
                "category": "operating",
                "history": [{"value": 178703, "period": "Mar 2025", "change": "+12.54%"}],
            },
            {"category": "investing", "history": [{"value": -137535, "period": "Mar 2025"}]},
        ],
    },
}
HOLDINGS = {
    "status": "success",
    "data": [
        {
            "category": "promoters",
            "history": [
                {"period": "Mar 2026", "value": 50.0},
                {"period": "Dec 2025", "value": 50.01},
            ],
        },
        {"category": "fii", "history": [{"period": "Mar 2026", "value": 18.67}]},
    ],
}
RATIOS = {
    "status": "success",
    "data": [
        {"name": "P/E", "company_value": "20.15", "sector_value": "12.46"},
        {"name": "ROE", "company_value": "8.94%", "sector_value": "16.46%"},
    ],
}
ACTIONS = {
    "status": "success",
    "data": [
        {
            "name": "Dividend",
            "expiry_date": "14 Aug 2025",
            "amount": 5.5,
            "ratio": None,
            "event_details": [{"name": "Announcement date", "value": "25 Apr 2025"}],
        },
        {
            "name": "Bonus",
            "expiry_date": "not a date",
            "amount": None,
            "ratio": "1:1",
            "event_details": [],
        },
    ],
}
ERROR_INVALID_ISIN = {
    "status": "error",
    "errors": [{"errorCode": "UDAPI1206", "message": "Invalid ISIN"}],
}


# --- pure parsers -------------------------------------------------------------------------------


def test_income_statement_is_normalized_with_original_values_preserved():
    rows, skipped = parse_statement(
        INCOME_YEARLY, Statement.INCOME, Basis.CONSOLIDATED, PeriodType.ANNUAL
    )
    revenue = next(
        r for r in rows if r.line_item == "summary.revenue" and r.period_label == "Mar 2026"
    )
    assert revenue.original_value == Decimal("1086181") and revenue.original_unit == "crore"
    assert revenue.period_end == date(2026, 3, 31)
    assert revenue.value_inr == Decimal("10861810000000")  # crore -> rupees
    loss = next(r for r in rows if r.line_item == "summary.net_profit")
    assert loss.original_value == Decimal("-250.5") and loss.value_inr < 0  # negative = loss
    assert skipped == 0


def test_eps_is_a_per_share_value_not_scaled_to_crores():
    rows, _ = parse_statement(
        INCOME_YEARLY, Statement.INCOME, Basis.CONSOLIDATED, PeriodType.ANNUAL
    )
    eps = next(r for r in rows if r.line_item == "EPS - Basic" and r.period_label == "Mar 2025")
    assert eps.original_unit == "inr_per_share" and eps.value_inr == Decimal("51.47")
    pat = next(r for r in rows if r.line_item == "Profit After Tax")
    assert pat.period_type is PeriodType.ANNUAL  # full statements are always annual


def test_quarterly_summary_keeps_the_quarterly_period_type():
    quarterly = json.loads(json.dumps(INCOME_YEARLY))
    quarterly["data"]["time_period"] = "quarterly"
    quarterly["data"]["income_statement"][0]["history"] = [{"value": 100, "period": "Sep 2026"}]
    quarterly["data"].pop("full_statement")
    rows, _ = parse_statement(quarterly, Statement.INCOME, Basis.CONSOLIDATED, PeriodType.QUARTERLY)
    assert {r.period_type for r in rows if r.line_item == "summary.revenue"} == {
        PeriodType.QUARTERLY
    }
    assert rows[0].period_end == date(2026, 9, 30)


def test_balance_sheet_and_cash_flow():
    rows, _ = parse_statement(
        BALANCE, Statement.BALANCE_SHEET, Basis.CONSOLIDATED, PeriodType.ANNUAL
    )
    assert {r.line_item for r in rows} == {
        "summary.total_assets",
        "summary.total_liabilities",
        "Total Assets",
    }
    cf, _ = parse_statement(CASH_FLOW, Statement.CASH_FLOW, Basis.CONSOLIDATED, PeriodType.ANNUAL)
    investing = next(r for r in cf if r.line_item == "summary.investing")
    assert investing.original_value == Decimal("-137535")  # outflow stays negative


def test_unreadable_periods_and_null_values_are_skipped_not_invented():
    bad = json.loads(json.dumps(INCOME_YEARLY))
    bad["data"]["income_statement"][0]["history"] = [
        {"value": 5, "period": "FY2026"},  # unreadable period
        {"value": None, "period": "Mar 2025"},  # missing value
        {"value": 7, "period": "Mar 2024"},
    ]
    rows, skipped = parse_statement(bad, Statement.INCOME, Basis.CONSOLIDATED, PeriodType.ANNUAL)
    revenue_rows = [r for r in rows if r.line_item == "summary.revenue"]
    assert [r.period_label for r in revenue_rows] == ["Mar 2024"]
    assert skipped == 2


def test_unknown_unit_is_kept_but_never_converted():
    odd = json.loads(json.dumps(BALANCE))
    odd["data"]["units_in"] = "furlongs"
    rows, _ = parse_statement(odd, Statement.BALANCE_SHEET, Basis.CONSOLIDATED, PeriodType.ANNUAL)
    assert all(r.value_inr is None and r.original_unit == "furlongs" for r in rows)


def test_basis_mismatch_is_rejected():
    with pytest.raises(UpstoxError, match="standalone"):
        parse_statement(INCOME_YEARLY, Statement.INCOME, Basis.STANDALONE, PeriodType.ANNUAL)


@pytest.mark.parametrize(
    "body", [{}, {"status": "error"}, {"status": "success"}, ERROR_INVALID_ISIN, []]
)
def test_malformed_or_error_payloads_raise(body):
    with pytest.raises(UpstoxError):
        parse_statement(body, Statement.INCOME, Basis.CONSOLIDATED, PeriodType.ANNUAL)


def test_shareholding_ratios_and_actions():
    holdings = parse_shareholding(HOLDINGS)
    assert (holdings[0].category, holdings[0].period_end, holdings[0].percent) == (
        "promoters",
        date(2026, 3, 31),
        Decimal("50.0"),
    )
    ratios = {r.name: r for r in parse_key_ratios(RATIOS)}
    assert ratios["ROE"].is_percent and ratios["ROE"].company_value == Decimal("8.94")
    assert not ratios["P/E"].is_percent and ratios["P/E"].company_value_raw == "20.15"
    actions = parse_corporate_actions(ACTIONS)
    assert actions[0].effective_date == date(2025, 8, 14) and actions[0].amount == Decimal("5.5")
    assert actions[0].ratio_text == ""  # null ratio is not "0"
    assert (
        actions[1].effective_date is None and actions[1].ratio_text == "1:1"
    )  # unparseable date stays unknown


def test_out_of_range_percentages_are_dropped():
    bad = {
        "status": "success",
        "data": [{"category": "fii", "history": [{"period": "Mar 2026", "value": 140}]}],
    }
    assert parse_shareholding(bad) == []


# --- provider with a mock transport --------------------------------------------------------------


@pytest.fixture
def reliance(session):
    seed_reference_data(session)
    seed_test_universe(session)
    c = session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))
    return CompanyRef(
        c.id, c.legal_name, c.common_name, "INE002A01018", "RELIANCE", "500325", c.website
    )


def make_provider(handler, tmp_path=None, token=TOKEN):
    seen = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = PoliteClient(
        ClientConfig(user_agent="t", min_interval_s=0, respect_robots=True, cache_dir=tmp_path),
        transport=httpx.MockTransport(transport),
        sleep=lambda s: None,
    )
    return UpstoxFundamentalsProvider(client, token=token), seen


def json_response(body, status=200):
    return httpx.Response(
        status, content=json.dumps(body), headers={"content-type": "application/json"}
    )


def standard_handler(request: httpx.Request) -> httpx.Response:
    path, query = request.url.path, request.url.query.decode()
    if "standalone" in query:
        return json_response(ERROR_INVALID_ISIN, 400)  # standalone not available for this company
    if path.endswith("/income-statement"):
        return json_response(INCOME_YEARLY)
    if path.endswith("/balance-sheet"):
        return json_response(BALANCE)
    if path.endswith("/cash-flow"):
        return json_response(CASH_FLOW)
    if path.endswith("/share-holdings"):
        return json_response(HOLDINGS)
    if path.endswith("/key-ratios"):
        return json_response(RATIOS)
    if path.endswith("/corporate-actions"):
        return json_response(ACTIONS)
    return json_response(
        {"status": "success", "data": {"company_profile": "x", "sector": "Refineries"}}
    )


def test_without_a_token_the_provider_is_unconfigured_and_makes_no_request(session, reliance):
    provider, seen = make_provider(standard_handler, token="")
    assert not provider.is_configured()
    result = provider.acquire(session, reliance, Dataset.BALANCE_SHEET)
    assert result.outcome is Outcome.UNCONFIGURED and seen == []


def test_unsupported_dataset_and_missing_isin(session, reliance):
    provider, seen = make_provider(standard_handler)
    assert (
        provider.acquire(session, reliance, Dataset.MARKET_PRICES_EOD).outcome
        is Outcome.UNSUPPORTED
    )
    no_isin = CompanyRef(reliance.id, "x", "x", None, None, None, None)
    assert provider.acquire(session, no_isin, Dataset.BALANCE_SHEET).outcome is Outcome.NO_DATA
    assert seen == []


def test_annual_income_is_fetched_stored_and_traceable(session, reliance):
    provider, seen = make_provider(standard_handler)
    result = provider.acquire(session, reliance, Dataset.INCOME_ANNUAL)
    assert result.outcome is Outcome.OK and result.rows > 0
    assert result.periods[0] == "Mar 2026"  # newest first
    # consolidated stored; standalone (HTTP 400) is reported, not hidden, and not substituted
    assert "STANDALONE: CLIENT_ERROR (UDAPI1206)" in result.message
    facts = session.scalars(select(FinancialFact)).all()
    assert {f.basis for f in facts} == {Basis.CONSOLIDATED}
    fact = next(
        f for f in facts if f.line_item == "summary.revenue" and f.period_label == "Mar 2026"
    )
    assert fact.provider_code == "UPSTOX_FUNDAMENTALS" and fact.original_unit == "crore"
    assert (
        "/INE002A01018/income-statement?type=consolidated&time_period=yearly&fs=true"
        in fact.source_url
    )
    payload = session.get(RawPayload, fact.payload_id)
    assert payload.payload["data"]["units_in"] == "crore"  # raw response archived verbatim
    assert all(TOKEN not in r.url.path + r.url.query.decode() for r in seen)


def test_token_is_sent_only_as_a_header_and_never_stored(session, reliance, tmp_path):
    provider, seen = make_provider(standard_handler, tmp_path)
    provider.acquire(session, reliance, Dataset.BALANCE_SHEET)
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in seen)
    stored_text = " ".join(
        [str(session.scalar(select(func.string_agg(RawPayload.request_url, " "))) or "")]
        + [p.read_text(errors="ignore") for p in tmp_path.rglob("*") if p.is_file()]
    )
    assert TOKEN not in stored_text  # not in URLs, payload archive or the HTTP cache


def test_refetch_is_idempotent_and_does_not_duplicate_facts(session, reliance):
    provider, _ = make_provider(standard_handler)
    provider.acquire(session, reliance, Dataset.BALANCE_SHEET)
    first = session.scalar(select(func.count()).select_from(FinancialFact))
    payloads = session.scalar(select(func.count()).select_from(RawPayload))
    provider.acquire(session, reliance, Dataset.BALANCE_SHEET)
    assert session.scalar(select(func.count()).select_from(FinancialFact)) == first
    assert session.scalar(select(func.count()).select_from(RawPayload)) == payloads


def test_quarterly_income_requests_quarterly_without_the_annual_only_full_statement(
    session, reliance
):
    quarterly = json.loads(json.dumps(INCOME_YEARLY))
    quarterly["data"]["time_period"] = "quarterly"
    quarterly["data"].pop("full_statement")
    provider, seen = make_provider(
        lambda r: (
            json_response(quarterly)
            if "standalone" not in r.url.query.decode()
            else json_response({"status": "error"}, 400)
        )
    )
    result = provider.acquire(session, reliance, Dataset.INCOME_QUARTERLY)
    assert result.outcome is Outcome.OK
    assert any(
        "time_period=quarterly" in r.url.query.decode() and "fs=true" not in r.url.query.decode()
        for r in seen
    )
    assert {f.period_type for f in session.scalars(select(FinancialFact))} == {PeriodType.QUARTERLY}


def test_shareholding_ratios_actions_and_profile_are_stored(session, reliance):
    provider, _ = make_provider(standard_handler)
    assert provider.acquire(session, reliance, Dataset.SHAREHOLDING).ok
    assert provider.acquire(session, reliance, Dataset.KEY_RATIOS).ok
    assert provider.acquire(session, reliance, Dataset.CORPORATE_ACTIONS).ok
    profile = provider.acquire(session, reliance, Dataset.COMPANY_PROFILE)
    assert profile.ok and session.scalar(select(func.count()).select_from(ShareholdingFact)) == 3
    assert session.scalar(select(func.count()).select_from(RatioSnapshot)) == 2
    assert session.scalar(select(func.count()).select_from(CorporateAction)) == 2
    assert (
        session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(RawPayload.dataset == "COMPANY_PROFILE")
        )
        == 1
    )


def test_invalid_token_is_reported_as_failure_without_retrying(session, reliance):
    err = {
        "status": "error",
        "errors": [{"errorCode": "UDAPI100050", "message": "Invalid token used to access API"}],
    }
    provider, seen = make_provider(lambda r: json_response(err, 401))
    result = provider.acquire(session, reliance, Dataset.SHAREHOLDING)
    assert result.outcome is Outcome.FAILED and "ACCESS_DENIED (UDAPI100050)" in result.message
    assert len(seen) == 1  # an authentication failure is final


def test_every_basis_failing_is_a_failure_not_silent_empty_success(session, reliance):
    provider, _ = make_provider(lambda r: json_response(ERROR_INVALID_ISIN, 400))
    result = provider.acquire(session, reliance, Dataset.CASH_FLOW)
    assert result.outcome is Outcome.FAILED and result.rows == 0
    assert session.scalar(select(func.count()).select_from(FinancialFact)) == 0


def test_response_with_the_wrong_basis_is_not_stored(session, reliance):
    provider, _ = make_provider(
        lambda r: json_response(INCOME_YEARLY)
    )  # always says "consolidated"
    result = provider.acquire(session, reliance, Dataset.INCOME_ANNUAL)
    assert result.outcome is Outcome.OK  # consolidated call was right
    assert "STANDALONE: invalid response" in result.message
    assert {f.basis for f in session.scalars(select(FinancialFact))} == {Basis.CONSOLIDATED}
