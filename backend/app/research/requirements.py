"""The data-requirements registry: what each kind of company needs before a report can be written.

Everything here is CURATED by the project (never produced by an LLM) and versioned. A company is
never asked for something that does not apply to its kind of business: a bank is never required to
have EBITDA or debt/equity, and an IT company is not required to report vehicle volumes.

Importance (what a missing item does):

* CRITICAL   material to the financial, valuation or sector conclusion. While one is missing the
             report is not "complete" and the user is asked for the document that supplies it.
* IMPORTANT  improves depth; the report can be written, with the gap disclosed.
* OPTIONAL   useful context; its absence is disclosed in the limitations only.

Each requirement says WHICH evidence satisfies it (any one of `metrics`), how recent it must be,
which
accounting basis is acceptable, which report sections depend on it, why it matters, which official
document normally contains it, and whether an uploaded document can currently resolve it
(`extractor`): where nothing in the system can read the figure out of a document yet, the registry
says so instead of asking the user for a file that would change nothing.
"""

from dataclasses import dataclass, field

VERSION = "1.0"

CRITICAL, IMPORTANT, OPTIONAL = "CRITICAL", "IMPORTANT", "OPTIONAL"
IMPORTANCE_ORDER = (CRITICAL, IMPORTANT, OPTIONAL)
IMPORTANCE_WEIGHT = {CRITICAL: 3.0, IMPORTANT: 2.0, OPTIONAL: 1.0}

# Report sections (the same structure in every report mode).
SECTIONS: tuple[tuple[str, str], ...] = (
    ("overview", "Company overview"),
    ("financial_performance", "Financial performance"),
    ("profitability", "Profitability and returns"),
    ("balance_sheet", "Balance sheet and financial health"),
    ("cash_flow", "Cash flow"),
    ("valuation", "Valuation"),
    ("sector_analysis", "Sector-specific analysis"),
    ("peer_comparison", "Peer comparison"),
    ("historical", "Historical performance"),
    ("segments", "Business segments"),
    ("risks_management", "Risks and management commentary"),
    ("scorecard", "Scorecard"),
)
SECTION_TITLES = dict(SECTIONS)

# Pillars (the readiness engine's own grouping; not the scoring pillars)
FINANCIALS, VALUATION, SECTOR, MARKET, NARRATIVE = (
    "FINANCIALS",
    "VALUATION",
    "SECTOR_SPECIFIC",
    "MARKET",
    "NARRATIVE",
)

# How a document is turned into evidence today
STATEMENT_TABLES = "STATEMENT_TABLES"  # primary statements (income, balance sheet, cash flow)
RATIO_TABLES = "RATIO_TABLES"  # "key ratios" / "analytical ratios" tables
SEGMENT_TABLES = "SEGMENT_TABLES"
NARRATIVE_SECTION = "NARRATIVE_SECTION"
MARKET_DATA = "MARKET_DATA"  # prices come from the exchange files / market provider, not uploads
DERIVED = "DERIVED"  # calculated from other evidence by the calculation engine
OPERATING_TEXT = "OPERATING_TEXT"  # stated in a sentence with its period (docintel/operating.py)
NONE = "NONE"  # no extractor exists: the figure cannot be machine-read from a document yet

ANNUAL_AGE = 548  # days; same staleness bound as the absolute-scoring gate
QUARTER_AGE = 240
PRICE_AGE = 10


@dataclass(frozen=True)
class Requirement:
    id: str
    label: str
    importance: str
    metrics: tuple[str, ...]  # any ONE of these evidence metrics satisfies the requirement
    sections: tuple[str, ...]  # report sections this is essential for
    why: str
    affects: str  # the calculation / score / section this feeds
    document: str  # human description of the document that normally contains it
    doc_types: tuple[str, ...] = ("annual_report", "quarterly_result")
    pillar: str = FINANCIALS
    period: str = "ANNUAL"  # ANNUAL | QUARTERLY | LATEST | PRICE | NARRATIVE
    max_age_days: int = ANNUAL_AGE
    basis: str = "CONSOLIDATED_PREFERRED"  # CONSOLIDATED_PREFERRED | STANDALONE_OK | NA
    extractor: str = STATEMENT_TABLES
    inputs: tuple[str, ...] = ()  # evidence a derived metric is built from (named when missing)
    alternative: str | None = None  # a permitted alternative source, if any
    official_source: str = "company investor-relations page, or the BSE/NSE filing"

    @property
    def upload_helps(self) -> bool:
        return self.extractor not in (NONE, MARKET_DATA)


# ---------------------------------------------------------------------------- building blocks


def R(**kw) -> Requirement:  # noqa: N802 - short constructor for the tables below
    return Requirement(**kw)


_ANNUAL_RESULTS = "audited annual financial statements (annual report) or the annual results filing"
_QUARTER_RESULTS = "latest quarterly financial results filing"
_PRESENTATION = "results press release / investor presentation"
_FS = ("financial_performance",)


def _income_core() -> list[Requirement]:
    return [
        R(
            id="revenue",
            label="Revenue from operations",
            importance=CRITICAL,
            metrics=("INCOME.revenue_from_operations", "INCOME.total_income"),
            sections=("financial_performance", "historical", "scorecard"),
            why="Every growth, margin and valuation ratio starts from the top line.",
            affects="revenue growth, margins, price/sales, the growth pillar of the score",
            document=_ANNUAL_RESULTS,
        ),
        R(
            id="net_profit",
            label="Net profit",
            importance=CRITICAL,
            metrics=("INCOME.net_profit", "INCOME.profit_after_tax", "INCOME.attributable_profit"),
            sections=("financial_performance", "profitability", "valuation", "scorecard"),
            why="Earnings drive P/E, returns on capital and every profitability conclusion.",
            affects="net margin, P/E, ROE, EPS growth",
            document=_ANNUAL_RESULTS,
        ),
        R(
            id="revenue_growth",
            label="Revenue growth (year on year)",
            importance=CRITICAL,
            metrics=("GROWTH.revenue_yoy", "GROWTH.total_income_yoy"),
            sections=("financial_performance", "historical", "scorecard"),
            why="Growth cannot be judged from a single year; two comparable years are needed.",
            affects="growth pillar, historical analysis",
            document="financial statements for the latest two financial years",
            extractor=DERIVED,
            inputs=("INCOME.revenue_from_operations",),
        ),
        R(
            id="profit_growth",
            label="Net profit growth (year on year)",
            importance=IMPORTANT,
            metrics=("GROWTH.net_profit_yoy",),
            sections=("financial_performance", "historical", "scorecard"),
            why="Shows whether earnings growth follows revenue growth.",
            affects="growth pillar, historical analysis",
            document="financial statements for the latest two financial years",
            extractor=DERIVED,
            inputs=("INCOME.net_profit",),
        ),
        R(
            id="latest_quarter",
            label="Latest quarterly results",
            importance=IMPORTANT,
            metrics=("INCOME.revenue_from_operations", "INCOME.total_income", "INCOME.net_profit"),
            sections=("financial_performance", "historical"),
            why="Annual figures can be a year old; the latest quarter shows the current run-rate.",
            affects="trailing-twelve-month figures, current valuation multiples",
            document=_QUARTER_RESULTS,
            doc_types=("quarterly_result",),
            period="QUARTERLY",
            max_age_days=QUARTER_AGE,
        ),
    ]


def _balance_core() -> list[Requirement]:
    return [
        R(
            id="total_assets",
            label="Total assets",
            importance=CRITICAL,
            metrics=("BALANCE_SHEET.total_assets", "BALANCE_SHEET.total_equity_and_liabilities"),
            sections=("balance_sheet", "scorecard"),
            why="Needed for asset turnover, returns on assets and any leverage ratio.",
            affects="asset turnover, ROA, balance-sheet strength",
            document=_ANNUAL_RESULTS,
        ),
        R(
            id="total_equity",
            label="Shareholders' equity",
            importance=CRITICAL,
            metrics=("BALANCE_SHEET.total_equity", "BALANCE.owners_equity"),
            sections=("balance_sheet", "valuation", "scorecard"),
            why="Book value per share, ROE and P/B all need equity.",
            affects="ROE, P/B, equity multiplier",
            document=_ANNUAL_RESULTS,
        ),
    ]


def _price_block(bank: bool) -> list[Requirement]:
    return [
        R(
            id="price",
            label="Recent share price",
            importance=CRITICAL,
            metrics=("PRICE.close",),
            sections=("valuation", "scorecard"),
            why="Every valuation multiple compares a price with earnings or book value.",
            affects="P/E, P/B, market capitalisation, momentum",
            document="exchange end-of-day (bhavcopy) data, refreshed by EquityOS",
            pillar=MARKET,
            period="PRICE",
            max_age_days=PRICE_AGE,
            basis="NA",
            extractor=MARKET_DATA,
        ),
        R(
            id="market_cap",
            label="Market capitalisation",
            importance=IMPORTANT if bank else CRITICAL,
            metrics=("VALUATION.market_cap",),
            sections=("valuation",),
            why="Size and the equity value that valuation is expressed against.",
            affects="valuation section, size comparison against peers",
            document="shares outstanding from the balance sheet / shareholding pattern + price",
            pillar=VALUATION,
            period="PRICE",
            max_age_days=PRICE_AGE,
            extractor=DERIVED,
            inputs=("BALANCE.shares_outstanding", "PRICE.close"),
            basis="NA",
        ),
        R(
            id="pe",
            label="Price-to-earnings (P/E)",
            importance=IMPORTANT if bank else CRITICAL,
            metrics=("VALUATION.pe_ttm", "VALUATION.pe_annual"),
            sections=("valuation", "scorecard"),
            why="The most common earnings-based valuation measure.",
            affects="valuation pillar, valuation section",
            document="share price + net profit and EPS from the results",
            pillar=VALUATION,
            period="PRICE",
            max_age_days=PRICE_AGE,
            extractor=DERIVED,
            inputs=("TTM.eps_basic", "PRICE.close"),
            basis="NA",
        ),
        R(
            id="pb",
            label="Price-to-book (P/B)",
            importance=CRITICAL if bank else IMPORTANT,
            metrics=("VALUATION.price_to_book",),
            sections=("valuation", "scorecard"),
            why="The standard valuation anchor for banks and other balance-sheet businesses.",
            affects="valuation pillar, valuation section",
            document="share price + shareholders' equity and shares outstanding",
            pillar=VALUATION,
            period="PRICE",
            max_age_days=PRICE_AGE,
            extractor=DERIVED,
            inputs=("BALANCE.owners_equity", "BALANCE.shares_outstanding"),
            basis="NA",
        ),
    ]


def _narrative_block() -> list[Requirement]:
    return [
        R(
            id="management_commentary",
            label="Management discussion and analysis",
            importance=IMPORTANT,
            metrics=("SECTION.management_discussion",),
            sections=("risks_management",),
            why="The only first-hand statement of management's view of risks and outlook.",
            affects="risks and management commentary section",
            document="annual report (Management Discussion and Analysis section)",
            doc_types=("annual_report",),
            pillar=NARRATIVE,
            period="NARRATIVE",
            basis="NA",
            extractor=NARRATIVE_SECTION,
        ),
        R(
            id="auditor_report",
            label="Auditor's report",
            importance=OPTIONAL,
            metrics=("SECTION.auditor_report",),
            sections=("risks_management",),
            why="Qualifications, emphasis-of-matter and going-concern paragraphs are risk signals.",
            affects="risk flags",
            document="annual report (Independent Auditor's Report)",
            doc_types=("annual_report",),
            pillar=NARRATIVE,
            period="NARRATIVE",
            basis="NA",
            extractor=NARRATIVE_SECTION,
        ),
        R(
            id="shareholding",
            label="Shareholding pattern",
            importance=OPTIONAL,
            metrics=("SHAREHOLDING.promoters.percent", "SHAREHOLDING.total.percent"),
            sections=("overview",),
            why="Promoter and institutional holding indicate who controls the business.",
            affects="ownership context",
            document="shareholding pattern filing (quarterly)",
            doc_types=("quarterly_result", "annual_report"),
            pillar=NARRATIVE,
            period="LATEST",
            max_age_days=420,
            basis="NA",
            extractor=STATEMENT_TABLES,
        ),
    ]


def _nonfin_common(
    *,
    ebitda: str = CRITICAL,
    roce: str = IMPORTANT,
    debt: str = IMPORTANT,
    cover: str = IMPORTANT,
    cfo: str = IMPORTANT,
    capex: str = OPTIONAL,
    fcf: str = OPTIONAL,
    operating_label: str = "EBITDA margin",
    operating_metrics: tuple[str, ...] = ("MARGIN.ebitda_margin",),
) -> list[Requirement]:
    return [
        R(
            id="operating_margin",
            label=operating_label,
            importance=ebitda,
            metrics=operating_metrics,
            sections=("profitability", "scorecard"),
            why="Shows how much of each rupee of sales is left after operating costs.",
            affects="profitability pillar, margin trend",
            document=_ANNUAL_RESULTS + " (depreciation and finance costs are required)",
            extractor=DERIVED,
            inputs=("INCOME.depreciation_and_amortisation", "INCOME.finance_costs"),
        ),
        R(
            id="roce",
            label="Return on capital employed",
            importance=roce,
            metrics=("RETURN.roce",),
            sections=("profitability", "scorecard"),
            why="Whether the business earns more than its cost of capital.",
            affects="profitability pillar, efficiency",
            document=_ANNUAL_RESULTS + " (borrowings and equity are required)",
            extractor=DERIVED,
            inputs=("BALANCE_SHEET.borrowings", "INCOME.finance_costs"),
        ),
        R(
            id="roe",
            label="Return on equity",
            importance=IMPORTANT,
            metrics=("RETURN.roe_closing", "RETURN.roe_average"),
            sections=("profitability", "scorecard"),
            why="The return shareholders earn on their capital.",
            affects="profitability pillar",
            document=_ANNUAL_RESULTS,
            extractor=DERIVED,
            inputs=("INCOME.net_profit", "BALANCE_SHEET.total_equity"),
        ),
        R(
            id="debt_to_equity",
            label="Debt to equity",
            importance=debt,
            metrics=("LEVERAGE.debt_to_equity",),
            sections=("balance_sheet", "scorecard"),
            why="The main measure of financial risk for a non-financial company.",
            affects="financial-health pillar, risk section",
            document=_ANNUAL_RESULTS + " (borrowings schedule on the balance sheet)",
            extractor=DERIVED,
            inputs=("BALANCE_SHEET.borrowings",),
        ),
        R(
            id="interest_cover",
            label="Interest coverage",
            importance=cover,
            metrics=("RATIO.interest_cover",),
            sections=("balance_sheet", "scorecard"),
            why="Whether operating profit comfortably covers the interest bill.",
            affects="financial-health pillar",
            document=_ANNUAL_RESULTS + " (finance costs and depreciation are required)",
            extractor=DERIVED,
            inputs=("INCOME.finance_costs", "INCOME.depreciation_and_amortisation"),
        ),
        R(
            id="operating_cash_flow",
            label="Cash flow from operations",
            importance=cfo,
            metrics=("CASH_FLOW.net_cash_from_operating_activities", "CASH_FLOW.summary.operating"),
            sections=("cash_flow", "scorecard"),
            why="Profit that is not backed by cash is a red flag; this tests earnings quality.",
            affects="cash conversion, free cash flow, financial health",
            document="cash-flow statement in the annual report",
            doc_types=("annual_report",),
        ),
        R(
            id="capex",
            label="Capital expenditure",
            importance=capex,
            metrics=("CASH_FLOW.capital_expenditure",),
            sections=("cash_flow",),
            why="Shows how much the business reinvests, and what is left as free cash flow.",
            affects="free cash flow",
            document="cash-flow statement in the annual report",
            doc_types=("annual_report",),
        ),
        R(
            id="free_cash_flow",
            label="Free cash flow",
            importance=fcf,
            metrics=("CASH.free_cash_flow",),
            sections=("cash_flow",),
            why="Cash left after investment: what can fund dividends, buybacks or debt repayment.",
            affects="cash-flow section",
            document="cash-flow statement in the annual report",
            doc_types=("annual_report",),
            extractor=DERIVED,
            inputs=(
                "CASH_FLOW.net_cash_from_operating_activities",
                "CASH_FLOW.capital_expenditure",
            ),
        ),
    ]


def _segment_reqs(level: str, ebitda_level: str) -> list[Requirement]:
    return [
        R(
            id="segment_revenue",
            label="Segment revenue",
            importance=level,
            metrics=("SEGMENT.segment_revenue", "SEGMENT.segment_revenue_total"),
            sections=("segments", "sector_analysis"),
            why="Shows which businesses generate the revenue and how concentrated it is.",
            affects="segment analysis, sum-of-the-parts valuation",
            document="segment reporting note of the annual report or quarterly results",
            extractor=SEGMENT_TABLES,
        ),
        R(
            id="segment_result",
            label="Segment profit (EBIT/EBITDA)",
            importance=ebitda_level,
            metrics=(
                "SEGMENT.segment_result_ebitda",
                "SEGMENT.segment_result_ebit",
                "SEGMENT.segment_result",
            ),
            sections=("segments", "sector_analysis"),
            why="Segment profit shows where the earnings come from; revenue alone does not.",
            affects="segment analysis, sum-of-the-parts valuation",
            document="segment reporting note of the annual report or quarterly results",
            extractor=SEGMENT_TABLES,
        ),
    ]


def _volume(
    label: str, importance: str, rid: str, why: str, *, extractor: str = OPERATING_TEXT
) -> Requirement:
    return R(
        id=rid,
        label=label,
        importance=importance,
        metrics=(f"OPERATING.{rid}",),
        sections=("sector_analysis",),
        why=why,
        affects="sector-specific analysis (not part of the financial score)",
        document="results press release, monthly sales release, fact sheet or annual report",
        doc_types=(
            "investor_presentation",
            "earnings_transcript",
            "quarterly_result",
            "annual_report",
        ),
        pillar=SECTOR,
        period="LATEST",
        max_age_days=ANNUAL_AGE,
        basis="NA",
        extractor=extractor,
    )


# ---------------------------------------------------------------------------- the profiles


def _bank() -> list[Requirement]:
    a = _ANNUAL_RESULTS
    ratio_doc = "bank results press release / investor presentation / Pillar-3 disclosures"
    return [
        R(
            id="total_income",
            label="Total income",
            importance=CRITICAL,
            metrics=("INCOME.total_income", "INCOME.interest_earned"),
            sections=("financial_performance", "historical", "scorecard"),
            why="The top line of a bank is interest earned plus other income.",
            affects="income growth, cost-to-income, scorecard",
            document=a,
            basis="STANDALONE_OK",
        ),
        R(
            id="interest_earned",
            label="Interest earned",
            importance=CRITICAL,
            metrics=("INCOME.interest_earned",),
            sections=("financial_performance", "sector_analysis"),
            why="Interest earned minus interest expended is net interest income, the core earning.",
            affects="net interest income, net interest margin",
            document="bank-format profit and loss account",
            basis="STANDALONE_OK",
        ),
        R(
            id="interest_expended",
            label="Interest expended",
            importance=CRITICAL,
            metrics=("INCOME.interest_expended",),
            sections=("financial_performance", "sector_analysis"),
            why="The cost of deposits and borrowings: the other half of net interest income.",
            affects="net interest income, net interest margin",
            document="bank-format profit and loss account",
            basis="STANDALONE_OK",
        ),
        R(
            id="net_interest_income",
            label="Net interest income",
            importance=CRITICAL,
            metrics=("INCOME.net_interest_income",),
            sections=("financial_performance", "sector_analysis", "scorecard"),
            why="The main profit engine of a bank; growth in it is the core growth signal.",
            affects="NII growth, net interest margin, growth pillar",
            document="bank-format profit and loss account",
            extractor=DERIVED,
            inputs=("INCOME.interest_earned", "INCOME.interest_expended"),
            basis="STANDALONE_OK",
        ),
        *[r for r in _income_core() if r.id in ("net_profit", "profit_growth", "latest_quarter")],
        R(
            id="advances",
            label="Advances (loans)",
            importance=CRITICAL,
            metrics=("BALANCE_SHEET.advances", "RATIOS.gross_advances"),
            period="LATEST",
            max_age_days=QUARTER_AGE,
            alternative=(
                "the gross advances a results release or presentation states (labelled gross, "
                "not the net balance-sheet figure)"
            ),
            sections=("balance_sheet", "sector_analysis", "scorecard"),
            why="The loan book is the bank's main asset; its growth and quality drive earnings.",
            affects="credit growth, credit-deposit ratio, asset quality ratios",
            document="bank-format balance sheet",
            basis="STANDALONE_OK",
        ),
        R(
            id="deposits",
            label="Deposits",
            importance=CRITICAL,
            metrics=("BALANCE_SHEET.deposits", "RATIOS.total_deposits"),
            period="LATEST",
            max_age_days=QUARTER_AGE,
            alternative="the total deposits a results release or presentation states",
            sections=("balance_sheet", "sector_analysis", "scorecard"),
            why="Deposits fund the bank; their growth and mix decide its cost of funds.",
            affects="deposit growth, credit-deposit ratio, CASA",
            document="bank-format balance sheet",
            basis="STANDALONE_OK",
        ),
        R(
            id="nim",
            label="Net interest margin (NIM)",
            importance=CRITICAL,
            metrics=("RATIOS.net_interest_margin",),
            sections=("sector_analysis", "profitability", "scorecard"),
            why="NIM is the spread a bank earns on its assets: its key profitability ratio.",
            affects="banking profitability analysis",
            document=ratio_doc,
            doc_types=("quarterly_result", "investor_presentation", "annual_report"),
            pillar=SECTOR,
            period="LATEST",
            max_age_days=QUARTER_AGE,
            extractor=RATIO_TABLES,
            basis="NA",
            alternative=(
                "reported NIM from the results presentation; NII divided by average total "
                "assets is NOT NIM and is not accepted as a substitute"
            ),
        ),
        R(
            id="gross_npa",
            label="Gross NPA ratio",
            importance=CRITICAL,
            metrics=("RATIOS.gross_npa_ratio",),
            sections=("sector_analysis", "balance_sheet", "scorecard"),
            why="Share of loans that have stopped paying: the main measure of credit risk.",
            affects="asset-quality analysis, financial-health pillar",
            document=ratio_doc,
            doc_types=("quarterly_result", "investor_presentation", "annual_report"),
            pillar=SECTOR,
            period="LATEST",
            max_age_days=QUARTER_AGE,
            extractor=RATIO_TABLES,
            basis="NA",
        ),
        R(
            id="net_npa",
            label="Net NPA ratio",
            importance=CRITICAL,
            metrics=("RATIOS.net_npa_ratio",),
            sections=("sector_analysis", "balance_sheet", "scorecard"),
            why="Bad loans after provisions: how much real loss risk is left on the books.",
            affects="asset-quality analysis, financial-health pillar",
            document=ratio_doc,
            doc_types=("quarterly_result", "investor_presentation", "annual_report"),
            pillar=SECTOR,
            period="LATEST",
            max_age_days=QUARTER_AGE,
            extractor=RATIO_TABLES,
            basis="NA",
        ),
        R(
            id="capital_adequacy",
            label="Capital adequacy ratio",
            importance=CRITICAL,
            metrics=("RATIOS.capital_adequacy_ratio",),
            sections=("sector_analysis", "balance_sheet", "scorecard"),
            why="How much loss the bank can absorb before depositors are at risk.",
            affects="capital strength analysis, financial-health pillar",
            document=ratio_doc,
            doc_types=("quarterly_result", "investor_presentation", "annual_report"),
            pillar=SECTOR,
            period="LATEST",
            max_age_days=QUARTER_AGE,
            extractor=RATIO_TABLES,
            basis="NA",
        ),
        R(
            id="casa",
            label="CASA ratio",
            importance=IMPORTANT,
            metrics=("RATIOS.casa_ratio",),
            sections=("sector_analysis",),
            why="Current and savings deposits are the cheapest funding; a high share protects NIM.",
            affects="funding-cost analysis",
            document=ratio_doc,
            doc_types=("quarterly_result", "investor_presentation", "annual_report"),
            pillar=SECTOR,
            period="LATEST",
            max_age_days=QUARTER_AGE,
            extractor=RATIO_TABLES,
            basis="NA",
        ),
        R(
            id="cost_to_income",
            label="Cost-to-income ratio",
            importance=IMPORTANT,
            metrics=("RATIO.cost_to_income",),
            sections=("profitability", "scorecard"),
            why="How much of income the bank spends on running itself.",
            affects="profitability pillar",
            document=a,
            extractor=DERIVED,
            inputs=("INCOME.operating_expenses", "INCOME.net_interest_income"),
            basis="STANDALONE_OK",
        ),
        R(
            id="roa",
            label="Return on assets",
            importance=IMPORTANT,
            metrics=("RETURN.roa_closing", "RETURN.roa_average"),
            sections=("profitability", "scorecard"),
            why="Banks are leveraged; ROA shows profit per rupee of assets.",
            affects="profitability pillar",
            document=a,
            extractor=DERIVED,
            inputs=("INCOME.net_profit", "BALANCE_SHEET.total_assets"),
            basis="STANDALONE_OK",
        ),
        R(
            id="roe",
            label="Return on equity",
            importance=IMPORTANT,
            metrics=("RETURN.roe_closing", "RETURN.roe_average"),
            sections=("profitability", "scorecard"),
            why="The return shareholders earn on their capital.",
            affects="profitability pillar",
            document=a,
            extractor=DERIVED,
            inputs=("INCOME.net_profit", "BALANCE_SHEET.total_equity"),
            basis="STANDALONE_OK",
        ),
        R(
            id="credit_deposit",
            label="Credit-deposit ratio",
            importance=IMPORTANT,
            metrics=("RATIO.advances_to_deposits",),
            sections=("sector_analysis",),
            why="How much of its deposits the bank has lent out.",
            affects="liquidity and growth-capacity analysis",
            document="bank-format balance sheet",
            extractor=DERIVED,
            inputs=("BALANCE_SHEET.advances", "BALANCE_SHEET.deposits"),
            basis="STANDALONE_OK",
        ),
        R(
            id="provisions",
            label="Provisions relative to pre-provision profit",
            importance=IMPORTANT,
            metrics=("RATIO.provisions_to_ppop",),
            sections=("sector_analysis", "scorecard"),
            why="Shows how much of operating profit is consumed by credit costs.",
            affects="asset-quality analysis",
            document="bank-format profit and loss account",
            extractor=DERIVED,
            inputs=("INCOME.provisions_and_contingencies",),
            basis="STANDALONE_OK",
        ),
        *_balance_core(),
        *_price_block(bank=True),
        *_narrative_block(),
    ]


def _fmcg() -> list[Requirement]:
    return [
        *_income_core(),
        *_balance_core(),
        *_nonfin_common(ebitda=CRITICAL, roce=CRITICAL, debt=IMPORTANT, cfo=IMPORTANT),
        _volume(
            "Volume growth",
            OPTIONAL,
            "volume_growth",
            "Separates real demand growth from price increases.",
            extractor=NONE,
        ),
        *_price_block(bank=False),
        *_narrative_block(),
    ]


def _automobiles() -> list[Requirement]:
    return [
        *_income_core(),
        *_balance_core(),
        *_nonfin_common(
            ebitda=CRITICAL,
            roce=IMPORTANT,
            debt=CRITICAL,
            cfo=CRITICAL,
            capex=IMPORTANT,
            fcf=IMPORTANT,
        ),
        _volume(
            "Vehicle sales volumes",
            IMPORTANT,
            "vehicle_volumes",
            "Volumes show unit demand and market share, separately from price and mix.",
        ),
        *_price_block(bank=False),
        *_narrative_block(),
    ]


def _it_services() -> list[Requirement]:
    return [
        *_income_core(),
        *_balance_core(),
        *_nonfin_common(
            ebitda=CRITICAL,
            roce=IMPORTANT,
            debt=OPTIONAL,
            cover=OPTIONAL,
            cfo=IMPORTANT,
            operating_label="Operating margin (EBIT margin)",
            operating_metrics=("MARGIN.ebit_margin", "MARGIN.ebitda_margin"),
        ),
        _volume(
            "Employee attrition",
            OPTIONAL,
            "attrition",
            "A people business: attrition drives cost and delivery risk.",
        ),
        _volume(
            "Utilisation",
            OPTIONAL,
            "utilization",
            "Utilisation is the main operating lever for margins.",
        ),
        *_price_block(bank=False),
        *_narrative_block(),
    ]


def _energy() -> list[Requirement]:
    return [
        *_income_core(),
        *_balance_core(),
        *_nonfin_common(
            ebitda=CRITICAL,
            roce=IMPORTANT,
            debt=CRITICAL,
            cfo=CRITICAL,
            capex=CRITICAL,
            fcf=IMPORTANT,
        ),
        *_segment_reqs(IMPORTANT, IMPORTANT),
        *_price_block(bank=False),
        *_narrative_block(),
    ]


def _conglomerate() -> list[Requirement]:
    return [
        *_income_core(),
        *_balance_core(),
        *_nonfin_common(
            ebitda=CRITICAL,
            roce=IMPORTANT,
            debt=CRITICAL,
            cfo=CRITICAL,
            capex=IMPORTANT,
            fcf=IMPORTANT,
        ),
        *_segment_reqs(CRITICAL, CRITICAL),
        *_price_block(bank=False),
        *_narrative_block(),
    ]


def _generic() -> list[Requirement]:
    return [
        *_income_core(),
        *_balance_core(),
        *_nonfin_common(ebitda=IMPORTANT, roce=IMPORTANT, debt=IMPORTANT, cfo=IMPORTANT),
        *_price_block(bank=False),
        *_narrative_block(),
    ]


# ---------------------------------------------------------------------------- readiness thresholds


@dataclass(frozen=True)
class Thresholds:
    """When a company may be researched automatically. NOT a single percentage: every rule applies.

    * no CRITICAL requirement may be missing or stale (a hard rule, independent of the score);
    * `min_score` is a floor on the overall readiness score;
    * each component must reach `min_component` (so a very high document coverage cannot hide a
      very poor validation record);
    * `max_open_errors` validation errors on the company's evidence are tolerated (default 0).
    """

    min_score: float = 70.0
    min_component: float = 40.0
    max_open_errors: int = 0
    weights: dict = field(
        default_factory=lambda: {
            "financial_completeness": 0.25,
            "document_coverage": 0.10,
            "source_reliability": 0.10,
            "period_consistency": 0.10,
            "validation_coverage": 0.15,
            "sector_metrics": 0.15,
            "valuation_inputs": 0.15,
        }
    )


PROFILE_DEFS = {
    "BANK": ("Bank", _bank),
    "FMCG": ("FMCG", _fmcg),
    "AUTOMOBILES": ("Automobiles", _automobiles),
    "IT_SERVICES": ("IT services", _it_services),
    "ENERGY": ("Energy", _energy),
    "CONGLOMERATE": ("Diversified conglomerate", _conglomerate),
    "GENERIC_NONFIN": ("Other non-financial", _generic),
}

# Per-profile overrides: a bank's weight sits on sector metrics; an IT company has few of them.
THRESHOLDS: dict[str, Thresholds] = {
    "BANK": Thresholds(
        weights={
            "financial_completeness": 0.20,
            "document_coverage": 0.10,
            "source_reliability": 0.10,
            "period_consistency": 0.10,
            "validation_coverage": 0.15,
            "sector_metrics": 0.25,
            "valuation_inputs": 0.10,
        }
    ),
    "IT_SERVICES": Thresholds(min_score=65.0),
    "GENERIC_NONFIN": Thresholds(min_score=65.0),
}
DEFAULT_THRESHOLDS = Thresholds()


def thresholds_for(profile: str) -> Thresholds:
    return THRESHOLDS.get(profile, DEFAULT_THRESHOLDS)


def requirements_for(profile: str) -> list[Requirement]:
    if profile not in PROFILE_DEFS:
        raise KeyError(f"unknown requirement profile {profile!r}")
    reqs = PROFILE_DEFS[profile][1]()
    ids = [r.id for r in reqs]
    assert len(ids) == len(set(ids)), f"duplicate requirement ids in {profile}"
    return reqs


def profile_for(sector: str | None, industry: str | None) -> str:
    """The requirement profile of a company: Energy is split out of the scoring profile set."""
    from app.scoring import registry as scoring

    code = scoring.profile_for(sector, industry).code
    if code == "GENERIC_NONFIN" and sector == "oil_and_gas":
        return "ENERGY"
    return code
