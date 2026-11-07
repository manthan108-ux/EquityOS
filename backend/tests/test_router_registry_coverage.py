"""Router, registry, manual routes and Coverage Engine."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.acquisition.http import ClientConfig, PoliteClient, redact_query
from app.core.database import get_session
from app.coverage import Confidence, Status, Verification, build_coverage
from app.main import create_app
from app.models import (
    Basis,
    Company,
    DatasetVerification,
    Document,
    EodPrice,
    FinancialFact,
    PeriodType,
    Security,
    Source,
    Statement,
)
from app.providers.adapters import GovernmentDataProvider
from app.providers.base import CompanyRef, Dataset, Outcome, ProviderResult, ProviderSpec
from app.providers.company_ir import extract_ir_links, guess_period
from app.providers.manual import (
    ManualImportError,
    ManualProvider,
    import_financials_csv,
    ingest_document_file,
)
from app.providers.registry import provider_statuses, sync_registry, verified_datasets
from app.providers.router import DataRouter
from app.seeds.loader import seed_reference_data, seed_test_universe

TODAY = date(2026, 11, 7)
PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\n%%EOF"


@pytest.fixture
def reliance(session):
    seed_reference_data(session)
    seed_test_universe(session)
    c = session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))
    ref = CompanyRef(
        c.id, c.legal_name, c.common_name, "INE002A01018", "RELIANCE", "500325", c.website
    )
    return c, ref


# --- fake providers for router tests -----------------------------------------------------------


class Fake:
    def __init__(self, code, datasets, outcome=Outcome.OK, configured=True, boom=False):
        self.spec = ProviderSpec(
            code, code, 5, frozenset(datasets), "x", "x", "x", "x", "x", "x", ("TOKEN",)
        )
        self._outcome, self._configured, self._boom, self.calls = outcome, configured, boom, 0

    def is_configured(self):
        return self._configured

    def acquire(self, session, company, dataset, **params):
        self.calls += 1
        if self._boom:
            raise RuntimeError("secret internal detail")
        return ProviderResult(
            self.spec.code, dataset, self._outcome, rows=1 if self._outcome is Outcome.OK else 0
        )


def test_router_asks_only_providers_that_declare_the_dataset(session, reliance):
    _, ref = reliance
    a = Fake("UPSTOX_FUNDAMENTALS", {Dataset.BALANCE_SHEET})
    b = Fake("MARKET_DATA", {Dataset.MARKET_PRICES_EOD})
    route = DataRouter([a, b]).acquire(session, ref, Dataset.BALANCE_SHEET)
    assert route.ok and a.calls == 1 and b.calls == 0


def test_fallback_is_never_silent(session, reliance):
    _, ref = reliance
    primary = Fake("UPSTOX_FUNDAMENTALS", {Dataset.BALANCE_SHEET}, configured=False)
    manual = Fake("MANUAL", {Dataset.BALANCE_SHEET})
    route = DataRouter([primary, manual]).acquire(session, ref, Dataset.BALANCE_SHEET)
    assert route.ok and route.chosen.provider == "MANUAL"
    assert route.fallback_used is True
    assert [(a.provider, a.outcome) for a in route.attempts] == [
        ("UPSTOX_FUNDAMENTALS", Outcome.UNCONFIGURED),
        ("MANUAL", Outcome.OK),
    ]
    assert "UPSTOX_FUNDAMENTALS:UNCONFIGURED -> MANUAL:OK" in route.explain()
    assert primary.calls == 0  # an unconfigured provider is never called


def test_primary_success_means_no_fallback_and_later_providers_untouched(session, reliance):
    _, ref = reliance
    primary, manual = (
        Fake("UPSTOX_FUNDAMENTALS", {Dataset.CASH_FLOW}),
        Fake("MANUAL", {Dataset.CASH_FLOW}),
    )
    route = DataRouter([primary, manual]).acquire(session, ref, Dataset.CASH_FLOW)
    assert (
        route.chosen.provider == "UPSTOX_FUNDAMENTALS"
        and not route.fallback_used
        and manual.calls == 0
    )


def test_all_failing_returns_no_data_and_lists_every_attempt(session, reliance):
    _, ref = reliance
    providers = [
        Fake("UPSTOX_FUNDAMENTALS", {Dataset.CASH_FLOW}, Outcome.FAILED),
        Fake("MANUAL", {Dataset.CASH_FLOW}, Outcome.NO_DATA),
    ]
    route = DataRouter(providers).acquire(session, ref, Dataset.CASH_FLOW)
    assert not route.ok and route.chosen is None and len(route.attempts) == 2


def test_a_crashing_provider_is_contained_and_leaks_no_details(session, reliance):
    _, ref = reliance
    route = DataRouter([Fake("UPSTOX_FUNDAMENTALS", {Dataset.CASH_FLOW}, boom=True)]).acquire(
        session, ref, Dataset.CASH_FLOW
    )
    assert route.attempts[0].outcome is Outcome.FAILED
    assert "secret internal detail" not in route.attempts[0].message


def test_acquire_all_compares_every_source(session, reliance):
    _, ref = reliance
    providers = [
        Fake("UPSTOX_FUNDAMENTALS", {Dataset.CASH_FLOW}),
        Fake("MANUAL", {Dataset.CASH_FLOW}),
    ]
    route = DataRouter(providers).acquire_all(session, ref, Dataset.CASH_FLOW)
    assert len(route.attempts) == 2 and route.chosen.provider == "UPSTOX_FUNDAMENTALS"


# --- registry: declared vs verified ------------------------------------------------------------


def test_declared_support_is_not_verified_support(session):
    provider = Fake("UPSTOX_FUNDAMENTALS", {Dataset.BALANCE_SHEET, Dataset.CASH_FLOW})
    seed_reference_data(session)
    (status,) = provider_statuses(session, [provider])
    assert status.verified == [] and len(status.unverified) == 2  # nothing proven yet
    session.add(
        DatasetVerification(provider_code="UPSTOX_FUNDAMENTALS", dataset="BALANCE_SHEET", ok=True)
    )
    session.flush()
    (status,) = provider_statuses(session, [provider])
    assert [d.value for d in status.verified] == ["BALANCE_SHEET"]
    assert [d.value for d in status.unverified] == ["CASH_FLOW"]


def test_a_later_failure_is_reported_but_does_not_erase_proven_support(session):
    seed_reference_data(session)
    for ok in (True, False):
        session.add(DatasetVerification(provider_code="P", dataset="NEWS", ok=ok))
        session.flush()
    ever_ok, latest_failed = verified_datasets(session, "P")
    assert "NEWS" in ever_ok and "NEWS" in latest_failed


def test_unconfigured_provider_says_what_is_needed(session):
    (status,) = provider_statuses(
        session, [Fake("UPSTOX_FUNDAMENTALS", {Dataset.CASH_FLOW}, configured=False)]
    )
    assert not status.configured and "TOKEN" in status.notes[0]


def test_registry_is_written_to_the_sources_table(session):
    seed_reference_data(session)
    provider = Fake("UPSTOX_FUNDAMENTALS", {Dataset.CASH_FLOW, Dataset.BALANCE_SHEET})
    sync_registry(session, [provider])
    row = session.scalar(select(Source).where(Source.code == "UPSTOX_FUNDAMENTALS"))
    assert row.declared_datasets == ["BALANCE_SHEET", "CASH_FLOW"] and row.access_method == "x"


# --- manual routes -----------------------------------------------------------------------------

CSV_HEADER = "isin,statement,line_item,period_end,period_type,basis,value,unit\n"


def annual_csv(years, basis="CONSOLIDATED", value="100"):
    rows = [f"INE002A01018,INCOME,Revenue,{y}-03-31,ANNUAL,{basis},{value},crore" for y in years]
    return (CSV_HEADER + "\n".join(rows) + "\n").encode()


def test_csv_import_stores_original_and_normalized_values(session, reliance):
    report = import_financials_csv(session, annual_csv([2026, 2025]), "mine.csv")
    assert report.rows == 2 and report.companies == 1
    fact = session.scalar(
        select(FinancialFact).where(FinancialFact.period_end == date(2026, 3, 31))
    )
    assert fact.provider_code == "MANUAL_IMPORT" and fact.original_unit == "crore"
    assert fact.value_inr == Decimal("1000000000") and fact.source_url.startswith(
        "manual-import://mine.csv#"
    )


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (
            CSV_HEADER + "INE000000000,INCOME,Revenue,2026-03-31,ANNUAL,CONSOLIDATED,1,crore\n",
            "unknown ISIN",
        ),
        (
            CSV_HEADER + "INE002A01018,INCOME,Revenue,2026-03-31,ANNUAL,CONSOLIDATED,-,crore\n",
            "missing or not a number",
        ),
        (
            CSV_HEADER + "INE002A01018,INCOME,Revenue,2026-03-31,ANNUAL,CONSOLIDATED,5,furlongs\n",
            "unknown unit",
        ),
        (
            CSV_HEADER + "INE002A01018,INCOME,Revenue,soon,ANNUAL,CONSOLIDATED,5,crore\n",
            "unreadable period_end",
        ),
        (
            CSV_HEADER + "INE002A01018,PROFIT,Revenue,2026-03-31,ANNUAL,CONSOLIDATED,5,crore\n",
            "invalid statement",
        ),
        (
            CSV_HEADER + "INE002A01018,INCOME,Revenue,2026-03-31,ANNUAL,MIXED,5,crore\n",
            "invalid statement",
        ),
        (CSV_HEADER, "no data rows"),
    ],
)
def test_bad_csv_is_rejected_with_a_reason_and_stores_nothing(session, reliance, body, fragment):
    with pytest.raises(ManualImportError, match=fragment):
        import_financials_csv(session, body.encode(), "bad.csv")
    assert session.scalar(select(func.count()).select_from(FinancialFact)) == 0


def test_one_bad_row_rejects_the_whole_file(session, reliance):
    body = (
        annual_csv([2026]).decode()
        + "INE002A01018,INCOME,Revenue,2025-03-31,ANNUAL,CONSOLIDATED,abc,crore\n"
    )
    with pytest.raises(ManualImportError):
        import_financials_csv(session, body.encode(), "partial.csv")
    assert (
        session.scalar(select(func.count()).select_from(FinancialFact)) == 0
    )  # the good row was NOT kept


def test_missing_columns_and_duplicate_rows_are_reported(session, reliance):
    with pytest.raises(ManualImportError, match="missing column"):
        import_financials_csv(session, b"isin,value\nINE002A01018,1\n", "x.csv")
    with pytest.raises(ManualImportError, match="duplicate"):
        import_financials_csv(session, annual_csv([2026, 2026]), "dup.csv")


def test_manual_document_ingestion_is_content_checked_and_path_safe(session, reliance, tmp_path):
    c, _ = reliance
    good = tmp_path / "../evil name.pdf"
    (tmp_path / "evil name.pdf").write_bytes(PDF)
    doc, created = ingest_document_file(
        session,
        tmp_path / "evil name.pdf",
        company_id=c.id,
        doc_type="annual_report",
        financial_period="FY2025",
        data_dir=tmp_path / "store",
    )
    assert created and doc.url == "manual-upload://evil name.pdf"
    stored = tmp_path / "store" / doc.storage_path
    assert stored.read_bytes() == PDF and stored.name == f"{doc.sha256}.pdf"  # name from the hash
    assert good.name  # the user-supplied name never reached the filesystem path
    (tmp_path / "x.exe").write_bytes(b"MZ\x90 binary")
    with pytest.raises(ValueError, match="unsafe"):
        ingest_document_file(
            session, tmp_path / "x.exe", company_id=c.id, doc_type="other", data_dir=tmp_path
        )


def test_manual_provider_reports_what_was_supplied(session, reliance, tmp_path):
    c, ref = reliance
    provider = ManualProvider()
    assert provider.acquire(session, ref, Dataset.BALANCE_SHEET).outcome is Outcome.NO_DATA
    import_financials_csv(session, annual_csv([2026]), "a.csv")
    assert provider.acquire(session, ref, Dataset.INCOME_ANNUAL).ok
    assert provider.acquire(session, ref, Dataset.INCOME_QUARTERLY).outcome is Outcome.NO_DATA
    assert provider.acquire(session, ref, Dataset.NEWS).outcome is Outcome.UNSUPPORTED


# --- credentials never leak ---------------------------------------------------------------------


def test_query_secrets_are_redacted_from_urls():
    url = "https://api.example.test/r/1?api-key=SECRET123&format=json&limit=5"
    assert (
        redact_query(url, ("api-key",))
        == "https://api.example.test/r/1?api-key=REDACTED&format=json&limit=5"
    )


def test_government_provider_sends_the_key_but_never_records_it(session, reliance):
    _, ref = reliance
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"records": [{"a": 1}], "total": 1})

    client = PoliteClient(
        ClientConfig(user_agent="t", min_interval_s=0, respect_robots=True),
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    provider = GovernmentDataProvider(client, api_key="SECRET123")
    result = provider.acquire(session, ref, Dataset.GOVERNMENT_STATISTICS, resource_id="abc")
    assert result.ok and any("SECRET123" in u for u in seen)  # sent on the wire
    assert "SECRET123" not in (result.source_url or "")
    from app.models import RawPayload

    assert all("SECRET123" not in r for r in session.scalars(select(RawPayload.request_url)))
    assert (
        GovernmentDataProvider(client, api_key="")
        .acquire(session, ref, Dataset.GOVERNMENT_STATISTICS, resource_id="abc")
        .outcome
        is Outcome.UNCONFIGURED
    )


# --- company IR classification --------------------------------------------------------------------


def test_links_are_classified_by_file_name_not_by_folder():
    html = (
        '<a href="/annual-reports/2026/AR2025-2026.pdf">Annual Report 2025-26</a>'
        '<a href="/annual-reports/2026/Company_ESG-Report_2025-26.pdf">Download</a>'
        '<a href="/annual-reports/2026/BRSR_2025-26.pdf">BRSR</a>'
    )
    links = extract_ir_links(html, "https://www.example.com/")
    assert [(link.kind, link.url.rsplit("/", 1)[-1]) for link in links] == [
        ("annual_report", "AR2025-2026.pdf")
    ]


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("financial-results-for-quarter-ended-sep-30-2026.pdf", "Q2FY2027"),
        ("amc-investor-presentation-six-months-ended-sep-30-2026.pdf", "Q2FY2027"),
        ("Q3 FY26 results", "Q3FY2026"),
        ("Annual Report 2024-25", "FY2025"),
        ("results quarter ended Jul 31 2026", None),
        ("no period here", None),
    ],
)
def test_period_hints(text, hint):
    assert guess_period(text) == hint


# --- coverage engine ---------------------------------------------------------------------------


def coverage(session, company):
    return {d.dataset: d for d in build_coverage(session, company.id, TODAY).datasets}


def add_fact(
    session,
    company,
    provider,
    year,
    value,
    basis=Basis.CONSOLIDATED,
    line="Revenue",
    statement=Statement.INCOME,
    period_type=PeriodType.ANNUAL,
    month=3,
    day=31,
):
    session.add(
        FinancialFact(
            company_id=company.id,
            provider_code=provider,
            statement=statement,
            line_item=line,
            period_end=date(year, month, day),
            period_type=period_type,
            basis=basis,
            original_value=Decimal(value),
            original_unit="crore",
            period_label=f"{year}",
            value_inr=Decimal(value) * Decimal(10**7),
            source_url="u",
            retrieved_at=datetime.now(UTC),
        )
    )
    session.flush()


def test_an_empty_company_reports_everything_missing_and_invents_nothing(session, reliance):
    c, _ = reliance
    report = build_coverage(session, c.id, TODAY)
    by = {d.dataset: d for d in report.datasets}
    assert by["INCOME_ANNUAL"].status is Status.MISSING and by["INCOME_ANNUAL"].completeness == 0
    assert (
        by["INCOME_ANNUAL"].available_periods == []
        and len(by["INCOME_ANNUAL"].missing_periods) == 5
    )
    assert (
        by["INCOME_ANNUAL"].verification is Verification.NOT_AVAILABLE
        and by["INCOME_ANNUAL"].confidence is Confidence.NONE
    )
    assert by["CORPORATE_ANNOUNCEMENTS"].status is Status.NOT_SUPPORTED
    assert by["NEWS"].status is Status.ON_DEMAND and by["NEWS"].missing_periods == []
    assert report.overall_completeness == 0 and report.can_proceed is True
    assert "Revenue & Growth" in report.affected_pillars and report.datasets_not_supported == [
        "CORPORATE_ANNOUNCEMENTS"
    ]


def test_required_annual_periods_follow_the_filing_calendar(session, reliance):
    c, _ = reliance
    assert coverage(session, c)["INCOME_ANNUAL"].required_periods == [
        "FY2026",
        "FY2025",
        "FY2024",
        "FY2023",
        "FY2022",
    ]
    early = build_coverage(session, c.id, date(2026, 4, 20))  # FY2026 results not yet due
    assert (
        next(d for d in early.datasets if d.dataset == "INCOME_ANNUAL").required_periods[0]
        == "FY2025"
    )


def test_partial_history_is_partial_with_exact_missing_periods(session, reliance):
    c, _ = reliance
    for y in (2026, 2025, 2024):
        add_fact(session, c, "MANUAL_IMPORT", y, 100)
    d = coverage(session, c)["INCOME_ANNUAL"]
    assert d.status is Status.PARTIAL and d.completeness == 0.6
    assert d.available_periods == ["FY2026", "FY2025", "FY2024"] and d.missing_periods == [
        "FY2023",
        "FY2022",
    ]
    assert d.verification is Verification.USER_SUPPLIED and d.confidence is not Confidence.HIGH
    assert "Profitability" in d.affected_pillars


def test_full_history_is_complete(session, reliance):
    c, _ = reliance
    for y in range(2022, 2027):
        add_fact(session, c, "MANUAL_IMPORT", y, 100)
    d = coverage(session, c)["INCOME_ANNUAL"]
    assert d.status is Status.COMPLETE and d.completeness == 1.0 and d.missing_periods == []
    assert d.affected_pillars == []


def test_a_published_period_counts_even_before_it_is_due(session, reliance):
    c, _ = reliance
    add_fact(
        session,
        c,
        "MANUAL_IMPORT",
        2026,
        50,
        statement=Statement.INCOME,
        period_type=PeriodType.QUARTERLY,
        month=9,
        day=30,
    )  # Sep-2026 quarter, "due" only by Dec 1
    d = coverage(session, c)["INCOME_QUARTERLY"]
    assert d.required_periods[0] == "Q2FY2027" and "Q2FY2027" in d.available_periods


def test_consolidated_and_standalone_are_never_blended(session, reliance):
    c, _ = reliance
    add_fact(session, c, "MANUAL_IMPORT", 2026, 100, basis=Basis.CONSOLIDATED)
    add_fact(session, c, "MANUAL_IMPORT", 2025, 90, basis=Basis.STANDALONE)  # a different basis
    d = coverage(session, c)["INCOME_ANNUAL"]
    assert d.basis == "CONSOLIDATED" and d.available_periods == ["FY2026"]
    assert any("kept separate" in lim for lim in d.limitations)


def test_standalone_only_is_disclosed(session, reliance):
    c, _ = reliance
    add_fact(session, c, "MANUAL_IMPORT", 2026, 100, basis=Basis.STANDALONE)
    d = coverage(session, c)["INCOME_ANNUAL"]
    assert d.basis == "STANDALONE" and any("only standalone" in lim for lim in d.limitations)


def test_disagreeing_sources_are_a_visible_conflict(session, reliance):
    c, _ = reliance
    add_fact(session, c, "UPSTOX_FUNDAMENTALS", 2026, 100)
    add_fact(session, c, "MANUAL_IMPORT", 2026, 110)  # 10% apart
    d = coverage(session, c)["INCOME_ANNUAL"]
    assert d.verification is Verification.CONFLICT and d.confidence is Confidence.LOW


def test_agreeing_sources_are_not_a_conflict(session, reliance):
    c, _ = reliance
    add_fact(session, c, "UPSTOX_FUNDAMENTALS", 2026, 1000)
    add_fact(session, c, "MANUAL_IMPORT", 2026, 1000.5)  # within 1%
    assert coverage(session, c)["INCOME_ANNUAL"].verification is not Verification.CONFLICT


def test_provider_data_is_unverified_until_a_live_retrieval_is_recorded(session, reliance):
    c, _ = reliance
    for y in range(2022, 2027):
        add_fact(session, c, "UPSTOX_FUNDAMENTALS", y, 100)
    d = coverage(session, c)["INCOME_ANNUAL"]
    assert d.verification is Verification.UNVERIFIED_SOURCE and d.confidence is Confidence.LOW
    session.add(
        DatasetVerification(provider_code="UPSTOX_FUNDAMENTALS", dataset="INCOME_ANNUAL", ok=True)
    )
    session.flush()
    d = coverage(session, c)["INCOME_ANNUAL"]
    assert d.verification is Verification.SINGLE_SOURCE and d.confidence is Confidence.MEDIUM


def add_prices(session, company, days, end=TODAY - timedelta(days=1)):
    sec = session.scalar(select(Security).where(Security.company_id == company.id).limit(1))
    day, n = end, 0
    while n < days:
        if day.weekday() < 5:
            session.add(
                EodPrice(
                    security_id=sec.id,
                    trade_date=day,
                    series="EQ",
                    close=Decimal(100),
                    source_url="u",
                    file_sha256="0" * 64,
                    retrieved_at=datetime.now(UTC),
                )
            )
            n += 1
        day -= timedelta(days=1)
    session.flush()


def test_price_coverage_is_measured_in_trading_days(session, reliance):
    c, _ = reliance
    add_prices(session, c, 3)
    d = coverage(session, c)["MARKET_PRICES_EOD"]
    assert d.status is Status.PARTIAL and d.completeness < 0.05 and d.missing_periods
    assert any("NOT adjusted" in lim for lim in d.limitations)


def test_a_year_of_prices_is_complete_and_stale_prices_are_flagged(session, reliance):
    c, _ = reliance
    add_prices(session, c, 250)
    d = coverage(session, c)["MARKET_PRICES_EOD"]
    assert d.status is Status.COMPLETE and d.completeness == 1.0
    stale = build_coverage(session, c.id, TODAY + timedelta(days=20))
    p = next(x for x in stale.datasets if x.dataset == "MARKET_PRICES_EOD")
    assert (
        any("do not present it as current" in lim for lim in p.limitations)
        and p.confidence is Confidence.LOW
    )


def test_documents_are_matched_by_financial_period(session, reliance, tmp_path):
    c, _ = reliance
    for period in ("FY2026", "FY2025"):
        body = PDF + period.encode()
        ingest_document_file_bytes(session, c, tmp_path, body, period)
    d = coverage(session, c)["ANNUAL_REPORTS"]
    assert d.available_periods == ["FY2026", "FY2025"] and d.missing_periods == ["FY2024"]
    assert round(d.completeness, 2) == 0.67


def ingest_document_file_bytes(session, company, tmp_path, body, period):
    path = tmp_path / f"{period}.pdf"
    path.write_bytes(body)
    return ingest_document_file(
        session,
        path,
        company_id=company.id,
        doc_type="annual_report",
        financial_period=period,
        data_dir=tmp_path / "store",
    )


def test_documents_without_a_period_cannot_be_matched_and_say_so(session, reliance, tmp_path):
    c, _ = reliance
    ingest_document_file_bytes(session, c, tmp_path, PDF + b"x", None)
    d = coverage(session, c)["ANNUAL_REPORTS"]
    assert d.available_periods == [] and any("no financial period" in lim for lim in d.limitations)
    assert session.scalar(select(func.count()).select_from(Document)) == 1


def test_coverage_endpoint(session, reliance):
    c, _ = reliance
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    body = client.get(f"/api/companies/{c.id}/coverage").json()
    assert body["company_name"] == "Reliance Industries" and body["can_proceed"] is True
    assert {d["dataset"] for d in body["datasets"]} >= {
        "INCOME_ANNUAL",
        "MARKET_PRICES_EOD",
        "NEWS",
    }
    assert client.get(f"/api/companies/{uuid.uuid4()}/coverage").status_code == 404
    status = client.get("/api/providers/status").json()
    assert all("verified_datasets" in s and "unverified_datasets" in s for s in status)


def test_manually_supplied_announcements_are_reported_but_not_called_complete(
    session, reliance, tmp_path
):
    c, _ = reliance
    assert coverage(session, c)["CORPORATE_ANNOUNCEMENTS"].status is Status.NOT_SUPPORTED
    path = tmp_path / "ann.pdf"
    path.write_bytes(PDF + b"announcement")
    ingest_document_file(
        session, path, company_id=c.id, doc_type="corporate_announcement", data_dir=tmp_path / "s"
    )
    d = coverage(session, c)["CORPORATE_ANNOUNCEMENTS"]
    assert d.status is Status.PARTIAL and d.completeness is None
    assert d.available_periods == ["1 manually supplied announcement document(s)"]
    assert d.verification is Verification.USER_SUPPLIED
