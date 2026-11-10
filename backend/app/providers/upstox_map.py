"""Upstox statement line names -> the canonical keys the calculation engine reads.

Only lines whose meaning was checked against values extracted from the companies' own filings
(same company, period, basis) are mapped. The check is recorded in docs/ARCHITECTURE.md decision 60:
every mapping below agreed with the document-extracted value on every pair that could be compared,
except those marked "usually", which agreed on most pairs and differ where the two sources define
the line differently (ITC: gross vs net of excise duty). The "usually" lines are therefore
DEFINITIONAL: the sync files them under the canonical key only for a statement/period/basis that no
filing covers, so the two sources are never blended inside one statement. The others are EXACT and
are always filed canonically, which lets the evidence engine record that a filing and Upstox agree.

Not mapped on purpose: "Equity Capital" is Upstox's name for TOTAL equity (it matched
BALANCE_SHEET.total_equity on 14 of 14 pairs and share capital on none), so it maps to
total_equity, not to equity_share_capital. "Total Expenses", the working-capital lines and the
`summary.*` lines have no canonical counterpart and stay under their own names.
"""

UPSTOX_CODE = "UPSTOX_FUNDAMENTALS"

EXACT: dict[tuple[str, str], str] = {
    ("INCOME", "Profit Before Tax"): "profit_before_tax",
    ("INCOME", "Tax"): "tax_expense",
    ("INCOME", "Other Income"): "other_income",
    ("INCOME", "EPS - Basic"): "eps_basic",
    ("BALANCE_SHEET", "Total Assets"): "total_assets",
    ("BALANCE_SHEET", "Equity Capital"): "total_equity",
    ("BALANCE_SHEET", "Current Assets"): "total_current_assets",
    ("BALANCE_SHEET", "Current Liabilities"): "total_current_liabilities",
    ("CASH_FLOW", "Cash flow from Operations"): "net_cash_from_operating_activities",
}

DEFINITIONAL: dict[tuple[str, str], str] = {
    ("INCOME", "Revenue"): "revenue_from_operations",
    ("INCOME", "Total Revenue"): "total_income",
    ("INCOME", "Profit After Tax"): "net_profit",
}


def canonical_line(
    provider_code: str, statement: str, line_item: str, filing_covers_statement: bool
) -> str:
    """The key evidence is stored under; unmapped lines (and other providers) keep their name."""
    if provider_code != UPSTOX_CODE:
        return line_item
    key = (statement, line_item.strip())
    if key in EXACT:
        return EXACT[key]
    if key in DEFINITIONAL and not filing_covers_statement:
        return DEFINITIONAL[key]
    return line_item
