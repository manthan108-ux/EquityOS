"""Sector profiles: what is scored, in which direction, and how much each pillar weighs.

Everything here is CURATED by the project and says so. It is not read from an exchange, not derived
from data, and never produced by an LLM (the specification forbids invented sector weights). Each
weight carries the reason it was chosen; to change one, edit it, bump `VERSION` and a new row is
stored (old versions stay, so past scores remain explainable).

Scored pillars (what the metrics measure):

* GROWTH          how fast the business is expanding
* PROFITABILITY   how much of each rupee of income is kept
* RETURNS         how well capital is used
* BALANCE_SHEET   financial risk: leverage, liquidity, provisioning burden
* VALUATION       how cheap the market price is relative to earnings or book value
* MOMENTUM        the share price's recent behaviour (technical)

Direction: HIGHER means a larger value is more favourable; LOWER means a smaller one is. For
VALUATION, favourable means *cheaper than peers*, which is not the same as better.

Known weakness: bank asset quality (GNPA/NNPA), net interest margin and CASA ratio, and for
non-financials EBITDA margin and interest cover, are not scored because those inputs are not
extracted yet (see KNOWN_LIMITATIONS.md). Pillars built on fewer metrics are weaker for it.
"""

from dataclasses import dataclass

VERSION = "1.0"
PILLARS = ("GROWTH", "PROFITABILITY", "RETURNS", "BALANCE_SHEET", "VALUATION", "MOMENTUM")
MIN_PEERS = 3  # fewer than this and every comparison is marked as a thin group


@dataclass(frozen=True)
class ScoredMetric:
    code: str  # a calculated metric (see docs/CALCULATION_ENGINE.md)
    pillar: str
    direction: str  # HIGHER | LOWER
    periods: tuple[str, ...]  # period types in order of preference; () = as of the price date
    label: str


def m(code, pillar, direction, periods, label):
    return ScoredMetric(code, pillar, direction, tuple(periods), label)


_ANNUAL = ("ANNUAL",)
# Flow ratios that do not depend on the length of the period (growth against the same period a
# year earlier, margins, balance-sheet ratios) may also be compared on a common quarter when too
# few peers share a year. Returns are annual only: a quarter's return is not comparable unless it
# is annualised, and nothing here annualises.
_YEAR_OR_QUARTER = ("ANNUAL", "QUARTERLY")
_TECH: tuple[ScoredMetric, ...] = (
    m("TECH.return_6m", "MOMENTUM", "HIGHER", (), "6-month price return"),
    m("TECH.return_1y", "MOMENTUM", "HIGHER", (), "1-year price return"),
    m("TECH.pct_from_52w_high", "MOMENTUM", "HIGHER", (), "Closeness to the 52-week high"),
    m("TECH.volatility_126d", "MOMENTUM", "LOWER", (), "Price volatility (126 days)"),
)

BANK_METRICS: tuple[ScoredMetric, ...] = (
    m(
        "GROWTH.net_interest_income_yoy",
        "GROWTH",
        "HIGHER",
        _YEAR_OR_QUARTER,
        "Net interest income growth",
    ),
    m("GROWTH.net_profit_yoy", "GROWTH", "HIGHER", _YEAR_OR_QUARTER, "Net profit growth"),
    m("GROWTH.advances_yoy", "GROWTH", "HIGHER", _YEAR_OR_QUARTER, "Advances growth"),
    m("GROWTH.deposits_yoy", "GROWTH", "HIGHER", _YEAR_OR_QUARTER, "Deposits growth"),
    m(
        "MARGIN.ppop_margin",
        "PROFITABILITY",
        "HIGHER",
        _YEAR_OR_QUARTER,
        "Pre-provision profit margin",
    ),
    m("RATIO.cost_to_income", "PROFITABILITY", "LOWER", _YEAR_OR_QUARTER, "Cost-to-income ratio"),
    m(
        "MARGIN.net_profit_to_income",
        "PROFITABILITY",
        "HIGHER",
        _YEAR_OR_QUARTER,
        "Net profit / income",
    ),
    m("RETURN.roe_closing", "RETURNS", "HIGHER", _ANNUAL, "Return on equity"),
    m("RETURN.roa_closing", "RETURNS", "HIGHER", _ANNUAL, "Return on assets"),
    m("LEVERAGE.equity_multiplier", "BALANCE_SHEET", "LOWER", _YEAR_OR_QUARTER, "Assets / equity"),
    m(
        "RATIO.provisions_to_ppop",
        "BALANCE_SHEET",
        "LOWER",
        _ANNUAL,
        "Provisions / pre-provision profit",
    ),
    m("VALUATION.price_to_book", "VALUATION", "LOWER", (), "Price to book"),
    m("VALUATION.pe_ttm", "VALUATION", "LOWER", (), "Price to earnings (trailing)"),
    *_TECH,
)

NONFIN_METRICS: tuple[ScoredMetric, ...] = (
    m("GROWTH.revenue_yoy", "GROWTH", "HIGHER", _YEAR_OR_QUARTER, "Revenue growth"),
    m("GROWTH.net_profit_yoy", "GROWTH", "HIGHER", _YEAR_OR_QUARTER, "Net profit growth"),
    m("GROWTH.eps_yoy", "GROWTH", "HIGHER", _YEAR_OR_QUARTER, "EPS growth"),
    m("MARGIN.net_margin", "PROFITABILITY", "HIGHER", _YEAR_OR_QUARTER, "Net profit margin"),
    m("MARGIN.pbt_margin", "PROFITABILITY", "HIGHER", _YEAR_OR_QUARTER, "Pre-tax profit margin"),
    m("RETURN.roe_closing", "RETURNS", "HIGHER", _ANNUAL, "Return on equity"),
    m("RETURN.roa_closing", "RETURNS", "HIGHER", _ANNUAL, "Return on assets"),
    m("LEVERAGE.debt_to_equity", "BALANCE_SHEET", "LOWER", _YEAR_OR_QUARTER, "Debt / equity"),
    m("LIQUIDITY.current_ratio", "BALANCE_SHEET", "HIGHER", _YEAR_OR_QUARTER, "Current ratio"),
    m("LEVERAGE.equity_multiplier", "BALANCE_SHEET", "LOWER", _YEAR_OR_QUARTER, "Assets / equity"),
    m("VALUATION.pe_ttm", "VALUATION", "LOWER", (), "Price to earnings (trailing)"),
    m("VALUATION.price_to_book", "VALUATION", "LOWER", (), "Price to book"),
    *_TECH,
)

METRICS = {"BANK": BANK_METRICS, "NONFIN": NONFIN_METRICS}


@dataclass(frozen=True)
class SectorProfile:
    sector_code: str
    profile: str  # BANK | NONFIN
    weights: dict[str, int]
    rationale: dict[str, str]
    note: str


def _p(code, profile, weights, rationale, note):
    assert sum(weights.values()) == 100 and set(weights) == set(PILLARS), code
    assert set(rationale) == set(PILLARS), code
    return SectorProfile(code, profile, weights, rationale, note)


_CURATED = "Curated project judgement, not derived from data and not LLM-generated."

BANKING = _p(
    "banking",
    "BANK",
    {
        "GROWTH": 15,
        "PROFITABILITY": 20,
        "RETURNS": 25,
        "BALANCE_SHEET": 15,
        "VALUATION": 15,
        "MOMENTUM": 10,
    },
    {
        "GROWTH": "Loan and deposit growth drive earnings, but growth bought with weak credit "
        "standards is not quality, so it does not dominate.",
        "PROFITABILITY": "Spread, fee income and cost control separate banks of similar size.",
        "RETURNS": "ROE and ROA are the primary measures of value creation for a bank, hence the "
        "largest weight.",
        "BALANCE_SHEET": "Leverage and the provisioning burden measure risk. Asset-quality ratios "
        "(GNPA, NNPA) would belong here but are not extracted yet, so this pillar is weaker "
        "than it should be.",
        "VALUATION": "Price to book anchors bank valuation; earnings multiples are secondary.",
        "MOMENTUM": "Price behaviour is context, not fundamentals: the smallest weight.",
    },
    _CURATED,
)
IT_SERVICES = _p(
    "it_services",
    "NONFIN",
    {
        "GROWTH": 20,
        "PROFITABILITY": 25,
        "RETURNS": 20,
        "BALANCE_SHEET": 5,
        "VALUATION": 20,
        "MOMENTUM": 10,
    },
    {
        "GROWTH": "Revenue and earnings growth set the multiple of an asset-light services firm.",
        "PROFITABILITY": "Margins reflect pricing power and delivery efficiency and differ widely "
        "between firms.",
        "RETURNS": "High returns on capital are normal in the sector; differences still matter.",
        "BALANCE_SHEET": "Most large IT services firms carry little debt, so leverage barely "
        "differentiates them; the weight is deliberately small.",
        "VALUATION": "Earnings multiples are the usual yardstick and vary a lot across peers.",
        "MOMENTUM": "Price behaviour is context, not fundamentals.",
    },
    _CURATED,
)
FMCG = _p(
    "fmcg",
    "NONFIN",
    {
        "GROWTH": 15,
        "PROFITABILITY": 25,
        "RETURNS": 25,
        "BALANCE_SHEET": 10,
        "VALUATION": 15,
        "MOMENTUM": 10,
    },
    {
        "GROWTH": "Mature, volume-led growth: valued but slower than in other sectors.",
        "PROFITABILITY": "Brand strength shows in margins, the sector's defining quality.",
        "RETURNS": "Asset-light models earn high returns on capital: the main quality test.",
        "BALANCE_SHEET": "Usually low leverage, so little differentiation.",
        "VALUATION": "These businesses trade at premium multiples, so relative value matters.",
        "MOMENTUM": "Price behaviour is context, not fundamentals.",
    },
    _CURATED,
)
AUTOMOBILES = _p(
    "automobiles",
    "NONFIN",
    {
        "GROWTH": 20,
        "PROFITABILITY": 20,
        "RETURNS": 15,
        "BALANCE_SHEET": 15,
        "VALUATION": 20,
        "MOMENTUM": 10,
    },
    {
        "GROWTH": "Volumes and launches drive a cyclical sector, so growth carries real weight.",
        "PROFITABILITY": "Margins swing with commodity costs and mix.",
        "RETURNS": "Capital intensity makes returns important but less decisive than elsewhere.",
        "BALANCE_SHEET": "Cyclical demand makes leverage and liquidity matter.",
        "VALUATION": "Cyclical earnings make the entry multiple especially important.",
        "MOMENTUM": "Price behaviour is context, not fundamentals.",
    },
    _CURATED,
)
OIL_AND_GAS = _p(
    "oil_and_gas",
    "NONFIN",
    {
        "GROWTH": 10,
        "PROFITABILITY": 20,
        "RETURNS": 20,
        "BALANCE_SHEET": 25,
        "VALUATION": 15,
        "MOMENTUM": 10,
    },
    {
        "GROWTH": "Earnings follow commodity cycles more than business growth.",
        "PROFITABILITY": "Margins capture refining and marketing spreads.",
        "RETURNS": "Capital-heavy; returns show whether investment pays.",
        "BALANCE_SHEET": "Large capital programmes make leverage and liquidity the main risk.",
        "VALUATION": "Cyclical earnings make multiples a rough guide.",
        "MOMENTUM": "Price behaviour is context, not fundamentals.",
    },
    _CURATED,
)
GENERIC_BANK = _p(
    "_generic_bank",
    "BANK",
    BANKING.weights,
    {
        k: f"Generic financial-company default (banking weights): {v}"
        for k, v in BANKING.rationale.items()
    },
    "Fallback for financial companies in sectors with no curated weights. " + _CURATED,
)
GENERIC_NONFIN = _p(
    "_generic_nonfin",
    "NONFIN",
    {
        "GROWTH": 15,
        "PROFITABILITY": 20,
        "RETURNS": 20,
        "BALANCE_SHEET": 15,
        "VALUATION": 20,
        "MOMENTUM": 10,
    },
    {
        "GROWTH": "Neutral default: growth matters in every sector.",
        "PROFITABILITY": "Neutral default: margins matter in every sector.",
        "RETURNS": "Neutral default: use of capital matters in every sector.",
        "BALANCE_SHEET": "Neutral default: financial risk matters in every sector.",
        "VALUATION": "Neutral default: price paid matters in every sector.",
        "MOMENTUM": "Price behaviour is context, not fundamentals.",
    },
    "Fallback for sectors with no curated weights. " + _CURATED,
)

PROFILES = {p.sector_code: p for p in (BANKING, IT_SERVICES, FMCG, AUTOMOBILES, OIL_AND_GAS)}


def profile_for(sector_code: str | None, statement_profile: str) -> SectorProfile:
    """Curated weights for the sector if they exist and fit the statements, else a generic set."""
    found = PROFILES.get(sector_code or "")
    if found is not None and found.profile == statement_profile:
        return found
    return GENERIC_BANK if statement_profile == "BANK" else GENERIC_NONFIN


__all__ = [
    "METRICS",
    "MIN_PEERS",
    "PILLARS",
    "PROFILES",
    "ScoredMetric",
    "SectorProfile",
    "VERSION",
    "profile_for",
]
