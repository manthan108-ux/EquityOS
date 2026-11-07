"""Arithmetic cross-checks inside one statement column.

A check is an identity that must hold in a correctly read statement, e.g.
    total income = revenue from operations + other income
    total assets = total equity and liabilities
Several alternative forms may be listed (companies print tax as a positive expense or as a
negative adjustment); the check passes if any form holds. A form is evaluated only when every
item it needs was read in that column, so a missing line never causes a failure.

Tolerance comes from rounding: each printed value can be off by half a unit of its last decimal,
so a sum of n terms may differ by up to (n + 1) / 2 units of the last decimal. A small relative
allowance (0.05%) covers statements that round subtotals independently. Nothing larger is
accepted: a misread column or a wrong unit breaks these identities.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.docintel.statements import StatementBlock
from app.docintel.taxonomy import BALANCE_SHEET, CASH_FLOW, INCOME
from app.models import ValidationStatus

RELATIVE_TOLERANCE = Decimal("0.0005")

# Each form: (total key, [(sign, key), ...]) meaning total == sum(sign * value).
Form = tuple[str, list[tuple[int, str]]]


@dataclass(frozen=True)
class Check:
    name: str
    statement: str
    forms: tuple[Form, ...]
    # skip the check if any of these items was read with a (non-nil) value in the column
    unless_present: tuple[str, ...] = ()


def _f(total: str, *terms: str) -> Form:
    return total, [(-1 if t.startswith("-") else 1, t.lstrip("+-")) for t in terms]


def _with_optional(total: str, required: list[str], optional: list[str]) -> tuple[Form, ...]:
    """One form per subset of the optional lines (banks print some lines only when non-zero)."""
    forms = []
    for mask in range(2 ** len(optional)):
        extra = [item for bit, item in enumerate(optional) if mask >> bit & 1]
        forms.append(_f(total, *required, *extra))
    return tuple(forms)


CHECKS: tuple[Check, ...] = (
    Check(
        "total income = revenue + other income",
        INCOME,
        (
            _f("total_income", "revenue_from_operations", "other_income"),
            _f("total_income", "interest_earned", "other_income"),
        ),
    ),
    Check(
        "profit before exceptional items = total income - total expenses",
        INCOME,
        (
            _f("profit_before_exceptional_items_and_tax", "total_income", "-total_expenses"),
            _f(
                "profit_before_exceptional_items_and_tax",
                "total_income",
                "-total_expenses",
                "share_of_profit_of_associates_and_jvs",
            ),
        ),
    ),
    Check(
        "profit before tax = profit before exceptional items + exceptional items",
        INCOME,
        (
            _f("profit_before_tax", "profit_before_exceptional_items_and_tax", "exceptional_items"),
            _f(
                "profit_before_tax", "profit_before_exceptional_items_and_tax", "-exceptional_items"
            ),
            _f(
                "profit_before_tax",
                "profit_before_exceptional_items_and_tax",
                "exceptional_items",
                "share_of_profit_of_associates_and_jvs",
            ),
        ),
    ),
    Check(
        "profit before tax = profit before exceptional items + associates (exceptional items nil)",
        INCOME,
        (
            _f(
                "profit_before_tax",
                "profit_before_exceptional_items_and_tax",
                "share_of_profit_of_associates_and_jvs",
            ),
        ),
        unless_present=("exceptional_items",),
    ),
    Check(
        "profit before tax = total income - total expenses (no exceptional items line)",
        INCOME,
        (
            _f("profit_before_tax", "total_income", "-total_expenses"),
            _f(
                "profit_before_tax",
                "total_income",
                "-total_expenses",
                "share_of_profit_of_associates_and_jvs",
            ),
        ),
        unless_present=("exceptional_items", "profit_before_exceptional_items_and_tax"),
    ),
    Check(
        "bank: total expenditure = interest expended + operating expenses",
        INCOME,
        (_f("total_expenditure_excl_provisions", "interest_expended", "operating_expenses"),),
    ),
    Check(
        "bank: operating profit = total income - expenditure",
        INCOME,
        (
            _f(
                "operating_profit_before_provisions",
                "total_income",
                "-total_expenditure_excl_provisions",
            ),
        ),
    ),
    Check(
        "bank: profit before tax = operating profit - provisions",
        INCOME,
        (
            _f(
                "profit_before_tax",
                "operating_profit_before_provisions",
                "-provisions_excluding_tax",
            ),
            _f(
                "profit_before_tax",
                "operating_profit_before_provisions",
                "-provisions_excluding_tax",
                "exceptional_items",
            ),
            # consolidated: + share of profit in associates
            _f(
                "profit_before_tax",
                "operating_profit_before_provisions",
                "-provisions_excluding_tax",
                "share_of_profit_of_associates_and_jvs",
            ),
            _f(
                "profit_before_tax",
                "operating_profit_before_provisions",
                "-provisions_excluding_tax",
                "exceptional_items",
                "share_of_profit_of_associates_and_jvs",
            ),
        ),
    ),
    # Bank annual-report format (Third Schedule, Form B): tax sits inside "Provisions and
    # contingencies", so expenditure covers everything and net profit = income - expenditure.
    Check(
        "bank: total expenditure = interest expended + operating expenses + provisions",
        INCOME,
        (
            _f(
                "total_expenses",
                "interest_expended",
                "operating_expenses",
                "provisions_and_contingencies",
            ),
        ),
    ),
    Check(
        "bank: net profit = total income - total expenditure (tax within provisions)",
        INCOME,
        (_f("net_profit", "total_income", "-total_expenses"),),
        unless_present=(
            "tax_expense",
            "profit_before_tax",
            "profit_before_exceptional_items_and_tax",
            "net_profit_before_associates_and_minority",
        ),
    ),
    Check(
        "bank (consolidated): profit before associates and minority = income - expenditure",
        INCOME,
        (_f("net_profit_before_associates_and_minority", "total_income", "-total_expenses"),),
    ),
    Check(
        "bank (consolidated): profit before minority = previous line + share of associates",
        INCOME,
        (
            _f(
                "net_profit_before_minority",
                "net_profit_before_associates_and_minority",
                "share_of_profit_of_associates_and_jvs",
            ),
        ),
    ),
    Check(
        "bank (consolidated): group profit = profit before minority - minority interest",
        INCOME,
        (_f("net_profit", "net_profit_before_minority", "-minority_share_of_profit"),),
    ),
    Check(
        "bank: profit before exceptional items = operating profit - provisions",
        INCOME,
        (
            _f(
                "profit_before_exceptional_items_and_tax",
                "operating_profit_before_provisions",
                "-provisions_excluding_tax",
            ),
        ),
    ),
    Check(
        "profit after tax = profit before tax - tax",
        INCOME,
        (
            _f("profit_after_tax", "profit_before_tax", "-tax_expense"),
            _f("profit_after_tax", "profit_before_tax", "tax_expense"),
        ),
    ),
    Check(
        "net profit = profit after tax + share of associates and joint ventures",
        INCOME,
        (_f("net_profit", "profit_after_tax", "share_of_profit_of_associates_and_jvs"),),
    ),
    Check(
        "net profit = profit before tax - tax",
        INCOME,
        (
            _f("net_profit", "profit_before_tax", "-tax_expense"),
            _f("net_profit", "profit_before_tax", "tax_expense"),
            _f("net_profit", "profit_before_tax", "-tax_expense", "-minority_share_of_profit"),
        ),
        unless_present=("net_profit_from_ordinary_activities",),
    ),
    Check(
        "bank: profit from ordinary activities after tax = profit before tax - tax",
        INCOME,
        (
            _f("net_profit_from_ordinary_activities", "profit_before_tax", "-tax_expense"),
            _f("net_profit_from_ordinary_activities", "profit_before_tax", "tax_expense"),
            _f(
                "net_profit_from_ordinary_activities",
                "profit_before_tax",
                "-tax_expense",
                "-minority_share_of_profit",
            ),
        ),
    ),
    Check(
        "bank: net profit = ordinary profit - extraordinary items",
        INCOME,
        (_f("net_profit", "net_profit_from_ordinary_activities", "-extraordinary_items"),),
    ),
    Check(
        "bank: net profit = ordinary profit (extraordinary items nil)",
        INCOME,
        (_f("net_profit", "net_profit_from_ordinary_activities"),),
        unless_present=("extraordinary_items",),
    ),
    Check(
        "total comprehensive income = net profit + other comprehensive income",
        INCOME,
        (_f("total_comprehensive_income", "net_profit", "other_comprehensive_income"),),
    ),
    Check(
        "net profit = owners' share + non-controlling share",
        INCOME,
        (_f("net_profit", "net_profit_owners", "net_profit_non_controlling"),),
    ),
    Check(
        "total assets = total equity and liabilities",
        BALANCE_SHEET,
        (
            _f("total_assets", "total_equity_and_liabilities"),
            _f("total_assets", "total_capital_and_liabilities"),
        ),
    ),
    Check(
        "total assets = non-current + current assets",
        BALANCE_SHEET,
        (_f("total_assets", "total_non_current_assets", "total_current_assets"),),
    ),
    Check(
        "total equity = share capital + other equity (+ non-controlling interests)",
        BALANCE_SHEET,
        (
            _f("total_equity", "equity_share_capital", "other_equity"),
            _f("total_equity", "equity_share_capital", "other_equity", "non_controlling_interests"),
            _f("total_equity", "equity_attributable_to_owners", "non_controlling_interests"),
            _f("equity_attributable_to_owners", "equity_share_capital", "other_equity"),
        ),
    ),
    Check(
        "total equity and liabilities = equity + liabilities",
        BALANCE_SHEET,
        (
            _f(
                "total_equity_and_liabilities",
                "total_equity",
                "total_non_current_liabilities",
                "total_current_liabilities",
            ),
            _f("total_equity_and_liabilities", "total_equity", "total_liabilities"),
        ),
    ),
    Check(
        "bank: capital and liabilities add up",
        BALANCE_SHEET,
        _with_optional(
            "total_capital_and_liabilities",
            [
                "capital",
                "reserves_and_surplus",
                "deposits",
                "borrowings",
                "other_liabilities_and_provisions",
            ],
            ["employee_stock_options_outstanding", "minority_interest", "policyholders_funds"],
        ),
    ),
    Check(
        "bank: assets add up",
        BALANCE_SHEET,
        _with_optional(
            "total_assets",
            [
                "cash_and_balances_with_rbi",
                "balances_with_banks",
                "investments",
                "advances",
                "fixed_assets",
                "other_assets",
            ],
            ["goodwill_on_consolidation"],
        ),
    ),
    Check(
        "net change in cash = operating + investing + financing",
        CASH_FLOW,
        (
            _f(
                "net_change_in_cash",
                "net_cash_from_operating_activities",
                "net_cash_from_investing_activities",
                "net_cash_from_financing_activities",
            ),
            _f(
                "net_change_in_cash",
                "net_cash_from_operating_activities",
                "net_cash_from_investing_activities",
                "net_cash_from_financing_activities",
                "fx_effect_on_cash",
            ),
        ),
    ),
    Check(
        "closing cash = opening cash + net change",
        CASH_FLOW,
        (
            _f("cash_at_end", "cash_at_beginning", "net_change_in_cash"),
            _f("cash_at_end", "cash_at_beginning", "net_change_in_cash", "fx_effect_on_cash"),
        ),
    ),
)


@dataclass
class CheckResult:
    name: str
    column: int
    passed: bool
    form: str  # the identity evaluated, with the numbers used
    difference: Decimal
    tolerance: Decimal
    keys: tuple[str, ...]
    # A confirm-only check can raise values to PASSED but its failure proves nothing (an
    # unrecognised component line is enough to break a sum), so it never marks values FAILED.
    confirm_only: bool = False

    def as_json(self) -> dict:
        return {
            "check": self.name,
            "column": self.column,
            "passed": self.passed,
            "evaluated": self.form,
            "difference": str(self.difference),
            "tolerance": str(self.tolerance),
            "confirm_only": self.confirm_only,
        }


def _evaluate(block: StatementBlock, column: int, form: Form) -> CheckResult | None:
    total_key, terms = form
    total = block.value(total_key, column)
    parts = [(sign, block.value(key, column)) for sign, key in terms]
    if total is None or total.value is None or any(p is None or p.value is None for _, p in parts):
        return None
    computed = sum((sign * p.value for sign, p in parts), Decimal(0))
    decimals = max([total.decimals, *(p.decimals for _, p in parts)])
    unit = Decimal(1).scaleb(-decimals)
    tolerance = max(unit * Decimal(len(parts) + 1) / 2, abs(total.value) * RELATIVE_TOLERANCE)
    difference = total.value - computed
    expr = " ".join(f"{'+' if s > 0 else '-'} {p.value}" for s, p in parts).lstrip("+ ")
    return CheckResult(
        name="",
        column=column,
        passed=abs(difference) <= tolerance,
        form=f"{total_key} {total.value} vs {expr} = {computed}",
        difference=difference,
        tolerance=tolerance,
        keys=(total_key, *(k for _, k in terms)),
    )


def _has_value(block: StatementBlock, key: str, column: int) -> bool:
    fact = block.value(key, column)
    return fact is not None and fact.value is not None


def run_checks(block: StatementBlock) -> list[CheckResult]:
    """All applicable checks, per usable column. A check with no evaluable form is skipped."""
    results: list[CheckResult] = []
    for column in block.columns:
        if column.is_note:
            continue
        for check in CHECKS:
            if check.statement != block.statement:
                continue
            if any(_has_value(block, k, column.index) for k in check.unless_present):
                continue
            evaluated = [r for r in (_evaluate(block, column.index, f) for f in check.forms) if r]
            if not evaluated:
                continue
            passing = [r for r in evaluated if r.passed]
            chosen = passing[0] if passing else evaluated[0]
            chosen.name = check.name
            results.append(chosen)
        for name, total, components, minimum in COMPONENT_SUMS:
            result = _component_sum(block, column.index, total, components, minimum)
            if result:
                result.name = name
                results.append(result)
    return results


# Sums whose component list varies by company: every component READ in the column is added.
COMPONENT_SUMS: tuple[tuple[str, str, tuple[str, ...], int], ...] = (
    (
        "total expenses = sum of expense lines (confirm-only)",
        "total_expenses",
        (
            "cost_of_materials_consumed",
            "purchases_of_stock_in_trade",
            "changes_in_inventories",
            "excise_duty",
            "employee_benefits_expense",
            "finance_costs",
            "depreciation_and_amortisation",
            "other_expenses",
        ),
        3,
    ),
    (
        "bank: operating expenses = employee cost + other operating expenses (confirm-only)",
        "operating_expenses",
        ("employee_benefits_expense", "other_expenses"),
        2,
    ),
)


def _component_sum(
    block: StatementBlock, column: int, total: str, components: tuple[str, ...], minimum: int
) -> CheckResult | None:
    present = [k for k in components if _has_value(block, k, column)]
    if len(present) < minimum or not _has_value(block, total, column):
        return None
    result = _evaluate(block, column, _f(total, *present))
    if result is not None:
        result.confirm_only = True
    return result


def fact_statuses(
    block: StatementBlock, results: list[CheckResult]
) -> dict[tuple[str, int], tuple[ValidationStatus, list[str]]]:
    """Status per (item, column): PASSED, FAILED (any check failed) or UNCHECKED."""
    out: dict[tuple[str, int], tuple[ValidationStatus, list[str]]] = {}
    for fact in block.facts:
        key = (fact.key, fact.column_index)
        mine = [
            r
            for r in results
            if r.column == fact.column_index
            and fact.key in r.keys
            and (r.passed or not r.confirm_only)
        ]
        if not mine:
            out[key] = (ValidationStatus.UNCHECKED, [])
        elif any(not r.passed for r in mine):
            out[key] = (
                ValidationStatus.FAILED,
                [f"failed: {r.name} ({r.form})" for r in mine if not r.passed],
            )
        else:
            out[key] = (ValidationStatus.PASSED, [f"passed: {r.name}" for r in mine])
    return out


def column_verified(results: list[CheckResult], column: int) -> bool:
    """A column is verified if at least one check passed in it and none failed."""
    mine = [r for r in results if r.column == column and (r.passed or not r.confirm_only)]
    return bool(mine) and all(r.passed for r in mine)
