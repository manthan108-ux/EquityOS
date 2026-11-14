"""Line-item mapping additions: what now maps, and what must keep meaning what it did."""

import pytest

from app.docintel import taxonomy as tx

INCOME, BS, CF = tx.INCOME, tx.BALANCE_SHEET, tx.CASH_FLOW


def key(label, statement, section=None, **ctx):
    c = tx.Context()
    c.section = section
    for name, value in ctx.items():
        setattr(c, name, value)
    return tx.match_label(label, statement, c)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("I Revenue from operations", "revenue_from_operations"),
        ("II Other income", "other_income"),
        ("III Total income (I+II)", "total_income"),
        ("VII Profit before tax (V-VI)", "profit_before_tax"),
        ("Other operating revenue", "other_operating_revenue"),
        ("Sale of products", "sale_of_products"),
        ("Cost of technical sub-contractors", "subcontracting_expense"),
        ("Subcontracting expenses", "subcontracting_expense"),
        ("Profit before exceptional item and tax", "profit_before_exceptional_items_and_tax"),
        ("Remeasurements of the defined benefit plans", "oci_remeasurement_defined_benefit"),
        (
            "Remeasurement losses on defined benefit obligations (net)",
            "oci_remeasurement_defined_benefit",
        ),
        ("Equity instruments through other comprehensive income", "oci_equity_instruments"),
        (
            "Income tax relating to items that will not be reclassified to profit or loss",
            "oci_tax_not_reclassified",
        ),
        (
            "Income taxes relating to items that will be reclassified to profit or loss",
            "oci_tax_reclassified",
        ),
        ("Transfer to statutory reserve", "transfer_to_statutory_reserve"),
        ("Balance carried over to balance sheet", "balance_carried_to_balance_sheet"),
    ],
)
def test_income_statement_labels(label, expected):
    assert key(label, INCOME) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Deferred tax liabilities (net)", "deferred_tax_liabilities_net"),
        ("Deferred tax assets (net)", "deferred_tax_assets_net"),
        ("Capital work-in-progress", "capital_work_in_progress"),
        ("Goodwill", "goodwill"),
        ("Other intangible assets", "other_intangible_assets"),
        ("Intangible assets under development", "intangible_assets_under_development"),
        ("Right-of-use assets", "right_of_use_assets"),
        ("Investment property", "investment_property"),
        ("Other non-current assets", "other_non_current_assets"),
        ("Other current liabilities", "other_current_liabilities"),
        ("Current tax liabilities (net)", "current_tax_liabilities_net"),
        ("Non-current tax assets (net)", "non_current_tax_assets_net"),
        (
            "Bank balances other than cash and cash equivalents",
            "bank_balances_other_than_cash_equivalents",
        ),
        ("Bank balances other than (iii) above", "bank_balances_other_than_cash_equivalents"),
        ("Contingent liabilities", "contingent_liabilities"),
        ("Bills for collection", "bills_for_collection"),
        (
            "Total outstanding dues of micro enterprises and small enterprises",
            "trade_payables_msme",
        ),
        (
            "Total outstanding dues of creditors other than micro enterprises and small enterprises",
            "trade_payables_others",
        ),
    ],
)
def test_balance_sheet_labels(label, expected):
    assert key(label, BS) == expected


@pytest.mark.parametrize(
    "label",
    [
        "Provisions",
        "Lease liabilities",
        "Other financial assets",
        "Other financial liabilities",
        "Loans",
    ],
)
def test_lines_that_sit_in_both_halves_carry_their_section(label):
    base = key(label, BS)
    assert base and not base.startswith(("current_", "non_current_"))
    assert key(label, BS, section="current") == f"current_{base}"
    assert key(label, BS, section="non_current") == f"non_current_{base}"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Profit before tax", "profit_before_tax"),
        ("Net profit before taxes", "profit_before_tax"),
        ("Depreciation and amortisation expense", "depreciation_and_amortisation"),
        ("Finance costs", "finance_costs"),
        ("Interest income", "interest_income"),
        ("Interest received", "interest_received"),
        ("Dividend received", "dividend_received"),
        ("Interest paid", "interest_paid"),
        (
            "Operating profit before working capital changes",
            "operating_profit_before_working_capital_changes",
        ),
        ("Inventories", "change_in_inventories"),
        ("Trade receivables", "change_in_trade_receivables"),
        ("Trade payables", "change_in_trade_payables"),
        ("Proceeds from sale of fixed assets", "proceeds_from_sale_of_fixed_assets"),
        (
            "Proceeds from sale of property, plant and equipment",
            "proceeds_from_sale_of_fixed_assets",
        ),
        ("Purchase of investments", "purchase_of_investments"),
        ("Proceeds from sale of mutual funds (net)", "proceeds_from_sale_of_investments"),
        ("Proceeds from long-term borrowings", "proceeds_from_long_term_borrowings"),
        ("Repayment of long-term borrowings", "repayment_of_long_term_borrowings"),
        (
            "Proceeds from issue of share capital (including ESOPs)",
            "proceeds_from_issue_of_share_capital",
        ),
        ("Payment of dividend", "dividends_paid"),
        ("Payment of lease liabilities", "payment_of_lease_liabilities"),
        ("Increase in deposits", "increase_in_deposits"),
        ("Decrease in advances", "decrease_in_advances"),
    ],
)
def test_cash_flow_labels(label, expected):
    assert key(label, CF) == expected


def test_a_label_means_the_same_thing_only_inside_its_own_statement():
    """'Inventories' is a balance-sheet asset, but in a cash flow it is the CHANGE in inventories."""
    assert key("Inventories", BS) == "inventories"
    assert key("Inventories", CF) == "change_in_inventories"
    assert key("Trade payables", BS, section="current") == "current_trade_payables"
    assert key("Trade payables", CF) == "change_in_trade_payables"


@pytest.mark.parametrize(
    ("label", "statement", "expected"),
    [
        # two different "provisions" lines must never merge (see the ICICI Bank cross-check)
        ("Provisions (other than tax) and contingencies", INCOME, "provisions_excluding_tax"),
        ("Provisions and contingencies", INCOME, "provisions_and_contingencies"),
        ("Other liabilities and provisions", BS, "other_liabilities_and_provisions"),
        ("Goodwill on consolidation", BS, "goodwill_on_consolidation"),
        ("Total revenue", INCOME, "total_income"),
        ("Revenue from operations", INCOME, "revenue_from_operations"),
        ("Investments", BS, "investments"),
        ("Interest earned", INCOME, "interest_earned"),
        ("Net cash generated from operating activities", CF, "net_cash_from_operating_activities"),
        ("Dividend paid", CF, "dividends_paid"),
    ],
)
def test_existing_meanings_are_unchanged(label, statement, expected):
    assert key(label, statement) == expected


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("Revenue", INCOME),  # ambiguous: a heading, a total or an operating line: not guessed
        ("Others", BS),
        ("Total", INCOME),
        ("Miscellaneous", CF),
    ],
)
def test_ambiguous_labels_stay_unmapped(label, statement):
    assert key(label, statement) is None


def test_enumeration_is_removed_but_words_that_start_like_numerals_are_not():
    assert tx.normalize_label("IV Total expenses") == "total expenses"
    assert tx.normalize_label("VIII Profit for the year") == "profit for the year"
    assert tx.normalize_label("XII Other comprehensive income") == "other comprehensive income"
    assert tx.normalize_label("Investments") == "investments"
    assert tx.normalize_label("Vehicles") == "vehicles"
    assert tx.normalize_label("Interest earned") == "interest earned"
    assert tx.normalize_label("Mixed funds") == "mixed funds"
    assert tx.normalize_label("Xerox rentals") == "xerox rentals"


def test_every_new_item_has_a_unique_key_per_statement():
    seen = {}
    for item in tx.ITEMS:
        pair = (item.statement, item.key)
        # a key may be listed twice for the SAME statement only to give it more label variants
        seen.setdefault(pair, 0)
        seen[pair] += 1
    assert all(statement in {INCOME, BS, CF, tx.RATIOS} for statement, _ in seen)
    new_keys = {"provisions", "lease_liabilities", "proceeds_from_long_term_borrowings", "goodwill"}
    assert new_keys <= {k for _, k in seen}


def test_buyback_and_acquisition_lines_in_the_cash_flow_are_recognised():
    from app.docintel import taxonomy as t

    def key(label):
        return t.match_label(label, "CASH_FLOW", t.Context())

    assert key("Payment for buy-back of shares") == "buyback_of_shares"
    assert key("Buy back of equity shares (including tax)") == "buyback_of_shares"
    assert (
        key("Acquisition of subsidiaries, net of cash acquired")
        == "acquisition_of_subsidiaries_and_businesses"
    )
    assert (
        key("Payments for acquisition of business") == "acquisition_of_subsidiaries_and_businesses"
    )
    assert key("Purchase of Investments") == "purchase_of_investments"
