"""Data Coverage Engine: what do we actually have for a company, and how much can we trust it?

Run BEFORE the research agents. The research workflow continues with partial coverage, but every
conclusion that depends on a missing or weak dataset is flagged through `affected_pillars`.

Honesty rules:
  * Completeness counts only data that exists in the database. A missing period is reported as
    missing; it is never filled, estimated or assumed to be zero.
  * "Verified" means the provider has a recorded, successful live retrieval for that dataset
    (`dataset_verifications`). It does not mean the numbers are correct.
  * Confidence is a transparent category with written reasons, not a probability.
  * Datasets with no permitted source are reported as NOT_SUPPORTED, not silently dropped.
"""

import enum
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Basis,
    Company,
    CorporateAction,
    Document,
    EodPrice,
    FinancialFact,
    PeriodType,
    RatioSnapshot,
    RawPayload,
    Security,
    ShareholdingFact,
    Statement,
)
from app.normalization import (
    fiscal_year_label,
    latest_expected_annual,
    latest_expected_quarter,
    previous_annual,
    previous_quarters,
    quarter_label,
)
from app.providers.base import Dataset
from app.providers.registry import verified_datasets

ANNUAL_YEARS = 5
QUARTERS = 8
DOC_YEARS = 3
DOC_QUARTERS = 4
PRICE_DAYS_REQUIRED = 245  # about one year of trading sessions
PRICE_STALE_DAYS = 6  # a weekend plus a holiday
CONFLICT_TOLERANCE = Decimal("0.01")  # 1% relative difference between sources

MANUAL_CODES = {"MANUAL_IMPORT", "MANUAL_UPLOAD"}


class Status(enum.StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    NOT_SUPPORTED = "NOT_SUPPORTED"  # no permitted source exists for this dataset
    ON_DEMAND = "ON_DEMAND"  # fetched at research time, not stored


class Verification(enum.StrEnum):
    VERIFIED = "VERIFIED"  # supplied by a provider with a recorded successful live retrieval
    SINGLE_SOURCE = "SINGLE_SOURCE"  # verified provider, one source, not cross-checked
    UNVERIFIED_SOURCE = "UNVERIFIED_SOURCE"  # provider support not yet proven live
    USER_SUPPLIED = "USER_SUPPLIED"
    CONFLICT = "CONFLICT"  # two sources disagree beyond tolerance
    NOT_AVAILABLE = "NOT_AVAILABLE"


class Confidence(enum.StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class SourceUsed(BaseModel):
    provider: str
    verified: bool
    user_supplied: bool = False


class DatasetCoverage(BaseModel):
    dataset: str
    label: str
    basis: str | None = None
    status: Status
    required_periods: list[str]
    available_periods: list[str]
    missing_periods: list[str]
    completeness: float | None  # 0..1 over required periods; None when not measurable
    sources: list[SourceUsed]
    latest_period: str | None = None
    retrieved_at: datetime | None = None
    age_days: int | None = None  # days since the latest period/observation
    verification: Verification
    confidence: Confidence
    confidence_basis: list[str]
    limitations: list[str]
    affected_pillars: list[str]


class CoverageReport(BaseModel):
    company_id: uuid.UUID
    company_name: str
    as_of: date
    datasets: list[DatasetCoverage]
    overall_completeness: float | None
    datasets_missing: list[str]
    datasets_not_supported: list[str]
    affected_pillars: list[str]
    can_proceed: bool
    methodology: str


METHODOLOGY = (
    "Completeness = available required periods / required periods, averaged over the measurable "
    "datasets. Required periods: last 5 fiscal years (annual statements), 8 quarters (quarterly "
    "results, shareholding), about one year of trading sessions (prices), 3 annual reports, 4 "
    "quarters of result documents. A period is required once results are due (60 days after quarter "
    "end, 75 days after year end). Only data present in the database counts; nothing is estimated."
)

PILLARS = {
    Dataset.COMPANY_PROFILE: ["Business & Company Analysis"],
    Dataset.INCOME_ANNUAL: [
        "Revenue & Growth",
        "Profitability",
        "Historical Performance",
        "Ratio Analysis",
    ],
    Dataset.INCOME_QUARTERLY: ["Quarterly Analysis", "Revenue & Growth", "Profitability"],
    Dataset.BALANCE_SHEET: ["Balance Sheet", "Ratio Analysis", "Capital Structure"],
    Dataset.CASH_FLOW: ["Cash Flow", "Ratio Analysis", "Valuation"],
    Dataset.SHAREHOLDING: ["Shareholding & Ownership"],
    Dataset.KEY_RATIOS: ["Ratio Analysis", "Valuation", "Peer Comparison"],
    Dataset.CORPORATE_ACTIONS: ["Capital Structure", "Valuation", "Technical Analysis"],
    Dataset.COMPETITORS: ["Peer Comparison", "Competitive Advantage / Moat"],
    Dataset.MARKET_PRICES_EOD: ["Technical Analysis", "Valuation"],
    Dataset.ANNUAL_REPORTS: [
        "Management & Governance",
        "Business & Company Analysis",
        "Future Outlook & Earnings",
    ],
    Dataset.QUARTERLY_RESULT_DOCS: ["Quarterly Analysis", "Future Outlook & Earnings"],
    Dataset.INVESTOR_PRESENTATIONS: ["Future Outlook & Earnings", "Management & Governance"],
    Dataset.EARNINGS_TRANSCRIPTS: ["Management & Governance", "Future Outlook & Earnings"],
    Dataset.CORPORATE_ANNOUNCEMENTS: ["News & Catalysts", "Management & Governance"],
    Dataset.NEWS: ["News & Catalysts"],
}

LABELS = {
    Dataset.COMPANY_PROFILE: "Company profile",
    Dataset.INCOME_ANNUAL: "Annual income statement",
    Dataset.INCOME_QUARTERLY: "Quarterly results",
    Dataset.BALANCE_SHEET: "Balance sheet",
    Dataset.CASH_FLOW: "Cash flow statement",
    Dataset.SHAREHOLDING: "Shareholding pattern",
    Dataset.KEY_RATIOS: "Provider key ratios",
    Dataset.CORPORATE_ACTIONS: "Corporate actions",
    Dataset.COMPETITORS: "Peer / competitor information",
    Dataset.MARKET_PRICES_EOD: "Market prices (end of day)",
    Dataset.ANNUAL_REPORTS: "Annual reports",
    Dataset.QUARTERLY_RESULT_DOCS: "Quarterly result documents",
    Dataset.INVESTOR_PRESENTATIONS: "Investor presentations",
    Dataset.EARNINGS_TRANSCRIPTS: "Earnings call transcripts",
    Dataset.CORPORATE_ANNOUNCEMENTS: "Corporate announcements",
    Dataset.NEWS: "News",
}

# Datasets the report measures, in display order. Management commentary is derived (see below).
ORDER = list(LABELS)


class _Builder:
    def __init__(self, session: Session, company: Company, today: date) -> None:
        self.s, self.company, self.today = session, company, today
        self.security_ids = list(
            session.scalars(select(Security.id).where(Security.company_id == company.id))
        )

    # ---- shared helpers ----------------------------------------------------------------------

    def _sources(self, dataset: Dataset, providers: list[str]) -> list[SourceUsed]:
        out = []
        for code in sorted(set(providers)):
            ok, _ = verified_datasets(self.s, code)
            out.append(
                SourceUsed(
                    provider=code, verified=dataset.value in ok, user_supplied=code in MANUAL_CODES
                )
            )
        return out

    @staticmethod
    def _verification(sources: list[SourceUsed], conflict: bool) -> Verification:
        if not sources:
            return Verification.NOT_AVAILABLE
        if conflict:
            return Verification.CONFLICT
        if all(s.user_supplied for s in sources):
            return Verification.USER_SUPPLIED
        real = [s for s in sources if not s.user_supplied]
        if any(not s.verified for s in real):
            return Verification.UNVERIFIED_SOURCE
        return (
            Verification.VERIFIED
            if len(real) > 1 or len(sources) > len(real)
            else Verification.SINGLE_SOURCE
        )

    @staticmethod
    def _confidence(
        completeness: float | None,
        verification: Verification,
        n_sources: int,
        age_days: int | None,
        stale_after: int,
    ) -> tuple[Confidence, list[str]]:
        if verification is Verification.NOT_AVAILABLE or completeness == 0:
            return Confidence.NONE, ["no data present"]
        reasons = [f"completeness {completeness:.0%}"] if completeness is not None else []
        reasons.append(f"{n_sources} source(s)")
        if verification is Verification.CONFLICT:
            return Confidence.LOW, [*reasons, "sources disagree"]
        if verification is Verification.UNVERIFIED_SOURCE:
            return Confidence.LOW, [*reasons, "provider support not yet proven by a live retrieval"]
        if age_days is not None and age_days > stale_after:
            return Confidence.LOW, [*reasons, f"latest data is {age_days} days old"]
        if completeness is not None and completeness < 0.5:
            return Confidence.LOW, [*reasons, "less than half of the required periods"]
        if (
            completeness is not None
            and completeness >= 0.8
            and verification is Verification.VERIFIED
        ):
            return Confidence.HIGH, [*reasons, "multiple sources / verified provider"]
        return Confidence.MEDIUM, [*reasons, "single source or partial history"]

    def _make(
        self,
        dataset: Dataset,
        *,
        required: list[str],
        available: list[str],
        sources: list[SourceUsed],
        basis: str | None = None,
        latest: str | None = None,
        retrieved: datetime | None = None,
        age_days: int | None = None,
        conflict: bool = False,
        limitations: list[str] | None = None,
        stale_after: int = 400,
        status_override: Status | None = None,
        measurable: bool = True,
    ) -> DatasetCoverage:
        missing = [p for p in required if p not in available]
        completeness = (
            (len([p for p in required if p in available]) / len(required))
            if required and measurable
            else None
        )
        if status_override:
            status = status_override
        elif not available:
            status = Status.MISSING
        else:
            status = Status.COMPLETE if not missing else Status.PARTIAL
        verification = self._verification(sources, conflict)
        confidence, basis_reasons = self._confidence(
            completeness, verification, len(sources), age_days, stale_after
        )
        if status in (Status.NOT_SUPPORTED, Status.ON_DEMAND):
            confidence, basis_reasons = Confidence.NONE, ["not stored; no assessment possible"]
        return DatasetCoverage(
            dataset=dataset.value,
            label=LABELS[dataset],
            basis=basis,
            status=status,
            required_periods=required,
            available_periods=available,
            missing_periods=[] if status in (Status.NOT_SUPPORTED, Status.ON_DEMAND) else missing,
            completeness=round(completeness, 3) if completeness is not None else None,
            sources=sources,
            latest_period=latest,
            retrieved_at=retrieved,
            age_days=age_days,
            verification=verification,
            confidence=confidence,
            confidence_basis=basis_reasons,
            limitations=limitations or [],
            affected_pillars=PILLARS.get(dataset, []) if status is not Status.COMPLETE else [],
        )

    # ---- financial statements ----------------------------------------------------------------

    def statement(
        self, dataset: Dataset, statement: Statement, period_type: PeriodType
    ) -> DatasetCoverage:
        annual = period_type is PeriodType.ANNUAL
        end = latest_expected_annual(self.today) if annual else latest_expected_quarter(self.today)
        required_dates = (
            previous_annual(end, ANNUAL_YEARS) if annual else previous_quarters(end, QUARTERS)
        )
        fmt = fiscal_year_label if annual else quarter_label

        facts = self.s.execute(
            select(
                FinancialFact.basis,
                FinancialFact.period_end,
                FinancialFact.provider_code,
                FinancialFact.retrieved_at,
                FinancialFact.line_item,
                FinancialFact.value_inr,
            ).where(
                FinancialFact.company_id == self.company.id,
                FinancialFact.statement == statement,
                FinancialFact.period_type == period_type,
            )
        ).all()
        required = [fmt(d) for d in required_dates]
        if not facts:
            return self._make(
                dataset,
                required=required,
                available=[],
                sources=[],
                limitations=["no data retrieved for this dataset"],
            )
        # One basis is evaluated at a time (they are never mixed): consolidated if present.
        bases = {f.basis for f in facts}
        basis = Basis.CONSOLIDATED if Basis.CONSOLIDATED in bases else Basis.STANDALONE
        chosen = [f for f in facts if f.basis == basis]
        have = {f.period_end for f in chosen}
        # A period that has been published counts even if it is not "due" yet by our deadline rule.
        newest = max((d for d in have if d <= self.today), default=None)
        if newest is not None and newest > end:
            end = newest
            required_dates = (
                previous_annual(end, ANNUAL_YEARS) if annual else previous_quarters(end, QUARTERS)
            )
            required = [fmt(d) for d in required_dates]
        available = [fmt(d) for d in required_dates if d in have]
        extra = sorted((d for d in have if d not in required_dates), reverse=True)
        providers = [f.provider_code for f in chosen]
        latest = max(have)
        limitations = []
        if Basis.STANDALONE in bases and Basis.CONSOLIDATED in bases:
            limitations.append(
                "both consolidated and standalone data exist; they are kept separate"
            )
        elif basis is Basis.STANDALONE:
            limitations.append("only standalone statements are available; no consolidated view")
        if extra:
            limitations.append(
                f"{len(extra)} older period(s) beyond the required window are also stored"
            )
        return self._make(
            dataset,
            required=required,
            available=available,
            sources=self._sources(dataset, providers),
            basis=basis.value,
            latest=fmt(latest),
            retrieved=max(f.retrieved_at for f in chosen),
            age_days=(self.today - latest).days,
            conflict=self._conflict(chosen),
            limitations=limitations,
            stale_after=550 if annual else 200,
        )

    @staticmethod
    def _conflict(facts: list) -> bool:
        """True if two providers report different values for the same line and period."""
        seen: dict[tuple, tuple[str, Decimal]] = {}
        for f in facts:
            if f.value_inr is None:
                continue
            key = (f.line_item.lower(), f.period_end)
            if key in seen and seen[key][0] != f.provider_code:
                other = seen[key][1]
                scale = max(abs(other), abs(f.value_inr), Decimal(1))
                if abs(other - f.value_inr) / scale > CONFLICT_TOLERANCE:
                    return True
            seen.setdefault(key, (f.provider_code, f.value_inr))
        return False

    # ---- other structured datasets -----------------------------------------------------------

    def shareholding(self) -> DatasetCoverage:
        end = latest_expected_quarter(self.today)
        rows = self.s.execute(
            select(
                ShareholdingFact.period_end,
                ShareholdingFact.provider_code,
                ShareholdingFact.retrieved_at,
            ).where(ShareholdingFact.company_id == self.company.id)
        ).all()
        have = {r.period_end for r in rows}
        newest = max((d for d in have if d <= self.today), default=None)
        if newest is not None and newest > end:
            end = newest  # already published, so it counts even if not yet "due"
        dates = previous_quarters(end, QUARTERS)
        required = [quarter_label(d) for d in dates]
        if not rows:
            return self._make(
                Dataset.SHAREHOLDING,
                required=required,
                available=[],
                sources=[],
                limitations=["no shareholding data retrieved"],
            )
        latest = max(have)
        return self._make(
            Dataset.SHAREHOLDING,
            required=required,
            available=[quarter_label(d) for d in dates if d in have],
            sources=self._sources(Dataset.SHAREHOLDING, [r.provider_code for r in rows]),
            latest=quarter_label(latest) if latest.month in (3, 6, 9, 12) else latest.isoformat(),
            retrieved=max(r.retrieved_at for r in rows),
            age_days=(self.today - latest).days,
            stale_after=200,
        )

    def prices(self) -> DatasetCoverage:
        since = self.today - timedelta(days=365)
        rows = self.s.execute(
            select(
                func.count(func.distinct(EodPrice.trade_date)),
                func.min(EodPrice.trade_date),
                func.max(EodPrice.trade_date),
                func.max(EodPrice.retrieved_at),
            ).where(
                EodPrice.security_id.in_(self.security_ids or [-1]), EodPrice.trade_date >= since
            )
        ).one()
        count, first, last, retrieved = rows
        required = [f"about {PRICE_DAYS_REQUIRED} trading days in the last 12 months"]
        limitations = [
            "prices are NOT adjusted for splits, bonuses or dividends (corporate actions pending)"
        ]
        if not count:
            return self._make(
                Dataset.MARKET_PRICES_EOD,
                required=required,
                available=[],
                sources=[],
                limitations=["no price history stored for this company"],
            )
        age = (self.today - last).days
        if age > PRICE_STALE_DAYS:
            limitations.append(f"latest price is {age} days old; do not present it as current")
        exchanges = self.s.execute(
            select(func.distinct(Security.exchange_id)).where(Security.id.in_(self.security_ids))
        ).all()
        completeness_ok = count >= PRICE_DAYS_REQUIRED * 0.98
        return self._make(
            Dataset.MARKET_PRICES_EOD,
            required=required,
            available=required if completeness_ok else [],
            sources=self._sources(Dataset.MARKET_PRICES_EOD, ["MARKET_DATA"]),
            latest=last.isoformat(),
            retrieved=retrieved,
            age_days=age,
            limitations=[
                f"{count} trading days stored ({first} to {last}) across {len(exchanges)} listing(s)",
                *limitations,
            ],
            stale_after=PRICE_STALE_DAYS,
        ).model_copy(
            update={
                "completeness": round(min(1.0, count / PRICE_DAYS_REQUIRED), 3),
                "missing_periods": []
                if completeness_ok
                else [f"about {max(0, PRICE_DAYS_REQUIRED - count)} trading days"],
                "status": Status.COMPLETE if completeness_ok else Status.PARTIAL,
            }
        )

    def corporate_actions(self) -> DatasetCoverage:
        rows = self.s.execute(
            select(
                CorporateAction.provider_code,
                CorporateAction.retrieved_at,
                CorporateAction.effective_date,
            ).where(CorporateAction.company_id == self.company.id)
        ).all()
        required = ["corporate action history (dividends, bonus, splits, rights)"]
        if not rows:
            return self._make(
                Dataset.CORPORATE_ACTIONS,
                required=required,
                available=[],
                sources=[],
                limitations=[
                    "cannot distinguish 'no actions' from 'not retrieved'; per-share history cannot be adjusted"
                ],
            )
        dates = [r.effective_date for r in rows if r.effective_date]
        return self._make(
            Dataset.CORPORATE_ACTIONS,
            required=required,
            available=required,
            sources=self._sources(Dataset.CORPORATE_ACTIONS, [r.provider_code for r in rows]),
            latest=max(dates).isoformat() if dates else None,
            retrieved=max(r.retrieved_at for r in rows),
            age_days=(self.today - max(dates)).days if dates else None,
            stale_after=800,
        )

    def key_ratios(self) -> DatasetCoverage:
        row = self.s.execute(
            select(
                func.max(RatioSnapshot.retrieved_at),
                func.array_agg(func.distinct(RatioSnapshot.provider_code)),
            ).where(RatioSnapshot.company_id == self.company.id)
        ).one()
        required = ["ratio snapshot no older than 30 days"]
        if row[0] is None:
            return self._make(
                Dataset.KEY_RATIOS,
                required=required,
                available=[],
                sources=[],
                limitations=["no provider ratios retrieved"],
            )
        age = (datetime.now(UTC) - row[0]).days
        return self._make(
            Dataset.KEY_RATIOS,
            required=required,
            available=required if age <= 30 else [],
            sources=self._sources(Dataset.KEY_RATIOS, list(row[1])),
            retrieved=row[0],
            age_days=age,
            latest=row[0].date().isoformat(),
            stale_after=30,
            limitations=[
                "ratios use the provider's own definitions; EquityOS recomputes its own from statements"
            ],
        )

    def raw_only(self, dataset: Dataset, what: str) -> DatasetCoverage:
        """Datasets kept as an archived provider response only (profile, competitors)."""
        row = self.s.execute(
            select(
                func.max(RawPayload.retrieved_at),
                func.array_agg(func.distinct(RawPayload.provider_code)),
            ).where(RawPayload.company_id == self.company.id, RawPayload.dataset == dataset.value)
        ).one()
        required = [what]
        if row[0] is None:
            return self._make(
                dataset,
                required=required,
                available=[],
                sources=[],
                limitations=[f"{what} not retrieved"],
            )
        return self._make(
            dataset,
            required=required,
            available=required,
            sources=self._sources(dataset, list(row[1])),
            retrieved=row[0],
            age_days=(datetime.now(UTC) - row[0]).days,
            latest=row[0].date().isoformat(),
            stale_after=180,
        )

    # ---- documents ---------------------------------------------------------------------------

    def documents(self, dataset: Dataset, doc_type: str, years: bool) -> DatasetCoverage:
        docs = self.s.execute(
            select(Document.financial_period, Document.url, Document.retrieved_at).where(
                Document.company_id == self.company.id, Document.doc_type == doc_type
            )
        ).all()
        have = {d.financial_period for d in docs if d.financial_period}
        # Required window: ends at the latest period that is due OR that we already hold.
        fmt = fiscal_year_label if years else quarter_label
        end = latest_expected_annual(self.today) if years else latest_expected_quarter(self.today)
        recent = previous_annual(end, 12) if years else previous_quarters(end, 12)
        probe = (
            previous_annual(date(self.today.year, 3, 31), 12)
            if years
            else previous_quarters(date(self.today.year + 1, 3, 31), 16)
        )
        held = [d for d in probe if fmt(d) in have and d <= self.today + timedelta(days=120)]
        if held and max(held) > end:
            end = max(held)
        count = DOC_YEARS if years else DOC_QUARTERS
        labels = [
            fmt(d)
            for d in (previous_annual(end, count) if years else previous_quarters(end, count))
        ]
        del recent
        unlabeled = sum(1 for d in docs if not d.financial_period)
        candidates = self.s.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(RawPayload.company_id == self.company.id, RawPayload.dataset == dataset.value)
        )
        limitations = []
        if have:
            limitations.append(
                "document periods were set at ingestion; those taken from link text are inferred"
            )
        if unlabeled:
            limitations.append(
                f"{unlabeled} stored document(s) have no financial period set and cannot be matched"
            )
        if candidates:
            limitations.append(
                "candidate links were found on the company website but are not downloaded"
            )
        origin = [
            ("MANUAL_UPLOAD" if d.url.startswith("manual-upload://") else "COMPANY_IR")
            for d in docs
        ]
        return self._make(
            dataset,
            required=labels,
            available=[p for p in labels if p in have],
            sources=self._sources(dataset, origin),
            retrieved=max((d.retrieved_at for d in docs), default=None),
            limitations=limitations or (["no documents stored"] if not docs else []),
            stale_after=10_000,
        )

    def announcements(self) -> DatasetCoverage:
        count = self.s.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                Document.company_id == self.company.id,
                Document.doc_type == "corporate_announcement",
            )
        )
        note = (
            "Exchange announcement and filing APIs are protected; EquityOS does not bypass them. "
            "Only announcements supplied manually (ingest-document --type corporate_announcement) "
            "are available, and they are not checked for completeness."
        )
        if count:
            return self._make(
                Dataset.CORPORATE_ANNOUNCEMENTS,
                required=["announcements (no complete list exists to measure against)"],
                available=[f"{count} manually supplied announcement document(s)"],
                sources=self._sources(Dataset.CORPORATE_ANNOUNCEMENTS, ["MANUAL_UPLOAD"]),
                status_override=Status.PARTIAL,
                measurable=False,
                limitations=[note],
            ).model_copy(update={"missing_periods": []})
        return self._make(
            Dataset.CORPORATE_ANNOUNCEMENTS,
            required=["exchange announcements"],
            available=[],
            sources=[],
            status_override=Status.NOT_SUPPORTED,
            measurable=False,
            limitations=[note],
        )

    def news(self) -> DatasetCoverage:
        return self._make(
            Dataset.NEWS,
            required=["recent headlines"],
            available=[],
            sources=[],
            status_override=Status.ON_DEMAND,
            measurable=False,
            limitations=[
                "retrieved at research time from permitted publisher feeds; recent window only, "
                "headlines are leads, not proof"
            ],
        )


def build_coverage(
    session: Session, company_id: uuid.UUID, today: date | None = None
) -> CoverageReport:
    company = session.get(Company, company_id)
    if company is None:
        raise LookupError(f"Company {company_id} not found")
    today = today or datetime.now(UTC).date()
    b = _Builder(session, company, today)
    datasets = [
        b.raw_only(Dataset.COMPANY_PROFILE, "company profile"),
        b.statement(Dataset.INCOME_ANNUAL, Statement.INCOME, PeriodType.ANNUAL),
        b.statement(Dataset.INCOME_QUARTERLY, Statement.INCOME, PeriodType.QUARTERLY),
        b.statement(Dataset.BALANCE_SHEET, Statement.BALANCE_SHEET, PeriodType.ANNUAL),
        b.statement(Dataset.CASH_FLOW, Statement.CASH_FLOW, PeriodType.ANNUAL),
        b.shareholding(),
        b.key_ratios(),
        b.corporate_actions(),
        b.raw_only(Dataset.COMPETITORS, "competitor list"),
        b.prices(),
        b.documents(Dataset.ANNUAL_REPORTS, "annual_report", years=True),
        b.documents(Dataset.QUARTERLY_RESULT_DOCS, "quarterly_result", years=False),
        b.documents(Dataset.INVESTOR_PRESENTATIONS, "investor_presentation", years=False),
        b.documents(Dataset.EARNINGS_TRANSCRIPTS, "earnings_transcript", years=False),
        b.announcements(),
        b.news(),
    ]
    measurable = [d.completeness for d in datasets if d.completeness is not None]
    pillars = sorted(
        {
            p
            for d in datasets
            if d.status in (Status.MISSING, Status.PARTIAL, Status.NOT_SUPPORTED)
            for p in d.affected_pillars
        }
    )
    return CoverageReport(
        company_id=company.id,
        company_name=company.common_name,
        as_of=today,
        datasets=datasets,
        overall_completeness=round(sum(measurable) / len(measurable), 3) if measurable else None,
        datasets_missing=[d.dataset for d in datasets if d.status is Status.MISSING],
        datasets_not_supported=[d.dataset for d in datasets if d.status is Status.NOT_SUPPORTED],
        affected_pillars=pillars,
        can_proceed=True,  # research continues with partial coverage; affected conclusions are flagged
        methodology=METHODOLOGY,
    )
