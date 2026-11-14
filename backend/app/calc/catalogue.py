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
    # minimum (materials + purchases + inventories) / revenue: below it the goods lines are
    # incidental (a services company's small stock in trade) and the figure describes nothing
    goods_share_min: float | None = None
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
GROSS = "INCOME.gross_profit"
# below this share of revenue, goods are incidental (a services company's small stock in trade):
# gross profit, inventory days and payable days would describe nothing
GOODS_SHARE_MIN = 0.1
RECEIVABLES = "BALANCE_SHEET.current_trade_receivables"
INVENTORIES = "BALANCE_SHEET.inventories"
PAYABLES = "BALANCE_SHEET.current_trade_payables"
DEBT = "LEVERAGE.total_borrowings"
CASH_BS = "BALANCE_SHEET.cash_and_cash_equivalents"
CFO_REF = ref("CASH_FLOW.net_cash_from_operating_activities")
CAPEX_REF = ref("CASH_FLOW.capital_expenditure")


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
    # Gross profit = revenue from operations less the cost of goods: raw materials consumed,
    # purchases of stock in trade and the change in inventories, as printed. Only companies that
    # print those lines have it (manufacturers and traders); a services company, which has no cost
    # of goods line, has none, rather than a figure invented from "other expenses".
    Metric(
        GROSS,
        "Revenue from operations less cost of goods (gross profit)",
        "DERIVED",
        (
            one(
                "revenue - materials - purchases - inventories",
                {
                    "revenue": ref(REV),
                    "materials": ref("INCOME.cost_of_materials_consumed"),
                    "purchases": ref("INCOME.purchases_of_stock_in_trade"),
                    "inventories": ref("INCOME.changes_in_inventories"),
                },
                note="cost of goods = materials consumed + purchases of stock in trade + change in "
                "inventories, each as printed",
            ),
            one(
                "revenue - materials - inventories",
                {
                    "revenue": ref(REV),
                    "materials": ref("INCOME.cost_of_materials_consumed"),
                    "inventories": ref("INCOME.changes_in_inventories"),
                },
                note="manufacturer: materials consumed + change in inventories (no stock in trade)",
            ),
            one(
                "revenue - purchases - inventories",
                {
                    "revenue": ref(REV),
                    "purchases": ref("INCOME.purchases_of_stock_in_trade"),
                    "inventories": ref("INCOME.changes_in_inventories"),
                },
                note="trader: purchases of stock in trade + change in inventories",
            ),
            one(
                "revenue - materials",
                {"revenue": ref(REV), "materials": ref("INCOME.cost_of_materials_consumed")},
                note="materials consumed only: no inventory line is printed",
            ),
        ),
        "inr",
        ALL_TYPES,
        profile="NONFIN",
        goods_share_min=GOODS_SHARE_MIN,
        anchor=("INCOME.cost_of_materials_consumed", "INCOME.purchases_of_stock_in_trade"),
    ),
    Metric(
        "MARGIN.gross_margin",
        "Gross margin (on revenue from operations)",
        "MARGIN",
        (one("gross / revenue * 100", {"gross": ref(GROSS), "revenue": ref(REV)}),),
        "percent",
        ALL_TYPES,
        profile="NONFIN",
        anchor=(GROSS,),
    ),
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
    _growth(
        "GROWTH.ebitda_yoy",
        "EBITDA growth, year on year",
        EBITDA,
        profile="NONFIN",
        positive=True,
    ),
    _growth("GROWTH.ebit_yoy", "EBIT growth, year on year", EBIT, profile="NONFIN", positive=True),
    _growth(
        "GROWTH.gross_profit_yoy",
        "Gross profit growth, year on year",
        GROSS,
        profile="NONFIN",
        positive=True,
    ),
    _cagr("GROWTH.revenue_cagr_3y", "Revenue from operations CAGR, 3 years", REV),
    _cagr("GROWTH.ebitda_cagr_3y", "EBITDA CAGR, 3 years", EBITDA),
    _cagr("GROWTH.gross_profit_cagr_3y", "Gross profit CAGR, 3 years", GROSS),
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
    Metric(
        "EFFICIENCY.asset_turnover",
        "Total income / average total assets (opening and closing)",
        "EFFICIENCY",
        (
            one(
                "income / ((assets + opening) / 2)",
                {
                    "income": ref("INCOME.total_income"),
                    "assets": ref(TA),
                    "opening": ref(TA, shift=12),
                },
            ),
        ),
        "x",
        YEARLY,
        profile="NONFIN",
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
        DEBT,
        "Total borrowings (non-current + current)",
        "LEVERAGE",
        (
            one(
                "debt",
                {"debt": ref("BALANCE_SHEET.borrowings")},
                note="borrowings as one printed line; lease liabilities are not included",
            ),
            one(
                "non_current + current",
                {
                    "non_current": ref("BALANCE_SHEET.non_current_borrowings"),
                    "current": ref("BALANCE_SHEET.current_borrowings"),
                },
                note="non-current + current borrowings as printed; no lease liabilities",
            ),
        ),
        "inr",
        POSITION_TYPES,
        profile="NONFIN",
        anchor=("BALANCE_SHEET.borrowings", "BALANCE_SHEET.non_current_borrowings"),
    ),
    Metric(
        "LEVERAGE.debt_to_equity",
        "Borrowings / owners' equity",
        "LEVERAGE",
        (one("debt / equity", {"debt": ref(DEBT), "equity": ref(EQ)}),),
        "x",
        POSITION_TYPES,
        profile="NONFIN",
        anchor=(DEBT,),
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
    # ---- working capital (annual: days are measured on a year's revenue or cost of goods) -------
    Metric(
        "WORKING_CAPITAL.receivable_days",
        "Receivable days (closing trade receivables / revenue x 365)",
        "WORKING_CAPITAL",
        (
            one(
                "receivables / revenue * 365",
                {"receivables": ref(RECEIVABLES), "revenue": ref(REV)},
                note="current trade receivables",
            ),
        ),
        "days",
        YEARLY,
        profile="NONFIN",
        requires=(("revenue", ">", 0),),
        anchor=(RECEIVABLES,),
    ),
    Metric(
        "WORKING_CAPITAL.inventory_days",
        "Inventory days (closing inventories / cost of goods x 365)",
        "WORKING_CAPITAL",
        (
            one(
                "inventories / (revenue - gross) * 365",
                {"inventories": ref(INVENTORIES), "revenue": ref(REV), "gross": ref(GROSS)},
                note="cost of goods = revenue from operations - gross profit, as printed",
            ),
        ),
        "days",
        YEARLY,
        profile="NONFIN",
        requires=(("revenue", ">", 0),),
        anchor=(INVENTORIES,),
    ),
    Metric(
        "WORKING_CAPITAL.payable_days",
        "Payable days (closing trade payables / cost of goods x 365)",
        "WORKING_CAPITAL",
        (
            one(
                "payables / (revenue - gross) * 365",
                {"payables": ref(PAYABLES), "revenue": ref(REV), "gross": ref(GROSS)},
                note="trade payables as one line; cost of goods = revenue - gross profit",
            ),
            one(
                "(msme + others) / (revenue - gross) * 365",
                {
                    "msme": ref("BALANCE_SHEET.trade_payables_msme"),
                    "others": ref("BALANCE_SHEET.trade_payables_others"),
                    "revenue": ref(REV),
                    "gross": ref(GROSS),
                },
                note="trade payables = micro/small enterprises + others, as printed separately",
            ),
        ),
        "days",
        YEARLY,
        profile="NONFIN",
        requires=(("revenue", ">", 0),),
        anchor=(PAYABLES, "BALANCE_SHEET.trade_payables_others"),
    ),
    Metric(
        "WORKING_CAPITAL.operating_cycle_days",
        "Working capital (operating) cycle: inventory days + receivable days",
        "WORKING_CAPITAL",
        (
            one(
                "inventory + receivable",
                {
                    "inventory": ref("WORKING_CAPITAL.inventory_days"),
                    "receivable": ref("WORKING_CAPITAL.receivable_days"),
                },
            ),
        ),
        "days",
        YEARLY,
        profile="NONFIN",
        anchor=("WORKING_CAPITAL.inventory_days",),
    ),
    Metric(
        "WORKING_CAPITAL.cash_conversion_cycle_days",
        "Cash conversion cycle: inventory days + receivable days - payable days",
        "WORKING_CAPITAL",
        (
            one(
                "inventory + receivable - payable",
                {
                    "inventory": ref("WORKING_CAPITAL.inventory_days"),
                    "receivable": ref("WORKING_CAPITAL.receivable_days"),
                    "payable": ref("WORKING_CAPITAL.payable_days"),
                },
            ),
        ),
        "days",
        YEARLY,
        profile="NONFIN",
        anchor=("WORKING_CAPITAL.inventory_days",),
    ),
    # ---- financial health --------------------------------------------------------------------
    Metric(
        "LIQUIDITY.quick_ratio",
        "(Current assets - inventories) / current liabilities",
        "LIQUIDITY",
        (
            one(
                "(assets - inventories) / liabilities",
                {
                    "assets": ref("BALANCE_SHEET.total_current_assets"),
                    "inventories": ref(INVENTORIES),
                    "liabilities": ref("BALANCE_SHEET.total_current_liabilities"),
                },
            ),
        ),
        "x",
        POSITION_TYPES,
        profile="NONFIN",
        requires=(("liabilities", ">", 0),),
        anchor=(INVENTORIES,),
    ),
    Metric(
        "LEVERAGE.net_debt",
        "Net debt: borrowings - cash and liquid investments",
        "LEVERAGE",
        (
            one(
                "debt - cash - bank - investments",
                {
                    "debt": ref(DEBT),
                    "cash": ref(CASH_BS),
                    "bank": ref("BALANCE_SHEET.bank_balances_other_than_cash_equivalents"),
                    "investments": ref("BALANCE_SHEET.current_investments"),
                },
                note="cash, other bank balances and current investments deducted",
            ),
            one(
                "debt - cash - investments",
                {
                    "debt": ref(DEBT),
                    "cash": ref(CASH_BS),
                    "investments": ref("BALANCE_SHEET.current_investments"),
                },
                note="cash and current investments deducted (no other bank balances printed)",
            ),
            one(
                "debt - cash - bank",
                {
                    "debt": ref(DEBT),
                    "cash": ref(CASH_BS),
                    "bank": ref("BALANCE_SHEET.bank_balances_other_than_cash_equivalents"),
                },
                note="cash and other bank balances deducted (no current investments printed)",
            ),
            one(
                "debt - cash",
                {"debt": ref(DEBT), "cash": ref(CASH_BS)},
                note="cash and cash equivalents deducted only",
            ),
        ),
        "inr",
        POSITION_TYPES,
        profile="NONFIN",
        anchor=(DEBT,),
    ),
    Metric(
        "LEVERAGE.net_debt_to_equity",
        "Net debt / owners' equity",
        "LEVERAGE",
        (one("net_debt / equity", {"net_debt": ref("LEVERAGE.net_debt"), "equity": ref(EQ)}),),
        "x",
        POSITION_TYPES,
        profile="NONFIN",
        requires=(("equity", ">", 0),),
        anchor=("LEVERAGE.net_debt",),
    ),
    Metric(
        "LEVERAGE.net_debt_to_ebitda",
        "Net debt / EBITDA",
        "LEVERAGE",
        (one("net_debt / ebitda", {"net_debt": ref("LEVERAGE.net_debt"), "ebitda": ref(EBITDA)}),),
        "x",
        YEARLY,
        profile="NONFIN",
        requires=(("ebitda", ">", 0),),
        anchor=("LEVERAGE.net_debt",),
    ),
    Metric(
        "LEVERAGE.debt_to_ebitda",
        "Borrowings / EBITDA",
        "LEVERAGE",
        (one("debt / ebitda", {"debt": ref(DEBT), "ebitda": ref(EBITDA)}),),
        "x",
        YEARLY,
        profile="NONFIN",
        requires=(("ebitda", ">", 0),),
        anchor=(EBITDA,),
    ),
    Metric(
        "RATIO.ebitda_interest_cover",
        "EBITDA / finance costs",
        "LEVERAGE",
        (one("ebitda / finance", {"ebitda": ref(EBITDA), "finance": ref(FIN)}),),
        "x",
        ALL_TYPES,
        profile="NONFIN",
        requires=(("finance", ">", 0),),
        anchor=(EBITDA,),
    ),
    Metric(
        "LIQUIDITY.cash_to_short_term_debt",
        "Cash and liquid investments / current borrowings",
        "LIQUIDITY",
        (
            one(
                "(cash + investments) / current_debt",
                {
                    "cash": ref(CASH_BS),
                    "investments": ref("BALANCE_SHEET.current_investments"),
                    "current_debt": ref("BALANCE_SHEET.current_borrowings"),
                },
            ),
            one(
                "cash / current_debt",
                {"cash": ref(CASH_BS), "current_debt": ref("BALANCE_SHEET.current_borrowings")},
                note="no current investments printed",
            ),
        ),
        "x",
        POSITION_TYPES,
        profile="NONFIN",
        requires=(("current_debt", ">", 0),),
        anchor=("BALANCE_SHEET.current_borrowings",),
    ),
    Metric(
        "LIQUIDITY.cfo_to_debt",
        "Operating cash flow / borrowings",
        "LIQUIDITY",
        (one("cfo / debt", {"cfo": CFO_REF, "debt": ref(DEBT)}),),
        "x",
        YEARLY,
        profile="NONFIN",
        requires=(("debt", ">", 0),),
        anchor=(DEBT,),
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
    # ---- cash conversion, capital expenditure and earnings quality (non-financial companies) ---
    Metric(
        "CASH.cfo_margin",
        "Operating cash flow / revenue from operations",
        "CASH",
        (one("cfo / revenue * 100", {"cfo": CFO_REF, "revenue": ref(REV)}),),
        "percent",
        FLOW_TYPES,
        profile="NONFIN",
        requires=(("revenue", ">", 0),),
        anchor=("INCOME.net_profit",),
    ),
    Metric(
        "CASH.cfo_to_ebitda",
        "Operating cash flow / EBITDA (cash conversion)",
        "CASH",
        (one("cfo / ebitda", {"cfo": CFO_REF, "ebitda": ref(EBITDA)}),),
        "x",
        FLOW_TYPES,
        profile="NONFIN",
        requires=(("ebitda", ">", 0),),
        anchor=(EBITDA,),
    ),
    Metric(
        "CASH.fcf_margin",
        "Free cash flow / revenue from operations",
        "CASH",
        (
            one(
                "(cfo + capex) / revenue * 100",
                {"cfo": CFO_REF, "capex": CAPEX_REF, "revenue": ref(REV)},
            ),
        ),
        "percent",
        FLOW_TYPES,
        profile="NONFIN",
        requires=(("capex", "<=", 0), ("revenue", ">", 0)),
        anchor=("CASH_FLOW.capital_expenditure",),
    ),
    Metric(
        "CASH.fcf_to_net_profit",
        "Free cash flow / net profit",
        "CASH",
        (
            one(
                "(cfo + capex) / profit",
                {"cfo": CFO_REF, "capex": CAPEX_REF, "profit": ref(ATTR)},
            ),
        ),
        "x",
        FLOW_TYPES,
        profile="NONFIN",
        requires=(("capex", "<=", 0), ("profit", ">", 0)),
        anchor=("CASH_FLOW.capital_expenditure",),
    ),
    Metric(
        "CASH.capex_to_revenue",
        "Capital expenditure / revenue from operations",
        "CASH",
        (one("-capex / revenue * 100", {"capex": CAPEX_REF, "revenue": ref(REV)}),),
        "percent",
        FLOW_TYPES,
        profile="NONFIN",
        requires=(("capex", "<=", 0), ("revenue", ">", 0)),
        anchor=("CASH_FLOW.capital_expenditure",),
    ),
    Metric(
        "CASH.capex_to_cfo",
        "Capital expenditure / operating cash flow",
        "CASH",
        (one("-capex / cfo * 100", {"capex": CAPEX_REF, "cfo": CFO_REF}),),
        "percent",
        FLOW_TYPES,
        profile="NONFIN",
        requires=(("capex", "<=", 0), ("cfo", ">", 0)),
        anchor=("CASH_FLOW.capital_expenditure",),
    ),
    Metric(
        "CASH.capex_to_depreciation",
        "Capital expenditure / depreciation and amortisation",
        "CASH",
        (
            one(
                "-capex / depreciation",
                {"capex": CAPEX_REF, "depreciation": ref("INCOME.depreciation_and_amortisation")},
                note="depreciation from the income statement",
            ),
            one(
                "-capex / depreciation",
                {
                    "capex": CAPEX_REF,
                    "depreciation": ref("CASH_FLOW.depreciation_and_amortisation"),
                },
                note="depreciation added back in the cash flow statement",
            ),
        ),
        "x",
        FLOW_TYPES,
        profile="NONFIN",
        requires=(("capex", "<=", 0), ("depreciation", ">", 0)),
        anchor=("CASH_FLOW.capital_expenditure",),
    ),
    Metric(
        "ACCRUAL.accruals",
        "Accruals: net profit less operating cash flow",
        "ACCRUAL",
        (one("profit - cfo", {"profit": ref(ATTR), "cfo": CFO_REF}),),
        "inr",
        FLOW_TYPES,
        profile="NONFIN",
        anchor=("INCOME.net_profit",),
    ),
    Metric(
        "ACCRUAL.accrual_ratio",
        "Accrual ratio: (net profit - operating cash flow) / average total assets",
        "ACCRUAL",
        (
            one(
                "(profit - cfo) / ((assets + opening) / 2) * 100",
                {
                    "profit": ref(ATTR),
                    "cfo": CFO_REF,
                    "assets": ref(TA),
                    "opening": ref(TA, shift=12),
                },
                note="average of opening and closing total assets",
            ),
            one(
                "(profit - cfo) / assets * 100",
                {"profit": ref(ATTR), "cfo": CFO_REF, "assets": ref(TA)},
                note="closing total assets: no opening balance sheet is stored",
            ),
        ),
        "percent",
        ("ANNUAL",),
        profile="NONFIN",
        spans=True,
        requires=(("assets", ">", 0),),
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
