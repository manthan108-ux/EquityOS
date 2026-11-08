"""Upstox Company Fundamentals API adapter.

Official docs: https://upstox.com/developer/api-documentation/ (Fundamentals section; launched
2026-05-11). Verified from the documentation on 2026-11-07:
  * Base URL   https://api.upstox.com/v2/fundamentals/{isin}/<endpoint>
  * Endpoints  profile, income-statement, balance-sheet, cash-flow, share-holdings, key-ratios,
               corporate-actions, competitors
  * Auth       "Authorization: Bearer <token>". A free read-only Analytics Token (1 year, no static
               IP needed, fundamentals included) can be generated on the Upstox Developer Apps page.
  * Units      monetary values in Indian rupees, crore ("units_in": "crore"); EPS is per share.
  * Period     labels such as "Mar 2026".

NOT documented: eligibility beyond holding an Upstox developer app, cost, fundamentals rate limits,
coverage of companies/years, and data-use terms. Upstox's AI-licensing page restricts republishing
its content as a competing product. We use the data privately, label it with its source, and never
redistribute the raw feed.

STATUS: written from the documentation and tested against documented response shapes. It counts as
supporting a dataset only after `verify()` records a successful LIVE retrieval, which requires a
token in the environment. Without a token every call returns UNCONFIGURED.
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.acquisition.http import FetchResult, PoliteClient
from app.core.config import get_settings
from app.models import Basis, PeriodType, Statement
from app.normalization import normalized_value, parse_amount, parse_percent, parse_period_label
from app.providers.base import CompanyRef, Dataset, Outcome, ProviderResult, ProviderSpec
from app.providers.persistence import (
    ActionRow,
    FactRow,
    HoldingRow,
    RatioRow,
    archive_payload,
    store_corporate_actions,
    store_financial_facts,
    store_ratio_snapshot,
    store_shareholding,
)

BASE_URL = "https://api.upstox.com/v2/fundamentals"
SOURCE_CODE = "UPSTOX_FUNDAMENTALS"
CACHE_TTL = timedelta(hours=6)

SPEC = ProviderSpec(
    code=SOURCE_CODE,
    name="Upstox Company Fundamentals API",
    authority_tier=5,  # licensed aggregator, not a primary filing
    datasets=frozenset(
        {
            Dataset.COMPANY_PROFILE,
            Dataset.INCOME_ANNUAL,
            Dataset.INCOME_QUARTERLY,
            Dataset.BALANCE_SHEET,
            Dataset.CASH_FLOW,
            Dataset.SHAREHOLDING,
            Dataset.KEY_RATIOS,
            Dataset.CORPORATE_ACTIONS,
            Dataset.COMPETITORS,
        }
    ),
    auth_requirement="Upstox developer app + Bearer token (free read-only Analytics Token, 1 year)",
    access_method="Documented authenticated REST API (GET, JSON), keyed by ISIN",
    rate_limits="Not documented for fundamentals; general 'other standard APIs': 50/s, 500/min, "
    "2000/30min (unconfirmed for this API). This adapter makes at most ~13 calls per company.",
    coverage_note="Documented: income statement (yearly, quarterly), balance sheet and cash flow "
    "(yearly), consolidated and standalone. History length and company coverage are not "
    "documented.",
    terms_note="Licensed API data. Use privately; keep attribution; no standalone redistribution.",
    redistribution_policy="No redistribution of the raw feed; derived analysis with attribution",
    env_vars=("UPSTOX_ANALYTICS_TOKEN",),
)


class UpstoxError(Exception):
    """A response that did not match the documented schema."""


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    return parse_amount(value if isinstance(value, str) else str(value))


def _check(body: dict, requested_basis: str | None = None) -> dict:
    if not isinstance(body, dict) or body.get("status") != "success" or "data" not in body:
        raise UpstoxError("response is not a successful Upstox payload")
    data = body["data"]
    if requested_basis and isinstance(data, dict):
        got = (data.get("type") or "").lower()
        if got and got != requested_basis:
            raise UpstoxError(f"asked for {requested_basis} but received {got}")
    return data


def _fact(
    statement: Statement,
    line_item: str,
    label: str,
    value: Any,
    unit: str,
    period_type: PeriodType,
    basis: Basis,
) -> FactRow | None:
    period_end = parse_period_label(label)
    amount = _dec(value)
    if period_end is None or amount is None:
        return None  # unparseable period or missing value: skipped, never invented
    return FactRow(
        statement,
        line_item,
        period_end,
        period_type,
        basis,
        amount,
        unit,
        label,
        normalized_value(amount, unit),
    )


def _unit_for(particular: str, units_in: str) -> str:
    return "inr_per_share" if particular.strip().upper().startswith("EPS") else units_in


def parse_statement(
    body: dict, statement: Statement, basis: Basis, period_type: PeriodType
) -> tuple[list[FactRow], int]:
    """Parse income / balance sheet / cash flow responses. Returns (rows, skipped value count)."""
    data = _check(body, basis.value.lower())
    units = str(data.get("units_in") or "").lower()
    rows: list[FactRow] = []
    skipped = 0

    def add(line: str, label: str, value: Any, unit: str, ptype: PeriodType) -> None:
        nonlocal skipped
        fact = _fact(statement, line, label, value, unit, ptype, basis)
        if fact:
            rows.append(fact)
        else:
            skipped += 1

    if statement is Statement.INCOME:
        for block in data.get("income_statement") or []:
            for h in block.get("history") or []:
                add(
                    f"summary.{block.get('category')}",
                    h.get("period", ""),
                    h.get("value"),
                    units,
                    period_type,
                )
    elif statement is Statement.CASH_FLOW:
        for block in data.get("cash_flow") or []:
            for h in block.get("history") or []:
                add(
                    f"summary.{block.get('category')}",
                    h.get("period", ""),
                    h.get("value"),
                    units,
                    period_type,
                )
    else:
        for h in data.get("history") or []:
            add(
                "summary.total_assets",
                h.get("period", ""),
                h.get("total_asset"),
                units,
                period_type,
            )
            add(
                "summary.total_liabilities",
                h.get("period", ""),
                h.get("total_liability"),
                units,
                period_type,
            )

    # The full statement is always annual, whatever the summary period type.
    for block in data.get("full_statement") or []:
        particular = str(block.get("particular") or "").strip()
        for h in block.get("history") or []:
            add(
                particular,
                h.get("period", ""),
                h.get("value"),
                _unit_for(particular, units),
                PeriodType.ANNUAL,
            )
    return rows, skipped


def parse_shareholding(body: dict) -> list[HoldingRow]:
    data = _check(body)
    if not isinstance(data, list):
        raise UpstoxError("share-holdings data is not a list")
    out = []
    for block in data:
        for h in block.get("history") or []:
            end, pct = parse_period_label(h.get("period", "")), _dec(h.get("value"))
            if end is not None and pct is not None and Decimal(0) <= pct <= Decimal(100):
                out.append(HoldingRow(str(block.get("category")), end, h["period"], pct))
    return out


def parse_key_ratios(body: dict) -> list[RatioRow]:
    data = _check(body)
    if not isinstance(data, list):
        raise UpstoxError("key-ratios data is not a list")
    out = []
    for item in data:
        company_raw, sector_raw = item.get("company_value"), item.get("sector_value")
        value, is_pct = parse_percent(company_raw if isinstance(company_raw, str) else None)
        sector, _ = parse_percent(sector_raw if isinstance(sector_raw, str) else None)
        if item.get("name") and company_raw is not None:
            out.append(
                RatioRow(str(item["name"]), str(company_raw), sector_raw, value, sector, is_pct)
            )
    return out


def _action_date(text: str | None) -> date | None:
    if not text:
        return None
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_corporate_actions(body: dict) -> list[ActionRow]:
    data = _check(body)
    if not isinstance(data, list):
        raise UpstoxError("corporate-actions data is not a list")
    out = []
    for item in data:
        if not item.get("name"):
            continue
        out.append(
            ActionRow(
                action_type=str(item["name"]),
                effective_date=_action_date(item.get("expiry_date")),
                amount=_dec(item.get("amount")),
                ratio_text=str(item.get("ratio") or ""),
                details=item.get("event_details"),
            )
        )
    return out


class UpstoxFundamentalsProvider:
    spec = SPEC

    def __init__(self, client: PoliteClient, token: str | None = None) -> None:
        self._client = client
        self._token_override = token

    # ---- configuration ---------------------------------------------------------------------

    def _token(self) -> str | None:
        if self._token_override is not None:
            return self._token_override.strip() or None
        settings = get_settings()
        token = settings.upstox_analytics_token or settings.upstox_access_token
        return token.strip() if token and token.strip() else None

    def is_configured(self) -> bool:
        if self._token_override is None and not get_settings().upstox_enabled:
            return False  # feature flag off: behaves exactly like "no token"
        return self._token() is not None

    # ---- HTTP ------------------------------------------------------------------------------

    def _get(
        self, isin: str, endpoint: str, query: str = ""
    ) -> tuple[FetchResult, dict | None, str]:
        url = f"{BASE_URL}/{isin}/{endpoint}{('?' + query) if query else ''}"
        result = self._client.fetch(
            url,
            source=SOURCE_CODE,
            ttl=CACHE_TTL,
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._token()}"},
            documented_api=True,
        )
        body = None
        if result.content:
            try:
                body = json.loads(result.content)
            except ValueError:
                body = None
        return result, body, url

    @staticmethod
    def _error_text(result: FetchResult, body: dict | None) -> str:
        codes = []
        if isinstance(body, dict):
            codes = [e.get("errorCode") or e.get("error_code") for e in body.get("errors") or []]
        detail = f" ({', '.join(c for c in codes if c)})" if codes else ""
        return f"{result.status.value}{detail}"

    # ---- public API ------------------------------------------------------------------------

    def acquire(
        self, session: Session, company: CompanyRef, dataset: Dataset, **params: Any
    ) -> ProviderResult:
        def fail(outcome: Outcome, message: str, **kw: Any) -> ProviderResult:
            return ProviderResult(SOURCE_CODE, dataset, outcome, message, **kw)

        if dataset not in SPEC.datasets:
            return fail(Outcome.UNSUPPORTED, f"Upstox does not offer {dataset.value}")
        if not self.is_configured():
            return fail(
                Outcome.UNCONFIGURED,
                "Upstox is not configured (no UPSTOX_ANALYTICS_TOKEN, or UPSTOX_ENABLED=false)",
            )
        if not company.isin:
            return fail(Outcome.NO_DATA, "Company has no ISIN; Upstox is keyed by ISIN")

        handler = {
            Dataset.INCOME_ANNUAL: lambda: self._statement(
                session,
                company,
                dataset,
                "income-statement",
                Statement.INCOME,
                PeriodType.ANNUAL,
                "yearly",
            ),
            Dataset.INCOME_QUARTERLY: lambda: self._statement(
                session,
                company,
                dataset,
                "income-statement",
                Statement.INCOME,
                PeriodType.QUARTERLY,
                "quarterly",
            ),
            Dataset.BALANCE_SHEET: lambda: self._statement(
                session,
                company,
                dataset,
                "balance-sheet",
                Statement.BALANCE_SHEET,
                PeriodType.ANNUAL,
                None,
            ),
            Dataset.CASH_FLOW: lambda: self._statement(
                session, company, dataset, "cash-flow", Statement.CASH_FLOW, PeriodType.ANNUAL, None
            ),
            Dataset.SHAREHOLDING: lambda: self._simple(session, company, dataset, "share-holdings"),
            Dataset.KEY_RATIOS: lambda: self._simple(session, company, dataset, "key-ratios"),
            Dataset.CORPORATE_ACTIONS: lambda: self._simple(
                session, company, dataset, "corporate-actions"
            ),
            Dataset.COMPANY_PROFILE: lambda: self._simple(session, company, dataset, "profile"),
            Dataset.COMPETITORS: lambda: self._simple(session, company, dataset, "competitors"),
        }[dataset]
        return handler()

    # ---- statements (consolidated and standalone are fetched and stored separately) ---------

    def _statement(
        self,
        session: Session,
        company: CompanyRef,
        dataset: Dataset,
        endpoint: str,
        statement: Statement,
        period_type: PeriodType,
        time_period: str | None,
    ) -> ProviderResult:
        total, periods, notes, last_url, last_time = 0, set(), [], None, None
        any_success = False
        for basis in (Basis.CONSOLIDATED, Basis.STANDALONE):
            query = f"type={basis.value.lower()}"
            if time_period:
                query += f"&time_period={time_period}"
            if time_period != "quarterly":
                query += "&fs=true"  # the full line-item statement is annual only
            result, body, url = self._get(company.isin or "", endpoint, query)
            if not result.ok or body is None:
                notes.append(f"{basis.value}: {self._error_text(result, body)}")
                continue
            try:
                rows, skipped = parse_statement(body, statement, basis, period_type)
            except UpstoxError as exc:
                notes.append(f"{basis.value}: invalid response ({exc})")
                continue
            any_success = True
            retrieved = result.retrieved_at or datetime.now(UTC)
            payload = archive_payload(
                session,
                company_id=company.id,
                provider_code=SOURCE_CODE,
                dataset=dataset.value,
                request_url=url,
                payload=body,
                retrieved_at=retrieved,
            )
            total += store_financial_facts(
                session,
                company.id,
                SOURCE_CODE,
                rows,
                source_url=url,
                retrieved_at=retrieved,
                payload_id=payload.id,
            )
            periods |= {r.period_label for r in rows}
            if skipped:
                notes.append(
                    f"{basis.value}: {skipped} values skipped (missing value or unreadable period)"
                )
            if rows:
                notes.append(f"{basis.value}: {len(rows)} values")
            last_url, last_time = url, retrieved
        if total:
            return ProviderResult(
                SOURCE_CODE,
                dataset,
                Outcome.OK,
                "; ".join(notes),
                total,
                sorted(periods, key=lambda p: parse_period_label(p) or date.min, reverse=True),
                last_url,
                last_time,
            )
        outcome = Outcome.NO_DATA if any_success else Outcome.FAILED
        return ProviderResult(SOURCE_CODE, dataset, outcome, "; ".join(notes) or "no data returned")

    # ---- single-call datasets ---------------------------------------------------------------

    def _simple(
        self, session: Session, company: CompanyRef, dataset: Dataset, endpoint: str
    ) -> ProviderResult:
        result, body, url = self._get(company.isin or "", endpoint)
        if not result.ok or body is None:
            return ProviderResult(
                SOURCE_CODE,
                dataset,
                Outcome.FAILED,
                self._error_text(result, body),
                fetch_status=result.status.value,
            )
        retrieved = result.retrieved_at or datetime.now(UTC)
        try:
            if dataset is Dataset.SHAREHOLDING:
                rows = parse_shareholding(body)
            elif dataset is Dataset.KEY_RATIOS:
                rows = parse_key_ratios(body)
            elif dataset is Dataset.CORPORATE_ACTIONS:
                rows = parse_corporate_actions(body)
            else:
                data = _check(body)
                rows = [data] if data else []
        except UpstoxError as exc:
            return ProviderResult(SOURCE_CODE, dataset, Outcome.FAILED, f"invalid response ({exc})")

        payload = archive_payload(
            session,
            company_id=company.id,
            provider_code=SOURCE_CODE,
            dataset=dataset.value,
            request_url=url,
            payload=body,
            retrieved_at=retrieved,
        )
        if not rows:
            return ProviderResult(
                SOURCE_CODE,
                dataset,
                Outcome.NO_DATA,
                "empty response",
                source_url=url,
                retrieved_at=retrieved,
            )
        stored = len(rows)
        periods: list[str] = []
        if dataset is Dataset.SHAREHOLDING:
            stored = store_shareholding(
                session,
                company.id,
                SOURCE_CODE,
                rows,
                source_url=url,
                retrieved_at=retrieved,
                payload_id=payload.id,
            )
            periods = sorted(
                {r.period_label for r in rows},
                key=lambda p: parse_period_label(p) or date.min,
                reverse=True,
            )
        elif dataset is Dataset.KEY_RATIOS:
            stored = store_ratio_snapshot(
                session, company.id, SOURCE_CODE, rows, source_url=url, retrieved_at=retrieved
            )
        elif dataset is Dataset.CORPORATE_ACTIONS:
            stored = store_corporate_actions(
                session,
                company.id,
                SOURCE_CODE,
                rows,
                source_url=url,
                retrieved_at=retrieved,
                payload_id=payload.id,
            )
        # Profile and competitors are kept as the archived raw payload only.
        return ProviderResult(
            SOURCE_CODE, dataset, Outcome.OK, f"{stored} records", stored, periods, url, retrieved
        )
