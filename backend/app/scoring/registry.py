# ruff: noqa: E501
"""Absolute-scoring profiles: which metrics, which pillar, which curve, which weight - and why.

All of this is CURATED by the project and says so. It is data, versioned (`VERSION`), copied into the
`metric_definitions` and `scoring_configs` tables by `seed()` so that every stored score can name the
exact definition it used. To change anything: edit here, bump `VERSION`, run the seed; old versions
stay in the database.

Honesty about the curves:

* A PERCENTILE metric is read against a benchmark built from real observations
  (`scoring/benchmarks.py`). If the sample is too small there is no benchmark, and the metric falls
  back to its curve only if it has one.
* Every fallback curve is a CONVENTION: a stated economic rationale (cost of capital, conventional
  safety levels), NOT a validated industry benchmark. Results using one are labelled that way and
  lower the run's confidence.
* Metrics with no source in the system (volume growth, inventory turnover, NPA ratios, ...) are
  listed on purpose, with the reason. They count as missing weight; they are never scored as zero.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MetricDefinition, ScoringConfig

VERSION = "1.1"
EFFECTIVE = date(2026, 11, 10)
PILLARS = ("GROWTH", "PROFITABILITY", "FINANCIAL_HEALTH", "VALUATION", "EFFICIENCY")
PRETTY = {
    "GROWTH": "Growth",
    "PROFITABILITY": "Profitability",
    "FINANCIAL_HEALTH": "Financial health",
    "VALUATION": "Valuation",
    "EFFICIENCY": "Operational efficiency",
}

CONVENTION_NOTE = (
    "Project convention with a stated rationale; NOT a validated industry benchmark. {why}"
)


def curve(points, why):
    return {"points": points, "why": why}


GDP = "Anchored on Indian nominal GDP growth of roughly 10% a year (an assumption, not a source)."
COC = "Anchored on a typical 10-12% cost of capital: returns below it destroy value."
SAFETY = "Conventional credit-safety levels used by lenders; not specific to this industry."

C_REV_GROWTH = curve([[-10, 0], [0, 25], [8, 50], [15, 75], [25, 100]], GDP)
C_PROFIT_GROWTH = curve([[-30, 0], [0, 30], [10, 55], [20, 80], [35, 100]], GDP)
C_ROCE = curve([[0, 0], [8, 30], [15, 60], [25, 85], [40, 100]], COC)
C_ROA_BANK = curve([[0, 0], [0.5, 30], [1.0, 60], [1.5, 85], [2.0, 100]], COC)
C_DE = curve([[0, 100], [0.5, 90], [1, 65], [2, 30], [3, 0]], SAFETY)
C_IC = curve([[1, 0], [1.5, 20], [3, 60], [5, 85], [8, 100]], SAFETY)
C_CFO = curve(
    [[0, 0], [0.5, 30], [0.8, 60], [1.0, 85], [1.2, 100]],
    "Cash from operations should roughly match reported profit; far below it signals low earnings"
    " quality.",
)
C_COST_INCOME = curve(
    [[35, 100], [45, 85], [55, 60], [65, 30], [80, 0]],
    "Conventional bank efficiency levels; varies by business model.",
)
C_EQ_MULT_BANK = curve(
    [[8, 100], [12, 75], [16, 35], [20, 0]], "Regulatory leverage context; conventional levels."
)
C_PROV_PPOP = curve(
    [[5, 100], [15, 80], [30, 50], [50, 20], [80, 0]],
    "Share of pre-provision profit absorbed by provisions; conventional levels.",
)
R_CURRENT = {
    "zero_low": 0.7,
    "full_low": 1.2,
    "full_high": 3.0,
    "zero_high": 5.0,
    "why": "Too little liquidity is a risk and so is idle working capital.",
}
R_ADV_DEP = {
    "zero_low": 50,
    "full_low": 65,
    "full_high": 85,
    "zero_high": 100,
    "why": "Lending far below deposits wastes funding; near 100% strains liquidity.",
}


@dataclass(frozen=True)
class Spec:
    code: str
    pillar: str
    name: str
    formula: str
    unit: str
    direction: str  # HIGHER | LOWER | RANGE
    kind: str  # PERCENTILE | THRESHOLD | RANGE
    calc: str | None  # the calculated metric it reads; None when no source exists
    weight: float = 1.0
    curve: dict | None = None  # fallback (PERCENTILE) or primary (THRESHOLD/RANGE)
    unavailable: str | None = None
    alts: tuple[str, ...] = ()  # other calculated metrics with the same meaning, tried in order


def pct(code, pillar, name, formula, unit, direction, calc, weight=1.0, fallback=None):
    return Spec(code, pillar, name, formula, unit, direction, "PERCENTILE", calc, weight, fallback)


def thr(code, pillar, name, formula, unit, direction, calc, curve_, weight=1.0):
    return Spec(code, pillar, name, formula, unit, direction, "THRESHOLD", calc, weight, curve_)


def rng(code, pillar, name, formula, unit, calc, range_, weight=1.0):
    return Spec(code, pillar, name, formula, unit, "RANGE", "RANGE", calc, weight, range_)


def missing(code, pillar, name, unit, direction, why, weight=1.0):
    kind = "PERCENTILE"
    return Spec(
        code, pillar, name, "not calculable", unit, direction, kind, None, weight, None, why
    )


NO_SEGMENTS = "segment data is not stored for this company"
NO_VOLUME = "unit volumes are in investor presentations, which are not extracted"
NO_WC = "inventory and receivables are not in the sources held for this company"
NO_CAPEX = "capital expenditure is not available, so free cash flow cannot be derived"
NO_NETDEBT = (
    "cash and investments are not extracted, so net debt and enterprise value cannot be built"
)
NO_BANKQ = "asset-quality and capital disclosures are not extracted for this bank"

# ---- shared building blocks -----------------------------------------------------------------------
G_REV = pct(
    "GROWTH.revenue_yoy",
    "GROWTH",
    "Revenue growth",
    "revenue / revenue a year earlier - 1",
    "percent",
    "HIGHER",
    "GROWTH.revenue_yoy",
    1.5,
    C_REV_GROWTH,
)
G_EPS = pct(
    "GROWTH.eps_yoy",
    "GROWTH",
    "EPS growth",
    "EPS / EPS a year earlier - 1",
    "percent",
    "HIGHER",
    "GROWTH.eps_yoy",
    1.0,
    C_PROFIT_GROWTH,
)
G_PROFIT = pct(
    "GROWTH.net_profit_yoy",
    "GROWTH",
    "Net profit growth",
    "profit / profit a year earlier - 1",
    "percent",
    "HIGHER",
    "GROWTH.net_profit_yoy",
    1.0,
    C_PROFIT_GROWTH,
)
G_CAGR = pct(
    "GROWTH.total_income_cagr_3y",
    "GROWTH",
    "Income growth (3-year CAGR)",
    "(income / income 3y earlier)^(1/3) - 1",
    "percent",
    "HIGHER",
    "GROWTH.total_income_cagr_3y",
    1.0,
    C_REV_GROWTH,
)
P_EBITDA = pct(
    "MARGIN.ebitda_margin",
    "PROFITABILITY",
    "EBITDA margin",
    "EBITDA / income",
    "percent",
    "HIGHER",
    "MARGIN.ebitda_margin",
    1.5,
)
P_EBIT = pct(
    "MARGIN.ebit_margin",
    "PROFITABILITY",
    "Operating profit margin",
    "EBIT / income",
    "percent",
    "HIGHER",
    "MARGIN.ebit_margin",
    1.0,
)
P_NET = pct(
    "MARGIN.net_margin",
    "PROFITABILITY",
    "Net profit margin",
    "profit / income",
    "percent",
    "HIGHER",
    "MARGIN.net_margin",
    1.0,
)
P_ROCE = pct(
    "RETURN.roce",
    "PROFITABILITY",
    "ROCE",
    "EBIT / capital employed",
    "percent",
    "HIGHER",
    "RETURN.roce",
    1.5,
    C_ROCE,
)
P_ROE = pct(
    "RETURN.roe_closing",
    "PROFITABILITY",
    "Return on equity",
    "profit / closing equity",
    "percent",
    "HIGHER",
    "RETURN.roe_closing",
    1.0,
    C_ROCE,
)
H_DE = thr(
    "LEVERAGE.debt_to_equity",
    "FINANCIAL_HEALTH",
    "Debt / equity",
    "borrowings / owners' equity",
    "x",
    "LOWER",
    "LEVERAGE.debt_to_equity",
    C_DE,
)
H_IC = thr(
    "RATIO.interest_cover",
    "FINANCIAL_HEALTH",
    "Interest coverage",
    "EBIT / finance costs",
    "x",
    "HIGHER",
    "RATIO.interest_cover",
    C_IC,
)
H_CFO = thr(
    "CASH.cfo_to_net_profit",
    "FINANCIAL_HEALTH",
    "Operating cash flow / profit (cash conversion)",
    "cash from operations / profit",
    "x",
    "HIGHER",
    "CASH.cfo_to_net_profit",
    C_CFO,
)
H_CUR = rng(
    "LIQUIDITY.current_ratio",
    "FINANCIAL_HEALTH",
    "Current ratio",
    "current assets / current liabilities",
    "x",
    "LIQUIDITY.current_ratio",
    R_CURRENT,
    0.5,
)
H_FCF = missing(
    "CASH.fcf_margin", "FINANCIAL_HEALTH", "Free cash flow", "percent", "HIGHER", NO_CAPEX
)
V_PE = pct(
    "VALUATION.pe_ttm",
    "VALUATION",
    "P/E (trailing)",
    "price / trailing EPS",
    "x",
    "LOWER",
    "VALUATION.pe_ttm",
    1.5,
)
V_PB = pct(
    "VALUATION.price_to_book",
    "VALUATION",
    "Price / book",
    "market cap / owners' equity",
    "x",
    "LOWER",
    "VALUATION.price_to_book",
    0.5,
)
V_EV = missing("VALUATION.ev_ebitda", "VALUATION", "EV / EBITDA", "x", "LOWER", NO_NETDEBT)
V_FCFY = missing(
    "VALUATION.fcf_yield", "VALUATION", "Free cash flow yield", "percent", "HIGHER", NO_CAPEX
)
E_AT = pct(
    "EFFICIENCY.asset_turnover",
    "EFFICIENCY",
    "Asset turnover",
    "income / average total assets",
    "x",
    "HIGHER",
    "EFFICIENCY.asset_turnover",
)
E_INV = missing(
    "EFFICIENCY.inventory_turnover", "EFFICIENCY", "Inventory turnover", "x", "HIGHER", NO_WC
)
E_WC = missing(
    "EFFICIENCY.working_capital_cycle",
    "EFFICIENCY",
    "Working-capital cycle",
    "days",
    "LOWER",
    NO_WC,
)
E_REC = missing(
    "EFFICIENCY.receivable_days", "EFFICIENCY", "Receivable days", "days", "LOWER", NO_WC
)
G_SEGREV = missing(
    "GROWTH.segment_revenue_growth",
    "GROWTH",
    "Segment revenue growth",
    "percent",
    "HIGHER",
    NO_SEGMENTS,
)
G_VOL = missing("GROWTH.volume_growth", "GROWTH", "Volume growth", "percent", "HIGHER", NO_VOLUME)


@dataclass(frozen=True)
class Profile:
    code: str
    label: str
    applies_to: dict
    weights: dict[str, int]
    rationale: dict[str, str]
    metrics: tuple[Spec, ...]
    min_weight_coverage: float = 60.0  # of the weight that has a data source (see engine)
    critical_pillars: tuple[str, ...] = ("PROFITABILITY", "FINANCIAL_HEALTH")
    max_staleness_days: int = 548


_STD = {
    "GROWTH": "Sets the long-run direction of the business.",
    "PROFITABILITY": "Margins and returns show pricing power and capital discipline.",
    "FINANCIAL_HEALTH": "Leverage, cover and cash conversion decide whether results are durable.",
    "VALUATION": "Cheaper is not better by itself, so valuation carries a lower weight.",
    "EFFICIENCY": "How hard the asset base works.",
}

PROFILES: dict[str, Profile] = {
    "FMCG": Profile(
        "FMCG",
        "FMCG (HUL, ITC, ...)",
        {"sectors": ["fmcg"]},
        {
            "GROWTH": 20,
            "PROFITABILITY": 25,
            "FINANCIAL_HEALTH": 20,
            "VALUATION": 15,
            "EFFICIENCY": 20,
        },
        {
            **_STD,
            "PROFITABILITY": "Pricing power and brand strength show up in margins and ROCE: highest weight.",
        },
        (
            G_REV,
            G_EPS,
            G_VOL,
            G_SEGREV,
            P_EBITDA,
            P_EBIT,
            P_NET,
            P_ROCE,
            H_DE,
            H_IC,
            H_CFO,
            H_FCF,
            V_PE,
            V_EV,
            V_PB,
            V_FCFY,
            E_AT,
            E_INV,
            E_WC,
            E_REC,
        ),
    ),
    "AUTOMOBILES": Profile(
        "AUTOMOBILES",
        "Automobiles (M&M, Maruti, ...)",
        {"sectors": ["automobiles"]},
        {
            "GROWTH": 20,
            "PROFITABILITY": 20,
            "FINANCIAL_HEALTH": 20,
            "VALUATION": 15,
            "EFFICIENCY": 25,
        },
        {
            **_STD,
            "EFFICIENCY": "A capital- and working-capital-heavy industry: asset use matters most.",
        },
        (
            G_REV,
            G_EPS,
            G_VOL,
            G_SEGREV,
            P_EBITDA,
            P_EBIT,
            P_NET,
            P_ROCE,
            H_DE,
            H_IC,
            H_CFO,
            H_FCF,
            V_PE,
            V_EV,
            V_PB,
            V_FCFY,
            E_AT,
            E_INV,
            E_WC,
            missing(
                "EFFICIENCY.capacity_utilisation",
                "EFFICIENCY",
                "Capacity utilisation",
                "percent",
                "HIGHER",
                "plant utilisation is not a reported financial-statement item",
            ),
        ),
    ),
    "CONGLOMERATE": Profile(
        "CONGLOMERATE",
        "Diversified conglomerate (Reliance)",
        {"industries": ["diversified_conglomerate"]},
        {
            "GROWTH": 15,
            "PROFITABILITY": 20,
            "FINANCIAL_HEALTH": 30,
            "VALUATION": 15,
            "EFFICIENCY": 20,
        },
        {
            **_STD,
            "FINANCIAL_HEALTH": "Heavy capital programmes make balance-sheet strength the main risk.",
        },
        (
            G_REV,
            G_SEGREV,
            missing(
                "GROWTH.segment_ebitda_growth",
                "GROWTH",
                "Segment EBITDA growth",
                "percent",
                "HIGHER",
                NO_SEGMENTS,
            ),
            P_EBITDA,
            P_ROCE,
            missing(
                "MARGIN.segment_ebitda_margin",
                "PROFITABILITY",
                "Segment EBITDA margins",
                "percent",
                "HIGHER",
                NO_SEGMENTS,
            ),
            H_IC,
            H_CFO,
            H_FCF,
            missing(
                "LEVERAGE.net_debt_ebitda",
                "FINANCIAL_HEALTH",
                "Net debt / EBITDA",
                "x",
                "LOWER",
                NO_NETDEBT,
            ),
            missing(
                "VALUATION.sotp",
                "VALUATION",
                "Sum-of-the-parts valuation",
                "ratio",
                "LOWER",
                "needs segment data and reliable segment valuation assumptions; never manufactured",
                2.0,
            ),
            pct(
                "VALUATION.pe_ttm",
                "VALUATION",
                "P/E (trailing, secondary reference)",
                "price / trailing EPS",
                "x",
                "LOWER",
                "VALUATION.pe_ttm",
                0.5,
            ),
            E_AT,
            missing(
                "EFFICIENCY.capex_efficiency",
                "EFFICIENCY",
                "Capital-expenditure efficiency",
                "x",
                "HIGHER",
                NO_CAPEX,
            ),
            missing(
                "EFFICIENCY.segment_roc",
                "EFFICIENCY",
                "Segment return on capital",
                "percent",
                "HIGHER",
                NO_SEGMENTS,
            ),
        ),
    ),
    "BANK": Profile(
        "BANK",
        "Banking",
        {"sectors": ["banking"]},
        {
            "GROWTH": 20,
            "PROFITABILITY": 25,
            "FINANCIAL_HEALTH": 30,
            "VALUATION": 10,
            "EFFICIENCY": 15,
        },
        {
            **_STD,
            "FINANCIAL_HEALTH": "Asset quality and capital decide a bank's survival: highest weight.",
        },
        (
            pct(
                "GROWTH.advances_yoy",
                "GROWTH",
                "Credit growth",
                "advances / advances a year earlier - 1",
                "percent",
                "HIGHER",
                "GROWTH.advances_yoy",
                1.0,
                C_REV_GROWTH,
            ),
            pct(
                "GROWTH.deposits_yoy",
                "GROWTH",
                "Deposit growth",
                "deposits / deposits a year earlier - 1",
                "percent",
                "HIGHER",
                "GROWTH.deposits_yoy",
                1.0,
                C_REV_GROWTH,
            ),
            pct(
                "GROWTH.net_interest_income_yoy",
                "GROWTH",
                "Net interest income growth",
                "NII / NII a year earlier - 1",
                "percent",
                "HIGHER",
                "GROWTH.net_interest_income_yoy",
                1.0,
                C_REV_GROWTH,
            ),
            G_PROFIT,
            pct(
                "RETURN.roa_closing",
                "PROFITABILITY",
                "Return on assets",
                "profit / closing assets",
                "percent",
                "HIGHER",
                "RETURN.roa_closing",
                1.5,
                C_ROA_BANK,
            ),
            P_ROE,
            missing(
                "RATIO.net_interest_margin",
                "PROFITABILITY",
                "Net interest margin",
                "percent",
                "HIGHER",
                "needs average earning assets, which are not extracted",
            ),
            thr(
                "LEVERAGE.equity_multiplier",
                "FINANCIAL_HEALTH",
                "Assets / equity",
                "total assets / owners' equity",
                "x",
                "LOWER",
                "LEVERAGE.equity_multiplier",
                C_EQ_MULT_BANK,
            ),
            thr(
                "RATIO.provisions_to_ppop",
                "FINANCIAL_HEALTH",
                "Provisions / pre-provision profit",
                "provisions / PPOP",
                "percent",
                "LOWER",
                "RATIO.provisions_to_ppop",
                C_PROV_PPOP,
            ),
            rng(
                "RATIO.advances_to_deposits",
                "FINANCIAL_HEALTH",
                "Credit-deposit ratio",
                "advances / deposits",
                "percent",
                "RATIO.advances_to_deposits",
                R_ADV_DEP,
                0.5,
            ),
            missing(
                "ASSET_QUALITY.gross_npa",
                "FINANCIAL_HEALTH",
                "Gross NPA ratio",
                "percent",
                "LOWER",
                NO_BANKQ,
                1.5,
            ),
            missing(
                "ASSET_QUALITY.net_npa",
                "FINANCIAL_HEALTH",
                "Net NPA ratio",
                "percent",
                "LOWER",
                NO_BANKQ,
                1.0,
            ),
            missing(
                "CAPITAL.capital_adequacy",
                "FINANCIAL_HEALTH",
                "Capital adequacy ratio",
                "percent",
                "HIGHER",
                NO_BANKQ,
                1.5,
            ),
            V_PE,
            pct(
                "VALUATION.price_to_book",
                "VALUATION",
                "Price / book",
                "market cap / owners' equity",
                "x",
                "LOWER",
                "VALUATION.price_to_book",
                1.5,
            ),
            thr(
                "RATIO.cost_to_income",
                "EFFICIENCY",
                "Cost-to-income ratio",
                "operating expenses / total income",
                "percent",
                "LOWER",
                "RATIO.cost_to_income",
                C_COST_INCOME,
            ),
            pct(
                "MARGIN.ppop_margin",
                "EFFICIENCY",
                "Pre-provision operating profit margin",
                "PPOP / income",
                "percent",
                "HIGHER",
                "MARGIN.ppop_margin",
                1.0,
            ),
            missing(
                "RATIO.casa_ratio",
                "EFFICIENCY",
                "CASA ratio",
                "percent",
                "HIGHER",
                "the deposit breakdown is in investor presentations, which are not extracted",
                0.5,
            ),
        ),
        critical_pillars=("PROFITABILITY",),
    ),
    "IT_SERVICES": Profile(
        "IT_SERVICES",
        "IT services",
        {"sectors": ["it_services"]},
        {
            "GROWTH": 20,
            "PROFITABILITY": 30,
            "FINANCIAL_HEALTH": 15,
            "VALUATION": 15,
            "EFFICIENCY": 20,
        },
        {**_STD, "PROFITABILITY": "Asset-light model: margins and returns are what differentiate."},
        (
            G_REV,
            G_PROFIT,
            G_EPS,
            missing(
                "GROWTH.constant_currency",
                "GROWTH",
                "Constant-currency revenue growth",
                "percent",
                "HIGHER",
                "reported by companies in presentations, not extracted; reported growth above is NOT constant-currency",
            ),
            P_EBIT,
            P_NET,
            P_ROCE,
            P_ROE,
            H_CFO,
            H_CUR,
            H_DE,
            V_PE,
            E_AT,
            missing(
                "EFFICIENCY.revenue_per_employee",
                "EFFICIENCY",
                "Revenue per employee",
                "inr",
                "HIGHER",
                "headcount is not extracted",
            ),
            missing(
                "EFFICIENCY.utilisation",
                "EFFICIENCY",
                "Utilisation",
                "percent",
                "HIGHER",
                "reported in presentations, not extracted",
            ),
            missing(
                "EFFICIENCY.attrition",
                "EFFICIENCY",
                "Attrition",
                "percent",
                "LOWER",
                "reported in presentations, not extracted",
            ),
        ),
    ),
    "GENERIC_NONFIN": Profile(
        "GENERIC_NONFIN",
        "Other non-financial companies (energy, two-wheelers, ...)",
        {"sectors": ["*"]},
        {
            "GROWTH": 20,
            "PROFITABILITY": 25,
            "FINANCIAL_HEALTH": 25,
            "VALUATION": 15,
            "EFFICIENCY": 15,
        },
        {
            **_STD,
            "FINANCIAL_HEALTH": "Without a sector view, balance-sheet strength gets extra weight.",
        },
        (
            G_REV,
            G_EPS,
            G_PROFIT,
            P_EBITDA,
            P_NET,
            P_ROCE,
            H_DE,
            H_IC,
            H_CFO,
            H_CUR,
            V_PE,
            V_PB,
            E_AT,
            E_INV,
            E_WC,
        ),
    ),
}


def profile_for(sector: str | None, industry: str | None) -> Profile:
    """The scoring profile of a company by its industry, then sector; generic otherwise."""
    for profile in PROFILES.values():
        if industry and industry in profile.applies_to.get("industries", []):
            return profile
    for profile in PROFILES.values():
        if sector and sector in profile.applies_to.get("sectors", []):
            return profile
    return PROFILES["GENERIC_NONFIN"]


def benchmarked_metrics() -> set[str]:
    """Every calculated metric that some profile reads against an empirical benchmark."""
    return {
        s.calc for p in PROFILES.values() for s in p.metrics if s.calc and s.kind == "PERCENTILE"
    }


def seed(session: Session) -> int:
    """Copy the definitions into the database (idempotent). Returns the number of rows added."""
    added = 0
    for profile in PROFILES.values():
        have = session.scalar(
            select(ScoringConfig).where(
                ScoringConfig.profile == profile.code, ScoringConfig.version == VERSION
            )
        )
        if have is None:
            session.add(
                ScoringConfig(
                    profile=profile.code,
                    version=VERSION,
                    pillar_weights=profile.weights,
                    rationale=profile.rationale,
                    min_weight_coverage=profile.min_weight_coverage,
                    critical_pillars=list(profile.critical_pillars),
                    max_staleness_days=profile.max_staleness_days,
                    effective_date=EFFECTIVE,
                )
            )
            added += 1
        for spec in profile.metrics:
            exists = session.scalar(
                select(MetricDefinition.id).where(
                    MetricDefinition.code == spec.code,
                    MetricDefinition.profile == profile.code,
                    MetricDefinition.version == VERSION,
                )
            )
            if exists:
                continue
            is_curve = spec.curve is not None
            session.add(
                MetricDefinition(
                    code=spec.code,
                    profile=profile.code,
                    version=VERSION,
                    pillar=spec.pillar,
                    display_name=spec.name,
                    formula=spec.formula,
                    unit=spec.unit,
                    direction=spec.direction,
                    kind=spec.kind,
                    curve=spec.curve,
                    calc_metric=spec.calc,
                    applies_to=profile.applies_to,
                    weight=spec.weight,
                    curve_source_class="CONVENTION" if is_curve else None,
                    curve_source_note=CONVENTION_NOTE.format(why=(spec.curve or {}).get("why", ""))
                    if is_curve
                    else None,
                    unavailable_reason=spec.unavailable,
                    min_quality="LOW",
                    effective_date=EFFECTIVE,
                )
            )
            added += 1
    session.flush()
    return added
