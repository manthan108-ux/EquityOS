"""The fundamental metrics the engine calculates, declared as data.

Each metric is a formula over named inputs. An input is a stored statement item (alternatives are
tried in order) at the same period or shifted back by whole months (growth, averages, trailing
sums). A metric may have several VARIANTS: the first whose inputs are all available is used, and
the variant is recorded, so a reader knows which definition produced a number.

Nothing here is a default or an estimate: if no variant can be satisfied the metric is a GAP with
the missing inputs listed. Formulas run through the evidence ledger (exact decimals, units tracked,
one basis, one period type).

Profiles: BANK (statements with interest earned/expended, advances and deposits) and NONFIN
(everything else). Metrics that make no sense for a profile (free cash flow of a bank, debt to
equity of a bank) are simply not attempted for it.

What is NOT here, and why: EBITDA, EBIT, operating margin, interest cover and ROCE need depreciation
and finance costs, which are not among the line items the document pipeline extracts yet. Rather
than approximate them, they are absent (see KNOWN_LIMITATIONS.md).
"""

from dataclasses import dataclass

ALL_TYPES = ("ANNUAL", "QUARTERLY", "HALF_YEAR", "NINE_MONTH")
YEARLY = ("ANNUAL",)
POSITION_TYPES = ("ANNUAL", "QUARTERLY")  # balance sheets exist for year ends and quarter ends
FLOW_TYPES = ("ANNUAL", "HALF_YEAR")  # cash-flow statements


@dataclass(frozen=True)
class Ref:
    metrics: tuple[str, ...]  # alternatives in priority order
    shift_months: int = 0  # use the period this many months EARLIER
    period_type: str | None = None  # None = the period type of the metric being calculated
    static: bool = False  # not tied to a period (a face value): use the current record


def ref(*metrics: str, shift: int = 0, ptype: str | None = None, static: bool = False) -> Ref:
    return Ref(tuple(metrics), shift, ptype, static)


@dataclass(frozen=True)
class Variant:
    expression: str
    inputs: dict[str, Ref]
    bases: tuple[str, ...] | None = None  # None = both
    profile: str = "ANY"
    note: str | None = None


@dataclass(frozen=True)
class Metric:
    code: str
    label: str
    category: str
    variants: tuple[Variant, ...]
    unit: str | None
    period_types: tuple[str, ...]
    profile: str = "ANY"
    spans: bool = False
    requires: tuple[tuple[str, str, int], ...] = ()  # (input name, operator, number)
    anchor: tuple[str, ...] = ()  # metrics whose periods decide which periods are attempted
    period_label: str | None = None  # name the result's period type (e.g. "TTM")


def one(expr: str, inputs: dict[str, Ref], **kw) -> Variant:
    return Variant(expr, inputs, **kw)


P = "INCOME.profit_before_tax"
REV = "INCOME.revenue_from_operations"
TOT = "INCOME.total_income"
OTH = "INCOME.other_income"
EPS = "INCOME.eps_basic"
EQ = "BALANCE.owners_equity"
ATTR = "INCOME.attributable_profit"
NII = "INCOME.net_interest_income"
TA = "BALANCE_SHEET.total_assets"
EBIT = "INCOME.ebit"
EBITDA = "INCOME.ebitda"
FIN = "INCOME.finance_costs"


def _growth(code, label, metric, *, profile="ANY", positive=False, types=ALL_TYPES, shift=12):
    return Metric(
        code=code,
        label=label,
        category="GROWTH",
        variants=(
            one(
                "(now / before - 1) * 100", {"now": ref(metric), "before": ref(metric, shift=shift)}
            ),
        ),
        unit="percent",
        period_types=types,
        profile=profile,
        spans=True,
        requires=(("before", ">", 0),) if positive else (),
    )


def _cagr(code, label, metric, years=3):
    return Metric(
        code=code,
        label=label,
        category="GROWTH",
        variants=(
            one(
                f"((now / before) ^ (1 / {years}) - 1) * 100",
                {"now": ref(metric), "before": ref(metric, shift=12 * years)},
            ),
        ),
        unit="percent",
        period_types=YEARLY,
        spans=True,
        requires=(("before", ">", 0), ("now", ">", 0)),
    )


def _ttm(code, label, metric, profile="ANY", note=None):
    """Trailing twelve months as the sum of the last four reported quarters."""
    return Metric(
        code=code,
        label=label,
        category="TTM",
        variants=(
            one(
                "q0 + q1 + q2 + q3",
                {
                    "q0": ref(metric),
                    "q1": ref(metric, shift=3),
                    "q2": ref(metric, shift=6),
                    "q3": ref(metric, shift=9),
                },
                note=note,
            ),
        ),
        unit=None,
        period_types=("QUARTERLY",),
        profile=profile,
        spans=True,
        period_label="TTM",
    )


def _ttm_cumulative(code, label, metric, ptype, months_since_year_end, profile="ANY", note=None):
    """Trailing twelve months = last full year + this year's cumulative - last year's cumulative."""
    method = "last full year + year-to-date - prior year-to-date"
    return Metric(
        code=code,
        label=label,
        category="TTM",
        variants=(
            one(
                "year + ytd - prior_ytd",
                {
                    "year": ref(metric, shift=months_since_year_end, ptype="ANNUAL"),
                    "ytd": ref(metric),
                    "prior_ytd": ref(metric, shift=12),
                },
                note=f"{note}; {method}" if note else method,
            ),
        ),
        unit=None,
        period_types=(ptype,),
        profile=profile,
        spans=True,
        period_label="TTM",
    )


def _ttm_both(code, label, metric, profile="ANY", note=None):
    return (
        _ttm(code, label, metric, profile, note),
        _ttm_cumulative(code, label, metric, "HALF_YEAR", 6, profile, note),
        _ttm_cumulative(code, label, metric, "NINE_MONTH", 9, profile, note),
    )


CATALOGUE: tuple[Metric, ...] = (
    # ---- building blocks (also stored: they are calculations like any other) ------------------
    Metric(
        EQ,
        "Shareholders' funds attributable to owners",
        "DERIVED",
        (
            one(
                "share_capital + other_equity",
                {
                    "share_capital": ref("BALANCE_SHEET.equity_share_capital"),
                    "other_equity": ref("BALANCE_SHEET.other_equity"),
                },
            ),
            one(
                "capital + reserves",
                {
                    "capital": ref("BALANCE_SHEET.capital"),
                    "reserves": ref("BALANCE_SHEET.reserves_and_surplus"),
                },
                note="share capital plus reserves and surplus; ESOP outstanding is not added",
            ),
            one(
                "total_equity",
                {"total_equity": ref("BALANCE_SHEET.total_equity")},
                bases=("STANDALONE",),
                note="total equity of a standalone balance sheet (no non-controlling interests)",
            ),
            one(
                "total_equity - nci",
                {
                    "total_equity": ref("BALANCE_SHEET.total_equity"),
                    "nci": ref("BALANCE_SHEET.non_controlling_interests"),
                },
                bases=("CONSOLIDATED",),
            ),
        ),
        "inr",
        POSITION_TYPES,
        anchor=(
            "BALANCE_SHEET.equity_share_capital",
            "BALANCE_SHEET.capital",
            "BALANCE_SHEET.total_equity",
        ),
    ),
    Metric(
        ATTR,
        "Net profit attributable to owners",
        "DERIVED",
        (
            one("p", {"p": ref("INCOME.net_profit_owners")}),
            one(
                "p",
                {"p": ref("INCOME.net_profit")},
                profile="BANK",
                note="net profit after minority interest, as printed",
            ),
            one("p", {"p": ref("INCOME.net_profit")}, profile="NONFIN", bases=("STANDALONE",)),
            one(
                "p",
                {"p": ref("INCOME.net_profit")},
                profile="NONFIN",
                bases=("CONSOLIDATED",),
                note="includes the non-controlling interests' share: profit attributable to "
                "owners is not stored for this period",
            ),
        ),
        "inr",
        ALL_TYPES,
        anchor=("INCOME.net_profit",),
    ),
    Metric(
        NII,
        "Net interest income",
        "DERIVED",
        (
            one(
                "earned - expended",
                {
                    "earned": ref("INCOME.interest_earned"),
                    "expended": ref("INCOME.interest_expended"),
                },
            ),
        ),
        "inr",
        ALL_TYPES,
        profile="BANK",
    ),
    # Shares outstanding are not printed in a statement table, but they follow from two official
    # figures: paid-up equity capital (statements) and the face value (the exchange's security
    # list). The printed capital is rounded to the statement's unit, so the share count carries
    # that rounding (a few thousand shares on billions).
    Metric(
        "BALANCE.shares_outstanding",
        "Shares outstanding (paid-up equity capital / face value)",
        "DERIVED",
        (
            one(
                "capital / face",
                {
                    "capital": ref("BALANCE_SHEET.equity_share_capital", "BALANCE_SHEET.capital"),
                    "face": ref("SECURITY.face_value", static=True),
                },
                note="paid-up equity capital divided by the face value in the exchange's list",
            ),
            one(
                "capital / face",
                {
                    "capital": ref("INCOME.paid_up_equity_share_capital"),
                    "face": ref("SECURITY.face_value", static=True),
                },
                note="paid-up equity capital (printed with the results) divided by the face value",
            ),
        ),
        "shares",
        POSITION_TYPES,
        anchor=(
            "BALANCE_SHEET.equity_share_capital",
            "BALANCE_SHEET.capital",
            "INCOME.paid_up_equity_share_capital",
        ),
    ),
    # ---- operating profit measures (non-financial companies) -------------------------------------
    # Defined from the printed lines only, so they are comparable between companies that print them:
    # EBIT = profit before tax + finance costs; EBITDA = EBIT + depreciation and amortisation. Both
    # include other income and any exceptional items, because the statements are not split further
    # here. A company that prints no finance-cost or depreciation line simply has no value.
    Metric(
        EBIT,
        "Profit before tax plus finance costs (EBIT)",
        "DERIVED",
        (
            one(
                "pbt + finance",
                {"pbt": ref(P), "finance": ref(FIN)},
                note="includes other income and exceptional items",
            ),
        ),
        "inr",
        ALL_TYPES,
        profile="NONFIN",
        anchor=(FIN,),
    ),
    Metric(
        EBITDA,
        "EBIT plus depreciation and amortisation (EBITDA)",
        "DERIVED",
        (
            one(
                "ebit + depreciation",
                {"ebit": ref(EBIT), "depreciation": ref("INCOME.depreciation_and_amortisation")},
                note="includes other income and exceptional items",
            ),
        ),
        "inr",
        ALL_TYPES,
        profile="NONFIN",
        anchor=("INCOME.depreciation_and_amortisation",),
    ),
    Metric(
        "MARGIN.ebit_margin",
        "EBIT margin (on revenue from operations)",
        "MARGIN",
        (one("ebit / revenue * 100", {"ebit": ref(EBIT), "revenue": ref(REV)}),),
        "percent",
        ALL_TYPES,
        profile="NONFIN",
        anchor=(EBIT,),
    ),
    Metric(
        "MARGIN.ebitda_margin",
        "EBITDA margin (on revenue from operations)",
        "MARGIN",
        (one("ebitda / revenue * 100", {"ebitda": ref(EBITDA), "revenue": ref(REV)}),),
        "percent",
        ALL_TYPES,
        profile="NONFIN",
        anchor=(EBITDA,),
    ),
    Metric(
        "RATIO.interest_cover",
        "EBIT / finance costs",
        "LEVERAGE",
        (one("ebit / finance", {"ebit": ref(EBIT), "finance": ref(FIN)}),),
        "x",
        ALL_TYPES,
        profile="NONFIN",
        requires=(("finance", ">", 0),),
        anchor=(EBIT,),
    ),
    Metric(
        "RETURN.roce",
        "Return on capital employed (EBIT / (assets - current liabilities))",
        "RETURN",
        (
            one(
                "ebit / (assets - liabilities) * 100",
                {
                    "ebit": ref(EBIT),
                    "assets": ref(TA),
                    "liabilities": ref("BALANCE_SHEET.total_current_liabilities"),
                },
            ),
        ),
        "percent",
        YEARLY,
        profile="NONFIN",
        requires=(("assets", ">", 0),),
        anchor=(EBIT,),
    ),
    # ---- growth ---------------------------------------------------------------------------------
    _growth("GROWTH.total_income_yoy", "Total income growth, year on year", TOT),
    _growth(
        "GROWTH.revenue_yoy", "Revenue from operations growth, year on year", REV, profile="NONFIN"
    ),
    _growth(
        "GROWTH.net_interest_income_yoy",
        "Net interest income growth, year on year",
        NII,
        profile="BANK",
    ),
    _growth(
        "GROWTH.net_profit_yoy",
        "Net profit growth, year on year",
        "INCOME.net_profit",
        positive=True,
    ),
    _growth("GROWTH.pbt_yoy", "Profit before tax growth, year on year", P, positive=True),
    _growth("GROWTH.eps_yoy", "Basic EPS growth, year on year", EPS, positive=True),
    _growth(
        "GROWTH.total_income_qoq",
        "Total income growth, quarter on quarter",
        TOT,
        types=("QUARTERLY",),
        shift=3,
    ),
    _growth(
        "GROWTH.net_profit_qoq",
        "Net profit growth, quarter on quarter",
        "INCOME.net_profit",
        positive=True,
        types=("QUARTERLY",),
        shift=3,
    ),
    _growth(
        "GROWTH.advances_yoy",
        "Advances growth, year on year",
        "BALANCE_SHEET.advances",
        profile="BANK",
        types=POSITION_TYPES,
    ),
    _growth(
        "GROWTH.deposits_yoy",
        "Deposits growth, year on year",
        "BALANCE_SHEET.deposits",
        profile="BANK",
        types=POSITION_TYPES,
    ),
    _cagr("GROWTH.total_income_cagr_3y", "Total income CAGR, 3 years", TOT),
    _cagr("GROWTH.net_profit_cagr_3y", "Net profit CAGR, 3 years", "INCOME.net_profit"),
    # ---- margins and shares -----------------------------------------------------------------
    Metric(
        "MARGIN.net_margin",
        "Net profit margin (on revenue from operations)",
        "MARGIN",
        (one("profit / revenue * 100", {"profit": ref(ATTR), "revenue": ref(REV)}),),
        "percent",
        ALL_TYPES,
        profile="NONFIN",
    ),
    Metric(
        "MARGIN.net_profit_to_income",
        "Net profit as % of total income",
        "MARGIN",
        (one("profit / income * 100", {"profit": ref(ATTR), "income": ref(TOT)}),),
        "percent",
        ALL_TYPES,
        profile="BANK",
    ),
    Metric(
        "MARGIN.pbt_margin",
        "Profit before tax margin",
        "MARGIN",
        (one("pbt / revenue * 100", {"pbt": ref(P), "revenue": ref(REV)}),),
        "percent",
        ALL_TYPES,
        profile="NONFIN",
    ),
    Metric(
        "MARGIN.other_income_share",
        "Other income as % of total income",
        "MARGIN",
        (one("other / total * 100", {"other": ref(OTH), "total": ref(TOT)}),),
        "percent",
        ALL_TYPES,
    ),
    Metric(
        "RATIO.effective_tax_rate",
        "Effective tax rate (tax expense / profit before tax)",
        "MARGIN",
        (one("tax / pbt * 100", {"tax": ref("INCOME.tax_expense"), "pbt": ref(P)}),),
        "percent",
        ALL_TYPES,
        requires=(("pbt", ">", 0),),
    ),
    # ---- bank-specific ----------------------------------------------------------------------
    Metric(
        "RATIO.cost_to_income",
        "Operating expenses / (net interest income + other income)",
        "BANK",
        (
            one(
                "opex / (nii + other) * 100",
                {"opex": ref("INCOME.operating_expenses"), "nii": ref(NII), "other": ref(OTH)},
            ),
        ),
        "percent",
        ALL_TYPES,
        profile="BANK",
    ),
    Metric(
        "MARGIN.ppop_margin",
        "Pre-provision operating profit as % of total income",
        "BANK",
        (
            one(
                "ppop / income * 100",
                {"ppop": ref("INCOME.operating_profit_before_provisions"), "income": ref(TOT)},
            ),
        ),
        "percent",
        ALL_TYPES,
        profile="BANK",
    ),
    Metric(
        "RATIO.provisions_to_ppop",
        "Provisions / pre-provision operating profit",
        "BANK",
        (
            one(
                "prov / ppop * 100",
                {
                    "prov": ref("INCOME.provisions_excluding_tax"),
                    "ppop": ref("INCOME.operating_profit_before_provisions"),
                },
            ),
        ),
        "percent",
        ALL_TYPES,
        profile="BANK",
        requires=(("ppop", ">", 0),),
    ),
    Metric(
        "RATIO.advances_to_deposits",
        "Advances / deposits",
        "BANK",
        (
            one(
                "advances / deposits * 100",
                {
                    "advances": ref("BALANCE_SHEET.advances"),
                    "deposits": ref("BALANCE_SHEET.deposits"),
                },
            ),
        ),
        "percent",
        POSITION_TYPES,
        profile="BANK",
    ),
    # ---- returns (annual only: a quarter's return is not comparable without annualising) -------
    Metric(
        "RETURN.roe_closing",
        "Return on equity (closing equity)",
        "RETURN",
        (one("profit / equity * 100", {"profit": ref(ATTR), "equity": ref(EQ)}),),
        "percent",
        YEARLY,
    ),
    Metric(
        "RETURN.roe_average",
        "Return on equity (average of opening and closing equity)",
        "RETURN",
        (
            one(
                "profit / ((equity + opening) / 2) * 100",
                {"profit": ref(ATTR), "equity": ref(EQ), "opening": ref(EQ, shift=12)},
            ),
        ),
        "percent",
        YEARLY,
        spans=True,
    ),
    Metric(
        "RETURN.roa_closing",
        "Return on assets (closing assets)",
        "RETURN",
        (one("profit / assets * 100", {"profit": ref(ATTR), "assets": ref(TA)}),),
        "percent",
        YEARLY,
    ),
    Metric(
        "RETURN.roa_average",
        "Return on assets (average of opening and closing assets)",
        "RETURN",
        (
            one(
                "profit / ((assets + opening) / 2) * 100",
                {"profit": ref(ATTR), "assets": ref(TA), "opening": ref(TA, shift=12)},
            ),
        ),
        "percent",
        YEARLY,
        spans=True,
    ),
    # ---- balance-sheet structure -----------------------------------------------------------
    Metric(
        "LEVERAGE.equity_multiplier",
        "Total assets / owners' equity",
        "LEVERAGE",
        (one("assets / equity", {"assets": ref(TA), "equity": ref(EQ)}),),
        "x",
        POSITION_TYPES,
    ),
    Metric(
        "LEVERAGE.debt_to_equity",
        "Borrowings / owners' equity",
        "LEVERAGE",
        (one("debt / equity", {"debt": ref("BALANCE_SHEET.borrowings"), "equity": ref(EQ)}),),
        "x",
        POSITION_TYPES,
        profile="NONFIN",
        anchor=(EQ,),
    ),
    Metric(
        "LIQUIDITY.current_ratio",
        "Current assets / current liabilities",
        "LIQUIDITY",
        (
            one(
                "assets / liabilities",
                {
                    "assets": ref("BALANCE_SHEET.total_current_assets"),
                    "liabilities": ref("BALANCE_SHEET.total_current_liabilities"),
                },
            ),
        ),
        "x",
        POSITION_TYPES,
        profile="NONFIN",
        anchor=(EQ,),
    ),
    # ---- cash flow (non-financial companies) -----------------------------------------------
    Metric(
        "CASH.free_cash_flow",
        "Operating cash flow less capital expenditure",
        "CASH",
        (
            one(
                "cfo + capex",
                {
                    "cfo": ref("CASH_FLOW.net_cash_from_operating_activities"),
                    "capex": ref("CASH_FLOW.capital_expenditure"),
                },
            ),
        ),
        "inr",
        FLOW_TYPES,
        profile="NONFIN",
        requires=(("capex", "<=", 0),),
        anchor=("INCOME.net_profit",),
    ),
    Metric(
        "CASH.cfo_to_net_profit",
        "Operating cash flow / net profit",
        "CASH",
        (
            one(
                "cfo / profit",
                {"cfo": ref("CASH_FLOW.net_cash_from_operating_activities"), "profit": ref(ATTR)},
            ),
        ),
        "x",
        FLOW_TYPES,
        profile="NONFIN",
        requires=(("profit", ">", 0),),
        anchor=("INCOME.net_profit",),
    ),
    # ---- trailing four quarters -----------------------------------------------------------
    *_ttm_both("TTM.total_income", "Total income, trailing twelve months", TOT),
    *_ttm_both(
        "TTM.revenue", "Revenue from operations, trailing twelve months", REV, profile="NONFIN"
    ),
    *_ttm_both("TTM.net_profit", "Net profit attributable to owners, trailing twelve months", ATTR),
    *_ttm_both(
        "TTM.eps_basic",
        "Basic EPS, trailing twelve months",
        EPS,
        note="built from EPS figures; changes in share count make this approximate",
    ),
)

# On a consolidated bank, "other income" and "operating expenses" include subsidiaries' businesses
# (insurance premiums and claims, broking, asset management), so ratios built on them differ a lot
# from the bank's own (standalone) ratios.
BANK_GROUP_CAVEAT = {
    "RATIO.cost_to_income",
    "MARGIN.ppop_margin",
    "MARGIN.other_income_share",
    "MARGIN.net_profit_to_income",
    "GROWTH.total_income_yoy",
    "GROWTH.total_income_qoq",
    "GROWTH.total_income_cagr_3y",
    "TTM.total_income",
}
BANK_GROUP_NOTE = (
    "consolidated bank statements include subsidiaries' businesses (for example insurance premiums "
    "and claims) in income and expenses; compare with the standalone figure for the bank itself"
)

BY_CODE = {m.code: m for m in CATALOGUE}

__all__ = [
    "BANK_GROUP_CAVEAT",
    "BANK_GROUP_NOTE",
    "BY_CODE",
    "CATALOGUE",
    "Metric",
    "Ref",
    "Variant",
]
