"""Canonical line items and the printed labels that map to them.

Covers the two formats Indian listed companies use:
  * Ind AS / Schedule III (most companies): revenue from operations, other income, ... ;
  * the Banking Regulation Act Third Schedule (banks): interest earned, interest expended, ... .

Matching is deterministic: a label is normalised (case, enumeration like "1." or "(a)", formula
references like "(I+II)", "/(loss)" variants, currency markers) and must match a pattern in FULL.
Unknown labels are left unmatched and reported, never forced onto the nearest item.

Some labels mean different things in different places:
  * balance-sheet items such as "Borrowings" appear under both current and non-current
    liabilities; they become `non_current_borrowings` / `current_borrowings` from the section
    heading above them, or plain `borrowings` when the statement has no such sections (banks);
  * "Owners of the Company" / "Non-controlling interests" in the income statement belong to the
    profit line or the comprehensive-income line above them.
"""

import re
import unicodedata
from dataclasses import dataclass

INCOME, BALANCE_SHEET, CASH_FLOW = "INCOME", "BALANCE_SHEET", "CASH_FLOW"


@dataclass(frozen=True)
class Item:
    key: str
    statement: str
    patterns: tuple[str, ...]
    kind: str = "amount"  # amount | per_share
    sectional: bool = False  # balance sheet: prefix with current / non_current from context


def _i(
    key: str, statement: str, *patterns: str, kind: str = "amount", sectional: bool = False
) -> Item:
    return Item(key, statement, patterns, kind, sectional)


ITEMS: tuple[Item, ...] = (
    # ---------------------------------------------------------------- income statement
    _i(
        "revenue_from_operations",
        INCOME,
        r"(total )?revenue from operations( \(net\))?( \(gross\))?",
        r"(total )?income from operations( \(net\))?",
        r"net sales( ?/ ?income from operations)?",
        r"revenue from contracts? with customers",
    ),
    _i("interest_earned", INCOME, r"interest earned"),
    _i("other_income", INCOME, r"other income( \(net\))?"),
    _i("total_income", INCOME, r"total income( \(net\))?", r"total revenue"),
    _i("interest_expended", INCOME, r"interest expended"),
    _i("operating_expenses", INCOME, r"operating expenses"),
    _i("cost_of_materials_consumed", INCOME, r"cost of (raw )?materials? consumed"),
    _i("purchases_of_stock_in_trade", INCOME, r"purchases? of stock[- ]in[- ]trade( goods)?"),
    _i("changes_in_inventories", INCOME, r"changes? in inventor(y|ies) of .*"),
    _i(
        "employee_benefits_expense",
        INCOME,
        r"employee benefits? expenses?",
        r"employees? costs?",
        r"payments? to and provisions? for employees",
    ),
    _i("finance_costs", INCOME, r"finance costs?"),
    _i(
        "depreciation_and_amortisation",
        INCOME,
        r"depreciation(,)?( depletion)?( and| ?/)? amorti[sz]ation( expenses?)?",
        r"depreciation and amorti[sz]ation expenses?",
        r"depreciation ?/ ?amorti[sz]ation( and depletion)?( expenses?)?",
    ),
    _i("excise_duty", INCOME, r"excise duty( on sale of goods)?"),
    _i("other_expenses", INCOME, r"other expenses", r"other operating expenses"),
    _i("total_expenses", INCOME, r"total expenses", r"total expenditure"),
    _i(
        "total_expenditure_excl_provisions",
        INCOME,
        r"total expenditure \(?excluding provisions and contingencies\)?",
    ),
    _i(
        "operating_profit_before_provisions",
        INCOME,
        r"operating profit( before provisions and contingencies)?",
    ),
    # Two different items that look alike. Results filings print "Provisions (other than tax) and
    # contingencies"; banks' annual accounts (Form B) print "Provisions and contingencies", which
    # INCLUDES tax. They must never share a key (an ICICI Bank cross-check found 5,380 vs 21,452).
    _i(
        "provisions_excluding_tax",
        INCOME,
        r"provisions \(other than tax\) and contingencies( \(net\))?",
    ),
    _i(
        "provisions_and_contingencies",
        INCOME,
        r"provisions and contingencies( \(net\))?",
    ),
    _i(
        "profit_before_exceptional_items_and_tax",
        INCOME,
        r"profit before exceptional items and tax",
        r"profit before share of .* exceptional items and tax",
        r"(net )?profit from ordinary activities before exceptional items and tax",
    ),
    _i(
        "share_of_profit_of_associates_and_jvs",
        INCOME,
        r"share (in|of) (net )?profit(s)? (of|from|in) .*(associates?|joint ventures?).*",
    ),
    # Banks' consolidated results deduct the minority shareholders' share after tax.
    _i(
        "minority_share_of_profit",
        INCOME,
        r"share of (net )?profit (of|attributable to) (minority|non[- ]controlling) (shareholders|interests?)",
        r"minority interests?",
    ),
    _i("exceptional_items", INCOME, r"exceptional items?( \(net\))?"),
    _i(
        "profit_before_tax",
        INCOME,
        r"profit before tax( for the (period|year|quarter))?",
        r"(net )?profit from ordinary activities before tax( and minority interests?)?",
    ),
    _i("current_tax", INCOME, r"current tax"),
    _i("deferred_tax", INCOME, r"deferred tax( charge| credit)?"),
    _i("tax_expense", INCOME, r"(total )?(income )?tax expenses?( \(net\))?"),
    _i(
        "net_profit",
        INCOME,
        r"(net )?profit for the (period|year|quarter)",
        r"net profit",
        # Banks' consolidated accounts (Indian GAAP): the profit line printed after minority
        # interest; it equals the results' "net profit for the period".
        r"consolidated (net )?profit for the (year|period) attributable to the group",
        # Groups that print associates after tax (e.g. Reliance): the final profit line.
        r"(net )?profit after tax and share of (net )?profit (of|in|from) associates and joint ventures",
    ),
    # "Profit after tax" as printed. Where a share of associates follows it, it is NOT the final
    # profit, so it is kept apart from net_profit; checks confirm which it is.
    _i(
        "profit_after_tax",
        INCOME,
        r"(net )?profit after tax( for the (period|year|quarter))?",
    ),
    # Banks: profit after tax before extraordinary items is a separate line from the final profit.
    _i(
        "net_profit_from_ordinary_activities",
        INCOME,
        r"(net )?profit from ordinary activities after tax",
    ),
    _i("extraordinary_items", INCOME, r"extraordinary items( \(net of tax( expense)?\))?"),
    # Banks' consolidated annual accounts build profit in steps (associates, then minority).
    _i(
        "net_profit_before_associates_and_minority",
        INCOME,
        r"net profit for the (year|period) \(before share in profit of associates and minority interests?\)",
    ),
    _i(
        "net_profit_before_minority",
        INCOME,
        r"net profit for the (year|period) before deducting minority interests?",
    ),
    _i(
        "other_comprehensive_income",
        INCOME,
        r"(total )?other comprehensive income( for the (period|year))?( \(net of tax\))?",
    ),
    _i(
        "total_comprehensive_income",
        INCOME,
        r"total comprehensive income( for the (period|year|quarter))?",
    ),
    _i("paid_up_equity_share_capital", INCOME, r"paid[- ]?up equity share capital.*"),
    _i(
        "eps_basic",
        INCOME,
        r"basic( eps)?",
        r"basic eps before and after extraordinary items,? net of tax( expense)?",
        r"basic earnings per share",
        r"earnings per share basic",
        kind="per_share",
    ),
    _i(
        "eps_diluted",
        INCOME,
        r"diluted( eps)?",
        r"diluted eps before and after extraordinary items,? net of tax( expense)?",
        r"diluted earnings per share",
        r"earnings per share diluted",
        kind="per_share",
    ),
    # ---------------------------------------------------------------- balance sheet
    _i("property_plant_and_equipment", BALANCE_SHEET, r"property, plant and equipment"),
    _i("total_non_current_assets", BALANCE_SHEET, r"total non[- ]current assets"),
    _i("inventories", BALANCE_SHEET, r"inventories"),
    _i("trade_receivables", BALANCE_SHEET, r"trade receivables", sectional=True),
    _i("cash_and_cash_equivalents", BALANCE_SHEET, r"cash and cash equivalents"),
    _i("total_current_assets", BALANCE_SHEET, r"total current assets"),
    _i(
        "cash_and_balances_with_rbi",
        BALANCE_SHEET,
        r"cash and balances? with reserve bank of india",
    ),
    _i(
        "balances_with_banks",
        BALANCE_SHEET,
        r"balances? with banks and money at call and short notice",
    ),
    _i("investments", BALANCE_SHEET, r"investments", sectional=True),
    _i("advances", BALANCE_SHEET, r"advances"),
    _i("fixed_assets", BALANCE_SHEET, r"fixed assets"),
    _i("goodwill_on_consolidation", BALANCE_SHEET, r"goodwill on consolidation"),
    _i("other_assets", BALANCE_SHEET, r"other assets"),
    _i("total_assets", BALANCE_SHEET, r"total assets"),
    _i("equity_share_capital", BALANCE_SHEET, r"equity share capital", r"share capital"),
    _i("capital", BALANCE_SHEET, r"capital"),
    _i(
        "employee_stock_options_outstanding",
        BALANCE_SHEET,
        r"employees? stock options?(/units)? outstanding",
    ),
    _i("reserves_and_surplus", BALANCE_SHEET, r"reserves and surplus"),
    _i("other_equity", BALANCE_SHEET, r"other equity"),
    _i(
        "equity_attributable_to_owners",
        BALANCE_SHEET,
        r"(total )?equity attributable to (the )?(owners|equity holders|shareholders) of the (company|parent|bank)",
    ),
    _i("non_controlling_interests", BALANCE_SHEET, r"non[- ]controlling interests?"),
    _i("minority_interest", BALANCE_SHEET, r"minority interests?"),
    _i("total_equity", BALANCE_SHEET, r"total equity"),
    _i("deposits", BALANCE_SHEET, r"deposits"),
    _i(
        "borrowings",
        BALANCE_SHEET,
        r"borrowings( \(includes? subordinated debt\))?",
        sectional=True,
    ),
    _i("policyholders_funds", BALANCE_SHEET, r"policyholders'? funds"),
    _i("trade_payables", BALANCE_SHEET, r"trade payables", sectional=True),
    _i("other_liabilities_and_provisions", BALANCE_SHEET, r"other liabilities and provisions"),
    _i("total_non_current_liabilities", BALANCE_SHEET, r"total non[- ]current liabilities"),
    _i("total_current_liabilities", BALANCE_SHEET, r"total current liabilities"),
    _i("total_liabilities", BALANCE_SHEET, r"total liabilities"),
    _i("total_equity_and_liabilities", BALANCE_SHEET, r"total equity and liabilities"),
    _i("total_capital_and_liabilities", BALANCE_SHEET, r"total capital and liabilities"),
    # ---------------------------------------------------------------- cash flow
    _i("cash_generated_from_operations", CASH_FLOW, r"cash generated from operations"),
    _i("income_taxes_paid", CASH_FLOW, r"(direct |income )?taxes paid.*"),
    _i(
        "net_cash_from_operating_activities",
        CASH_FLOW,
        r"net cash( flows?)?( generated| used| \(used\))?( from| in)? operating activities( \(a\))?",
    ),
    _i(
        "net_cash_from_investing_activities",
        CASH_FLOW,
        r"net cash( flows?)?( generated| used| \(used\))?( from| in)? investing activities( \(b\))?",
    ),
    _i(
        "net_cash_from_financing_activities",
        CASH_FLOW,
        r"net cash( flows?)?( generated| used| \(used\))?( from| in)? financing activities( \(c\))?",
    ),
    _i(
        "capital_expenditure",
        CASH_FLOW,
        r"(purchase|acquisition)s? of property, plant and equipment.*",
        r"purchase of fixed assets.*",
    ),
    _i("dividends_paid", CASH_FLOW, r"dividends? paid.*"),
    _i(
        "fx_effect_on_cash",
        CASH_FLOW,
        r"effect of exchange (rate )?(differences?|fluctuations?|changes?|rate changes?)( on .*)?",
    ),
    _i(
        "net_change_in_cash",
        CASH_FLOW,
        r"net (increase|decrease|change) in cash and cash equivalents?.*",
    ),
    _i(
        "cash_at_beginning",
        CASH_FLOW,
        r"cash and cash equivalents? at (the )?beginning of the (period|year).*",
        r"opening cash and cash equivalents",
    ),
    _i(
        "cash_at_end",
        CASH_FLOW,
        r"cash and cash equivalents? at (the )?end of the (period|year).*",
        r"closing cash and cash equivalents",
    ),
)

_COMPILED = [(item, [re.compile(p) for p in item.patterns]) for item in ITEMS]
BY_KEY = {item.key: item for item in ITEMS}

# Income-statement labels whose meaning depends on the anchor line above them.
_OWNERS = re.compile(
    r"((net )?profit |total comprehensive income )?(attributable to )?(the )?"
    r"(owners|equity holders|equity shareholders|shareholders) of the (company|parent|bank)"
)
_NCI = re.compile(
    r"((net )?profit |total comprehensive income )?(attributable to )?non[- ]controlling interests?"
)
_ANCHORS = {
    "net_profit": "net_profit",
    "other_comprehensive_income": "other_comprehensive_income",
    "total_comprehensive_income": "total_comprehensive_income",
}

# ---------------------------------------------------------------- label normalisation
# Leading enumeration: "1.", "(a)", "iv)", "1.2", or a bare serial number "6 " before the label.
_ENUM = re.compile(
    r"^(\(?([0-9]{1,2}|[ivx]{1,5}|[a-z])[\.\)]|\(([0-9]{1,2}|[ivx]{1,5}|[a-z])\)|[0-9]{1,2}\.[0-9]"
    r"|[0-9]{1,2}(?=\s)|(add|less)\s*:)\s*"
)
_TOKEN = r"(?:[0-9]{1,2}|[ivx]{1,5}|[a-z])"
_FORMULA = re.compile(
    r"\(\s*" + _TOKEN + r"(\s*[+\-=/]\s*" + _TOKEN + r")+\s*\)"
    r"|(\(\s*" + _TOKEN + r"\s*\)\s*[+\-]\s*)+\(\s*" + _TOKEN + r"\s*\)"
    r"|\(\s*" + _TOKEN + r"\s*\)\s*$"
)
_SLASH_ALT = re.compile(r"\s*/\s*\(\s*[a-z ]{2,20}\s*\)|\(\s*[a-z ]{2,20}\s*\)\s*/\s*")
_NOISE = re.compile(
    r"\((refer )?notes? [^)]*\)|\(\s*(in )?(₹|rs\.?|inr|`)[^)]*\)|\((not )?annuali[sz]ed\)"
    r"|\(face value[^)]*\)|\(of ₹[^)]*\)"
)


def normalize_label(label: str) -> str:
    text = unicodedata.normalize("NFKC", label).lower().replace("’", "'").replace("&", " and ")
    text = re.sub(r"[–—‒−]", "-", text)
    text = _NOISE.sub(" ", text)
    text = text.replace("₹", " ").replace("`", " ")
    text = re.sub(r"\brs\.?(?=\s|$)", " ", text)
    for _ in range(3):  # "I. (a) Revenue" -> "Revenue"
        stripped = _ENUM.sub("", text.strip())
        if stripped == text.strip():
            break
        text = stripped
    text = _FORMULA.sub(" ", text)
    text = _SLASH_ALT.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" :;,.-*")
    # trailing schedule / note reference printed beside the label: "Interest earned 13", "... 1A"
    text = re.sub(r"\s\d{1,2}[a-z]{0,2}$", "", text)
    return text


@dataclass
class Context:
    """State carried down a statement while rows are read top to bottom."""

    section: str | None = None  # "current" | "non_current" for balance sheets
    anchor: str | None = None  # last profit / comprehensive-income line in an income statement


_SECTION = re.compile(r"(non[- ]current|current) (assets|liabilities)")
_RESET_SECTION = re.compile(
    r"assets|equity and liabilities|capital and liabilities|liabilities|equity|"
    r"shareholders'? funds|equity and liabilities:?"
)


_ATTRIBUTION_HEADINGS = [
    (
        re.compile(r"(net )?profit( for the (period|year|quarter))?( is)? attributable to"),
        "net_profit",
    ),
    (
        re.compile(
            r"other comprehensive income( for the (period|year|quarter))?( is)? attributable to"
        ),
        "other_comprehensive_income",
    ),
    (
        re.compile(
            r"total comprehensive income( for the (period|year|quarter))?( is)? attributable to"
        ),
        "total_comprehensive_income",
    ),
]


def observe_heading(label: str, statement: str, ctx: Context) -> None:
    """Update context from a heading line (a line with a label but no values)."""
    norm = normalize_label(label)
    if statement == INCOME:
        for pattern, anchor in _ATTRIBUTION_HEADINGS:
            if pattern.fullmatch(norm):
                ctx.anchor = anchor
        return
    if statement != BALANCE_SHEET:
        return
    match = _SECTION.fullmatch(norm)
    if match:
        ctx.section = "non_current" if match.group(1).startswith("non") else "current"
    elif _RESET_SECTION.fullmatch(norm):
        ctx.section = None


def match_label(label: str, statement: str, ctx: Context) -> str | None:
    """Canonical key for a printed label in the given statement, or None if not recognised."""
    norm = normalize_label(label)
    if not norm:
        return None
    if statement == INCOME:
        owners, nci = _OWNERS.fullmatch(norm), _NCI.fullmatch(norm)
        if owners or nci:
            # an explicit prefix ("Profit attributable to owners ...") beats the context
            anchor = ctx.anchor
            if norm.startswith(("profit", "net profit")):
                anchor = "net_profit"
            elif norm.startswith("total comprehensive income"):
                anchor = "total_comprehensive_income"
            if anchor:
                return f"{anchor}_owners" if owners else f"{anchor}_non_controlling"
            return None
    for item, patterns in _COMPILED:
        if item.statement != statement:
            continue
        if any(p.fullmatch(norm) for p in patterns):
            if statement == INCOME and item.key in _ANCHORS:
                ctx.anchor = _ANCHORS[item.key]
            if item.sectional and ctx.section:
                return f"{ctx.section}_{item.key}"
            return item.key
    return None


def item_kind(key: str) -> str:
    """amount | per_share. Derived keys (e.g. net_profit_owners) are amounts."""
    item = BY_KEY.get(key)
    return item.kind if item else "amount"


def score_statement_types(labels: list[str]) -> dict[str, int]:
    """How many labels match each statement type's items (used when a page has no title)."""
    scores = {INCOME: 0, BALANCE_SHEET: 0, CASH_FLOW: 0}
    for label in labels:
        norm = normalize_label(label)
        for statement in scores:
            if any(
                item.statement == statement and any(p.fullmatch(norm) for p in patterns)
                for item, patterns in _COMPILED
            ):
                scores[statement] += 1
    return scores
