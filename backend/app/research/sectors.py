"""Sector profiles for the research report: what to analyse, compare, monitor and warn about.

One profile per initial sector (bank, IT services, FMCG, automobiles, energy). A profile only names
evidence metrics that already exist in the evidence store (statement items, calculation-engine
ratios, disclosed ratios); it never invents a metric. If a company lacks one, the report says so.

Risk templates are SECTOR risks: they are generic to the industry, are labelled as such in the report
and are never presented as evidence about the particular company.
"""

from __future__ import annotations

from dataclasses import dataclass, field

AMOUNT, PCT, MULT, PERSHARE = "amount", "pct", "x", "per_share"


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    names: tuple[str, ...]  # evidence metric names, in priority order
    kind: str = PCT  # amount | pct | x | per_share
    better: str | None = None  # "higher" | "lower" | None (no judgement of good/bad)
    ptype: str = "ANNUAL"  # ANNUAL | QUARTERLY | ANY
    group: str = ""


@dataclass(frozen=True)
class Sector:
    code: str
    label: str
    lens: str
    trend: tuple[Metric, ...]
    quarterly: tuple[Metric, ...]
    peer: tuple[Metric, ...]
    monitor: tuple[str, ...]  # keys of metrics (trend or peer) worth watching each quarter
    risks: tuple[tuple[str, str], ...]  # (title, text): sector risk templates
    valuation: str  # BANK | EARNINGS
    cash_flow_meaningful: bool = True
    notes: tuple[str, ...] = field(default=())


# ---- shared metric definitions -------------------------------------------------------------------

INCOME = ("INCOME.total_income", "INCOME.revenue_from_operations")
REVENUE = ("INCOME.revenue_from_operations", "INCOME.total_income")
PROFIT = ("INCOME.net_profit", "INCOME.attributable_profit", "INCOME.profit_after_tax")

BANK = Sector(
    code="BANK",
    label="Banking",
    lens=(
        "A bank is read through net interest income and margin, the cost of its funding (CASA), asset "
        "quality (gross and net NPA, provisions) and capital adequacy, not through operating margin "
        "or industrial cash-flow measures."
    ),
    trend=(
        Metric("total_income", "Total income", INCOME, AMOUNT, "higher", group="Income"),
        Metric(
            "interest_earned",
            "Interest earned",
            ("INCOME.interest_earned",),
            AMOUNT,
            "higher",
            group="Income",
        ),
        Metric(
            "nii",
            "Net interest income",
            ("INCOME.net_interest_income",),
            AMOUNT,
            "higher",
            group="Income",
        ),
        Metric("net_profit", "Net profit", PROFIT, AMOUNT, "higher", group="Income"),
        Metric("eps", "EPS (basic)", ("INCOME.eps_basic",), PERSHARE, "higher", group="Income"),
        Metric(
            "advances",
            "Advances",
            ("BALANCE_SHEET.advances",),
            AMOUNT,
            "higher",
            group="Balance sheet",
        ),
        Metric(
            "deposits",
            "Deposits",
            ("BALANCE_SHEET.deposits",),
            AMOUNT,
            "higher",
            group="Balance sheet",
        ),
        Metric(
            "total_assets",
            "Total assets",
            ("BALANCE_SHEET.total_assets",),
            AMOUNT,
            None,
            group="Balance sheet",
        ),
        Metric(
            "total_equity",
            "Shareholders' equity",
            ("BALANCE_SHEET.total_equity", "BALANCE.owners_equity"),
            AMOUNT,
            "higher",
            group="Balance sheet",
        ),
        Metric(
            "cd_ratio",
            "Credit-deposit ratio",
            ("RATIO.advances_to_deposits",),
            PCT,
            None,
            group="Efficiency",
        ),
        Metric(
            "cost_to_income",
            "Cost-to-income ratio",
            ("RATIO.cost_to_income",),
            PCT,
            "lower",
            group="Efficiency",
        ),
        Metric(
            "ppop_margin",
            "Pre-provision operating margin",
            ("MARGIN.ppop_margin",),
            PCT,
            "higher",
            group="Efficiency",
        ),
        Metric(
            "provisions_to_ppop",
            "Provisions / pre-provision profit",
            ("RATIO.provisions_to_ppop",),
            PCT,
            "lower",
            group="Asset quality",
        ),
        Metric(
            "roe",
            "Return on equity",
            ("RETURN.roe_closing", "RETURN.roe_average"),
            PCT,
            "higher",
            group="Returns",
        ),
        Metric(
            "roa",
            "Return on assets",
            ("RETURN.roa_closing", "RETURN.roa_average"),
            PCT,
            "higher",
            group="Returns",
        ),
    ),  # fmt: skip
    quarterly=(
        Metric("total_income", "Total income", INCOME, AMOUNT, ptype="QUARTERLY"),
        Metric(
            "nii", "Net interest income", ("INCOME.net_interest_income",), AMOUNT, ptype="QUARTERLY"
        ),
        Metric("net_profit", "Net profit", PROFIT, AMOUNT, ptype="QUARTERLY"),
        Metric("advances", "Advances", ("BALANCE_SHEET.advances",), AMOUNT, ptype="QUARTERLY"),
        Metric("deposits", "Deposits", ("BALANCE_SHEET.deposits",), AMOUNT, ptype="QUARTERLY"),
        Metric(
            "nim", "Net interest margin", ("RATIOS.net_interest_margin",), PCT, ptype="QUARTERLY"
        ),
        Metric(
            "gnpa", "Gross NPA ratio", ("RATIOS.gross_npa_ratio",), PCT, "lower", ptype="QUARTERLY"
        ),
        Metric("nnpa", "Net NPA ratio", ("RATIOS.net_npa_ratio",), PCT, "lower", ptype="QUARTERLY"),
        Metric(
            "car",
            "Capital adequacy ratio",
            ("RATIOS.capital_adequacy_ratio",),
            PCT,
            "higher",
            ptype="QUARTERLY",
        ),
    ),  # fmt: skip
    peer=(
        Metric(
            "total_income_yoy",
            "Total income growth (YoY)",
            ("GROWTH.total_income_yoy",),
            PCT,
            "higher",
            "ANY",
            "Growth",
        ),
        Metric(
            "net_profit_yoy",
            "Net profit growth (YoY)",
            ("GROWTH.net_profit_yoy",),
            PCT,
            "higher",
            "ANY",
            "Growth",
        ),
        Metric(
            "advances_yoy",
            "Advances growth (YoY)",
            ("GROWTH.advances_yoy",),
            PCT,
            "higher",
            "ANY",
            "Growth",
        ),
        Metric(
            "deposits_yoy",
            "Deposit growth (YoY)",
            ("GROWTH.deposits_yoy",),
            PCT,
            "higher",
            "ANY",
            "Growth",
        ),
        Metric(
            "nim",
            "Net interest margin",
            ("RATIOS.net_interest_margin",),
            PCT,
            "higher",
            "ANY",
            "Profitability",
        ),
        Metric(
            "roa",
            "Return on assets",
            ("RETURN.roa_closing",),
            PCT,
            "higher",
            "ANNUAL",
            "Profitability",
        ),
        Metric(
            "roe",
            "Return on equity",
            ("RETURN.roe_closing",),
            PCT,
            "higher",
            "ANNUAL",
            "Profitability",
        ),
        Metric(
            "cost_to_income",
            "Cost-to-income ratio",
            ("RATIO.cost_to_income",),
            PCT,
            "lower",
            "ANNUAL",
            "Profitability",
        ),
        Metric(
            "gnpa",
            "Gross NPA ratio",
            ("RATIOS.gross_npa_ratio",),
            PCT,
            "lower",
            "ANY",
            "Asset quality",
        ),
        Metric(
            "nnpa", "Net NPA ratio", ("RATIOS.net_npa_ratio",), PCT, "lower", "ANY", "Asset quality"
        ),
        Metric(
            "provisions_to_ppop",
            "Provisions / pre-provision profit",
            ("RATIO.provisions_to_ppop",),
            PCT,
            "lower",
            "ANNUAL",
            "Asset quality",
        ),
        Metric(
            "car",
            "Capital adequacy ratio",
            ("RATIOS.capital_adequacy_ratio",),
            PCT,
            "higher",
            "ANY",
            "Capital",
        ),
        Metric(
            "cd_ratio",
            "Credit-deposit ratio",
            ("RATIO.advances_to_deposits",),
            PCT,
            None,
            "ANNUAL",
            "Efficiency",
        ),
        Metric(
            "pe",
            "Price-to-earnings",
            ("VALUATION.pe_annual", "VALUATION.pe_ttm"),
            MULT,
            None,
            "ANY",
            "Valuation",
        ),
        Metric("pb", "Price-to-book", ("VALUATION.price_to_book",), MULT, None, "ANY", "Valuation"),
    ),  # fmt: skip
    monitor=(
        "nim",
        "gnpa",
        "nnpa",
        "car",
        "deposits",
        "advances",
        "cost_to_income",
        "roe",
        "roa",
        "provisions_to_ppop",
    ),
    risks=(
        (
            "Asset quality",
            "Slippages or a rise in provisioning would reduce profit; credit cost is the main swing factor for lenders.",
        ),
        (
            "Interest-rate and margin pressure",
            "Falling policy rates or rising deposit costs can compress net interest margin.",
        ),
        (
            "Funding and liquidity",
            "A slowdown in low-cost deposit (CASA) growth raises funding cost and tightens the credit-deposit ratio.",
        ),
        (
            "Regulation",
            "Changes in RBI capital, provisioning or lending norms can alter returns and growth capacity.",
        ),
        (
            "Capital",
            "Growth above internal capital generation needs fresh capital, which dilutes existing holders.",
        ),
    ),  # fmt: skip
    valuation="BANK",
    cash_flow_meaningful=False,
    notes=(
        "Bank cash flow is dominated by changes in deposits and advances, so free cash flow and cash "
        "conversion are not meaningful and are not reported.",
    ),
)


def _nonfin(code, label, lens, risks, extra_monitor=()) -> Sector:
    return Sector(
        code=code,
        label=label,
        lens=lens,
        trend=(
            Metric("revenue", "Revenue", REVENUE, AMOUNT, "higher", group="Income"),
            Metric("ebitda", "EBITDA", ("INCOME.ebitda",), AMOUNT, "higher", group="Income"),
            Metric("ebit", "EBIT", ("INCOME.ebit",), AMOUNT, "higher", group="Income"),
            Metric("net_profit", "Net profit", PROFIT, AMOUNT, "higher", group="Income"),
            Metric("eps", "EPS (basic)", ("INCOME.eps_basic",), PERSHARE, "higher", group="Income"),
            Metric(
                "ebitda_margin",
                "EBITDA margin",
                ("MARGIN.ebitda_margin",),
                PCT,
                "higher",
                group="Margins",
            ),
            Metric(
                "ebit_margin",
                "EBIT margin",
                ("MARGIN.ebit_margin",),
                PCT,
                "higher",
                group="Margins",
            ),
            Metric(
                "net_margin", "Net margin", ("MARGIN.net_margin",), PCT, "higher", group="Margins"
            ),
            Metric(
                "total_assets",
                "Total assets",
                ("BALANCE_SHEET.total_assets",),
                AMOUNT,
                None,
                group="Balance sheet",
            ),
            Metric(
                "total_equity",
                "Shareholders' equity",
                ("BALANCE_SHEET.total_equity", "BALANCE.owners_equity"),
                AMOUNT,
                "higher",
                group="Balance sheet",
            ),
            Metric(
                "debt_to_equity",
                "Debt to equity",
                ("LEVERAGE.debt_to_equity",),
                MULT,
                "lower",
                group="Balance sheet",
            ),
            Metric(
                "current_ratio",
                "Current ratio",
                ("LIQUIDITY.current_ratio",),
                MULT,
                "higher",
                group="Balance sheet",
            ),
            Metric(
                "roe",
                "Return on equity",
                ("RETURN.roe_closing", "RETURN.roe_average"),
                PCT,
                "higher",
                group="Returns",
            ),
            Metric(
                "roce",
                "Return on capital employed",
                ("RETURN.roce",),
                PCT,
                "higher",
                group="Returns",
            ),
            Metric(
                "cfo",
                "Net cash from operations",
                ("CASH_FLOW.summary.operating", "CASH_FLOW.net_cash_from_operating_activities"),
                AMOUNT,
                "higher",
                group="Cash flow",
            ),
            Metric(
                "cfi",
                "Net cash from investing",
                ("CASH_FLOW.summary.investing",),
                AMOUNT,
                None,
                group="Cash flow",
            ),
            Metric(
                "cff",
                "Net cash from financing",
                ("CASH_FLOW.summary.financing",),
                AMOUNT,
                None,
                group="Cash flow",
            ),
            Metric(
                "fcf",
                "Free cash flow",
                ("CASH.free_cash_flow",),
                AMOUNT,
                "higher",
                group="Cash flow",
            ),
            Metric(
                "cfo_to_profit",
                "Cash conversion (CFO / net profit)",
                ("CASH.cfo_to_net_profit",),
                MULT,
                "higher",
                group="Cash flow",
            ),
        ),  # fmt: skip
        quarterly=(
            Metric("revenue", "Revenue", REVENUE, AMOUNT, ptype="QUARTERLY"),
            Metric("ebit", "EBIT", ("INCOME.ebit",), AMOUNT, ptype="QUARTERLY"),
            Metric("net_profit", "Net profit", PROFIT, AMOUNT, ptype="QUARTERLY"),
        ),
        peer=(
            Metric(
                "revenue_yoy",
                "Revenue growth (YoY)",
                ("GROWTH.revenue_yoy",),
                PCT,
                "higher",
                "ANY",
                "Growth",
            ),
            Metric(
                "net_profit_yoy",
                "Net profit growth (YoY)",
                ("GROWTH.net_profit_yoy",),
                PCT,
                "higher",
                "ANY",
                "Growth",
            ),
            Metric(
                "eps_yoy",
                "EPS growth (YoY)",
                ("GROWTH.eps_yoy",),
                PCT,
                "higher",
                "ANNUAL",
                "Growth",
            ),
            Metric(
                "ebit_margin",
                "EBIT margin",
                ("MARGIN.ebit_margin",),
                PCT,
                "higher",
                "ANNUAL",
                "Profitability",
            ),
            Metric(
                "ebitda_margin",
                "EBITDA margin",
                ("MARGIN.ebitda_margin",),
                PCT,
                "higher",
                "ANNUAL",
                "Profitability",
            ),
            Metric(
                "net_margin",
                "Net margin",
                ("MARGIN.net_margin",),
                PCT,
                "higher",
                "ANNUAL",
                "Profitability",
            ),
            Metric(
                "roe",
                "Return on equity",
                ("RETURN.roe_closing",),
                PCT,
                "higher",
                "ANNUAL",
                "Returns",
            ),
            Metric(
                "roce",
                "Return on capital employed",
                ("RETURN.roce",),
                PCT,
                "higher",
                "ANNUAL",
                "Returns",
            ),
            Metric(
                "debt_to_equity",
                "Debt to equity",
                ("LEVERAGE.debt_to_equity",),
                MULT,
                "lower",
                "ANNUAL",
                "Balance sheet",
            ),
            Metric(
                "current_ratio",
                "Current ratio",
                ("LIQUIDITY.current_ratio",),
                MULT,
                "higher",
                "ANNUAL",
                "Balance sheet",
            ),
            Metric(
                "interest_cover",
                "Interest cover",
                ("RATIO.interest_cover",),
                MULT,
                "higher",
                "ANNUAL",
                "Balance sheet",
            ),
            Metric(
                "cfo_to_profit",
                "Cash conversion (CFO / net profit)",
                ("CASH.cfo_to_net_profit",),
                MULT,
                "higher",
                "ANNUAL",
                "Cash flow",
            ),
            Metric(
                "pe",
                "Price-to-earnings",
                ("VALUATION.pe_annual", "VALUATION.pe_ttm"),
                MULT,
                None,
                "ANY",
                "Valuation",
            ),
            Metric(
                "pb", "Price-to-book", ("VALUATION.price_to_book",), MULT, None, "ANY", "Valuation"
            ),
        ),  # fmt: skip
        monitor=(
            "revenue",
            "ebit_margin",
            "net_margin",
            "roe",
            "roce",
            "cfo_to_profit",
            "debt_to_equity",
            *extra_monitor,
        ),
        risks=risks,
        valuation="EARNINGS",
    )


IT_SERVICES = _nonfin(
    "IT_SERVICES",
    "IT services",
    "An IT-services company is read through revenue growth, operating margin, return on equity and cash "
    "conversion; utilisation, attrition and deal wins are used only where a source supplies them.",
    (
        ("Demand and client concentration", "Discretionary technology spending and a few large clients can move revenue quickly."),
        ("Margin pressure", "Wage inflation, attrition and pricing pressure compress operating margin."),
        ("Currency", "Revenue is largely in foreign currency, so rupee moves change reported growth and margin."),
        ("Technology shifts", "Automation and AI can change the demand for billable effort."),
    ),
)  # fmt: skip

FMCG = _nonfin(
    "FMCG",
    "FMCG",
    "An FMCG company is read through volume-led revenue growth, operating margin, return on capital "
    "employed and working capital.",
    (
        ("Input costs", "Agricultural and packaging cost swings affect gross margin before price increases follow."),
        ("Consumption demand", "Rural and urban demand cycles move volume growth."),
        ("Competition", "Regional and new-age brands pressure share and pricing."),
        ("Regulation and taxes", "Changes in indirect tax, labelling or health rules affect category economics."),
    ),
)  # fmt: skip

AUTOMOBILES = _nonfin(
    "AUTOMOBILES",
    "Automobiles",
    "An automobile company is read through volume and revenue growth, operating margin, leverage and "
    "capital intensity; monthly volume releases are used where captured.",
    (
        ("Demand cycle", "Vehicle demand is cyclical and sensitive to interest rates, fuel prices and consumer sentiment."),
        ("Input and supply costs", "Commodity and component costs, and supply disruptions, affect margin and volume."),
        ("Technology transition", "The shift to electric vehicles needs capital and can change competitive position."),
        ("Regulation", "Emission, safety and tax changes alter product cost and demand."),
    ),
)  # fmt: skip

ENERGY = _nonfin(
    "ENERGY",
    "Energy",
    "An energy company is read through revenue, operating margin, leverage and capital spending; "
    "commodity-price sensitivity and regulated pricing are not modelled in this edition.",
    (
        ("Commodity prices", "Crude, gas and refining margins drive earnings and are outside company control."),
        ("Government pricing and subsidy", "Administered prices and sharing arrangements can cap or reduce realisations."),
        ("Capital intensity and leverage", "Large capital programmes and debt raise financial risk."),
        ("Energy transition", "Long-run demand and regulation for fossil fuels carry uncertainty."),
    ),
    extra_monitor=("debt_to_equity",),
)  # fmt: skip

GENERIC = _nonfin(
    "GENERIC_NONFIN",
    "Non-financial company",
    "A non-financial company is read through revenue growth, margins, returns, leverage and cash "
    "conversion.",
    (
        ("Demand and competition", "Revenue and margin depend on market demand and competitive intensity."),
        ("Leverage", "Debt raises financial risk when earnings fall."),
    ),
)  # fmt: skip

SECTORS: dict[str, Sector] = {
    "BANK": BANK,
    "IT_SERVICES": IT_SERVICES,
    "FMCG": FMCG,
    "AUTOMOBILES": AUTOMOBILES,
    "ENERGY": ENERGY,
    "CONGLOMERATE": GENERIC,
    "GENERIC_NONFIN": GENERIC,
}


def sector_for(profile: str | None) -> Sector:
    return SECTORS.get(profile or "", GENERIC)
