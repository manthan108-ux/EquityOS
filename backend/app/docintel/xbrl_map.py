"""From raw XBRL facts to EquityOS statement blocks.

The reader (`xbrl.py`) keeps every fact exactly as filed. This module decides which of them EquityOS
can use, and nothing more:

* Two taxonomy FAMILIES are handled separately, never mixed: the Ind AS (non-financial company)
  taxonomy and the banking taxonomy. A concept is looked up only in its own family's table. A filing
  that holds concepts of both is ambiguous and nothing is mapped.
* Standalone and consolidated are independent: the basis comes from the filing itself (its "nature
  of report" fact, or a dimension member naming the basis). If the filing does not state it, the
  basis is None and nothing is promoted by the pipeline (the existing rule). It is never inferred.
* Only facts in DIMENSIONLESS contexts feed the primary statements. Facts with dimensions (segments,
  notes, components) stay in the raw store.
* The reporting unit comes from the filing's "level of rounding" fact, or from the caller (the
person
  who supplied the file). If neither exists, or the filing's own `decimals` contradict the stated
  rounding level, no amount is given a unit and nothing is promoted. A percentage is accepted only
  with an explicit percent unit (or a caller who confirms `pure` means a fraction).
* A ratio of exactly zero is a placeholder some filers use for "not reported": it is not promoted.

CONCEPT NAMES: the tables below are written from the published Ind AS / banking taxonomy naming and
are DATA, extendable without code changes. They have been exercised only on synthetic fixtures,
not on
a real exchange filing (none was available offline): any concept not listed stays in the raw store
and is counted as unmapped. See docs/KNOWN_LIMITATIONS.md.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from app.docintel import taxonomy
from app.docintel.layout import decimals_of
from app.docintel.periods import ColumnPeriod
from app.docintel.statements import CellFact, ColumnInfo, StatementBlock
from app.docintel.xbrl import Context, Fact, Instance

IND_AS, BANK = "IND_AS", "BANK"
DETECTION = "xbrl"
INCOME, BALANCE, CASH, RATIOS = (
    taxonomy.INCOME,
    taxonomy.BALANCE_SHEET,
    taxonomy.CASH_FLOW,
    taxonomy.RATIOS,
)

# concept local name -> (statement, canonical item key)
IND_AS_MAP: dict[str, tuple[str, str]] = {
    "RevenueFromOperations": (INCOME, "revenue_from_operations"),
    "OtherIncome": (INCOME, "other_income"),
    "Income": (INCOME, "total_income"),
    "CostOfMaterialsConsumed": (INCOME, "cost_of_materials_consumed"),
    "PurchasesOfStockInTrade": (INCOME, "purchases_of_stock_in_trade"),
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade": (
        INCOME,
        "changes_in_inventories",
    ),
    "EmployeeBenefitExpense": (INCOME, "employee_benefits_expense"),
    "FinanceCosts": (INCOME, "finance_costs"),
    "DepreciationDepletionAndAmortisationExpense": (INCOME, "depreciation_and_amortisation"),
    "OtherExpenses": (INCOME, "other_expenses"),
    "Expenses": (INCOME, "total_expenses"),
    "ProfitBeforeExceptionalItemsAndTax": (INCOME, "profit_before_exceptional_items_and_tax"),
    "ExceptionalItemsBeforeTax": (INCOME, "exceptional_items"),
    "ProfitBeforeTax": (INCOME, "profit_before_tax"),
    "CurrentTax": (INCOME, "current_tax"),
    "DeferredTax": (INCOME, "deferred_tax"),
    "TaxExpense": (INCOME, "tax_expense"),
    "ProfitLossForPeriod": (INCOME, "net_profit"),
    "OtherComprehensiveIncomeNetOfTaxes": (INCOME, "other_comprehensive_income"),
    "ComprehensiveIncomeForThePeriod": (INCOME, "total_comprehensive_income"),
    "PaidUpValueOfEquityShareCapital": (INCOME, "paid_up_equity_share_capital"),
    "BasicEarningsLossPerShareFromContinuingOperationsAndDiscontinuedOperations": (
        INCOME,
        "eps_basic",
    ),
    "DilutedEarningsLossPerShareFromContinuingOperationsAndDiscontinuedOperations": (
        INCOME,
        "eps_diluted",
    ),
    "PropertyPlantAndEquipment": (BALANCE, "property_plant_and_equipment"),
    "NoncurrentAssets": (BALANCE, "total_non_current_assets"),
    "Inventories": (BALANCE, "inventories"),
    "TradeReceivablesCurrent": (BALANCE, "trade_receivables"),
    "CashAndCashEquivalents": (BALANCE, "cash_and_cash_equivalents"),
    "CurrentAssets": (BALANCE, "total_current_assets"),
    "Assets": (BALANCE, "total_assets"),
    "EquityShareCapital": (BALANCE, "equity_share_capital"),
    "OtherEquity": (BALANCE, "other_equity"),
    "EquityAttributableToOwnersOfParent": (BALANCE, "equity_attributable_to_owners"),
    "NonControllingInterest": (BALANCE, "non_controlling_interests"),
    "Equity": (BALANCE, "total_equity"),
    "Borrowings": (BALANCE, "borrowings"),
    "TradePayablesCurrent": (BALANCE, "trade_payables"),
    "NoncurrentLiabilities": (BALANCE, "total_non_current_liabilities"),
    "CurrentLiabilities": (BALANCE, "total_current_liabilities"),
    "Liabilities": (BALANCE, "total_liabilities"),
    "EquityAndLiabilities": (BALANCE, "total_equity_and_liabilities"),
    "CashFlowsFromUsedInOperatingActivities": (CASH, "net_cash_from_operating_activities"),
    "CashFlowsFromUsedInInvestingActivities": (CASH, "net_cash_from_investing_activities"),
    "CashFlowsFromUsedInFinancingActivities": (CASH, "net_cash_from_financing_activities"),
    "IncreaseDecreaseInCashAndCashEquivalents": (CASH, "net_change_in_cash"),
}

BANK_MAP: dict[str, tuple[str, str]] = {
    "InterestEarned": (INCOME, "interest_earned"),
    "InterestExpended": (INCOME, "interest_expended"),
    "OtherIncome": (INCOME, "other_income"),
    "Income": (INCOME, "total_income"),
    "OperatingExpenses": (INCOME, "operating_expenses"),
    "ProvisionsOtherThanTaxAndContingencies": (INCOME, "provisions_excluding_tax"),
    "OperatingProfitBeforeProvisionAndContingencies": (
        INCOME,
        "operating_profit_before_provisions",
    ),
    "ProfitBeforeTax": (INCOME, "profit_before_tax"),
    "TaxExpense": (INCOME, "tax_expense"),
    "ProfitLossForPeriod": (INCOME, "net_profit"),
    "PaidUpValueOfEquityShareCapital": (INCOME, "paid_up_equity_share_capital"),
    "BasicEarningsLossPerShareFromContinuingOperationsAndDiscontinuedOperations": (
        INCOME,
        "eps_basic",
    ),
    "DilutedEarningsLossPerShareFromContinuingOperationsAndDiscontinuedOperations": (
        INCOME,
        "eps_diluted",
    ),
    "CashAndBalancesWithReserveBankOfIndia": (BALANCE, "cash_and_balances_with_rbi"),
    "BalancesWithBanksAndMoneyAtCallAndShortNotice": (BALANCE, "balances_with_banks"),
    "Investments": (BALANCE, "investments"),
    "Advances": (BALANCE, "advances"),
    "FixedAssets": (BALANCE, "fixed_assets"),
    "OtherAssets": (BALANCE, "other_assets"),
    "Assets": (BALANCE, "total_assets"),
    "Capital": (BALANCE, "capital"),
    "ReservesAndSurplus": (BALANCE, "reserves_and_surplus"),
    "Deposits": (BALANCE, "deposits"),
    "Borrowings": (BALANCE, "borrowings"),
    "OtherLiabilitiesAndProvisions": (BALANCE, "other_liabilities_and_provisions"),
    "CapitalAndLiabilities": (BALANCE, "total_capital_and_liabilities"),
    "PercentageOfGrossNpa": (RATIOS, "gross_npa_ratio"),
    "PercentageOfNpa": (RATIOS, "net_npa_ratio"),
    "CapitalAdequacyRatio": (RATIOS, "capital_adequacy_ratio"),
    "ReturnOnAssets": (RATIOS, "return_on_assets"),
    "GrossNpa": (RATIOS, "gross_npa_amount"),
    "NetNpa": (RATIOS, "net_npa_amount"),
}
MAPS = {IND_AS: IND_AS_MAP, BANK: BANK_MAP}

# Concepts that only one family uses: they decide the family when the caller does not say.
_ONLY_IND_AS = {"RevenueFromOperations", "CostOfMaterialsConsumed", "PropertyPlantAndEquipment"}
_ONLY_BANK = {"InterestEarned", "InterestExpended", "Advances", "Deposits", "PercentageOfGrossNpa"}

_ROUNDING_CONCEPT = "levelofroundingusedinfinancialstatements"
_BASIS_CONCEPT = "natureofreportstandaloneconsolidated"
_AUDIT_CONCEPT = "whetherresultsareauditedorunaudited"
ROUNDING = {
    "actual": "inr",
    "actuals": "inr",
    "unit": "inr",
    "units": "inr",
    "rupees": "inr",
    "inr": "inr",
    "thousand": "thousand",
    "thousands": "thousand",
    "lakh": "lakhs",
    "lakhs": "lakhs",
    "lac": "lakhs",
    "million": "million",
    "millions": "million",
    "crore": "crores",
    "crores": "crores",
}
_DECIMALS_FOR = {"inr": 0, "thousand": -3, "lakhs": -5, "million": -6, "crores": -7}
_PERCENT_UNITS = {"percent", "percentage", "pct", "%"}
_NON_FLOW = {BALANCE}  # point-in-time statements


@dataclass
class Mapping:
    family: str | None
    basis: str | None
    basis_source: str
    unit: str | None
    unit_source: str
    audited: bool | None
    blocks: list[StatementBlock] = field(default_factory=list)
    mapped: dict[int, tuple[str, str]] = field(default_factory=dict)  # fact index -> (stmt, key)
    unmapped: list[str] = field(default_factory=list)  # concept qnames with no mapping
    dimensional: int = 0  # facts left raw because their context has dimensions
    issues: list[dict] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)


def _norm(local: str) -> str:
    return local.lower()


def _text_fact(inst: Instance, concept: str) -> str | None:
    for f in inst.facts:
        if _norm(f.local) == concept and not f.nil and f.text:
            return f.text
    return None


def detect_family(inst: Instance, override: str | None = None) -> tuple[str | None, str]:
    if override:
        if override not in MAPS:
            raise ValueError(f"unknown taxonomy family {override!r}")
        return override, "stated by the person who supplied the file"
    names = {f.local for f in inst.facts}
    ind, bank = names & _ONLY_IND_AS, names & _ONLY_BANK
    if ind and bank:
        return None, (
            f"ambiguous: Ind AS concepts ({', '.join(sorted(ind))}) and banking concepts "
            f"({', '.join(sorted(bank))}) are both present"
        )
    if bank:
        return BANK, "banking concepts present: " + ", ".join(sorted(bank))
    if ind:
        return IND_AS, "Ind AS concepts present: " + ", ".join(sorted(ind))
    return None, "no concept that identifies the taxonomy family"


def detect_basis(inst: Instance, override: str | None = None) -> tuple[str | None, str]:
    """CONSOLIDATED / STANDALONE as the filing states it, or None. Never inferred."""
    text = _text_fact(inst, _BASIS_CONCEPT)
    stated = None
    if text:
        low = text.lower()
        if "consolidated" in low and "standalone" not in low:
            stated = "CONSOLIDATED"
        elif "standalone" in low and "consolidated" not in low:
            stated = "STANDALONE"
    if stated is None:
        members = {m.lower() for c in inst.contexts.values() for _a, m, _k in c.dimensions}
        cons = any("consolidated" in m for m in members)
        stand = any("standalone" in m for m in members)
        if cons != stand:
            stated = "CONSOLIDATED" if cons else "STANDALONE"
    if override and stated and override != stated:
        return None, f"conflict: the filing states {stated} but {override} was supplied"
    if stated:
        return stated, "stated in the filing"
    if override:
        return override, "stated by the person who supplied the file"
    return None, "the filing does not state standalone or consolidated"


def detect_unit(
    inst: Instance, amounts: list[Fact], override: str | None
) -> tuple[str | None, str, list[dict]]:
    """The amount unit, from the filing's level of rounding or the caller; else None."""
    issues: list[dict] = []
    stated = None
    text = _text_fact(inst, _ROUNDING_CONCEPT)
    if text:
        stated = ROUNDING.get(re.sub(r"[^a-z]", "", text.lower()))
        if stated is None:
            issues.append(
                {
                    "page": None,
                    "code": "ROUNDING_UNKNOWN",
                    "detail": f"level of rounding {text!r} is not a unit EquityOS knows",
                }
            )
    if override:
        key = ROUNDING.get(override.lower())
        if key is None:
            raise ValueError(f"unknown amount scale {override!r}")
        if stated and stated != key:
            issues.append(
                {
                    "page": None,
                    "code": "SCALE_CONFLICT",
                    "detail": f"the filing states {stated} but {key} was supplied",
                }
            )
            return None, "conflict between the filing and the supplied scale", issues
        return key, "stated by the person who supplied the file", issues
    if stated is None:
        return None, "the filing states no level of rounding", issues
    expected = _DECIMALS_FOR[stated]
    declared = [f.decimals for f in amounts if f.decimals not in (None, "INF")]
    if stated != "inr" and declared:
        hits = sum(1 for d in declared if d == str(expected))
        if hits >= 0.8 * len(declared):
            issues.append(
                {
                    "page": None,
                    "code": "SCALE_AMBIGUOUS",
                    "detail": (
                        f"the filing says {stated} but most amounts declare decimals={expected}, "
                        "which is how absolute rupees rounded to that level look; the scale "
                        "must be confirmed by whoever supplies the file"
                    ),
                }
            )
            return None, "level of rounding contradicted by the amounts' own decimals", issues
    return stated, "stated in the filing (level of rounding)", issues


def _months(ctx: Context) -> tuple[int | None, str | None]:
    if ctx.start is None or ctx.end is None:
        return None, "duration context without both dates"
    days = (ctx.end - ctx.start).days + 1
    for months, lo, hi in ((3, 89, 93), (6, 180, 185), (9, 271, 277), (12, 363, 367)):
        if lo <= days <= hi:
            return months, None
    return None, f"a period of {days} days is not a quarter, half-year, nine months or a year"


def _period(ctx: Context, audited: bool | None) -> tuple[ColumnPeriod, str]:
    if ctx.is_instant:
        return ColumnPeriod(ctx.instant, None, True, audited), f"as at {ctx.instant}"
    months, problem = _months(ctx)
    return (
        ColumnPeriod(ctx.end, months, False, audited, problem),
        f"{ctx.start} to {ctx.end}",
    )


def _percent_unit(fact: Fact, inst: Instance) -> str | None:
    unit = inst.unit(fact)
    return unit.label.lower() if unit else None


def build(
    inst: Instance,
    *,
    family: str | None = None,
    basis: str | None = None,
    amount_scale: str | None = None,
    pure_means_fraction: bool | None = None,
) -> Mapping:
    fam, fam_why = detect_family(inst, family)
    bas, bas_why = detect_basis(inst, basis)
    audit_text = _text_fact(inst, _AUDIT_CONCEPT)
    audited = None
    if audit_text:
        low = audit_text.lower()
        audited = "unaudited" not in low and "audited" in low
    table = MAPS.get(fam or "", {})
    primary = [f for f in inst.facts if f.local in table and not f.nil]
    amount_facts = [
        f
        for f in primary
        if table[f.local][0] != RATIOS and taxonomy.item_kind(table[f.local][1]) == "amount"
    ]
    unit, unit_why, unit_issues = detect_unit(inst, amount_facts, amount_scale)
    out = Mapping(fam, bas, bas_why, unit, unit_why, audited)
    out.issues.extend(unit_issues)
    out.entity_ids = sorted({c.entity_id for c in inst.contexts.values() if c.entity_id}, key=str)
    if fam is None:
        out.issues.append({"page": None, "code": "TAXONOMY_FAMILY", "detail": fam_why})
        out.unmapped = sorted({f.qname for f in inst.facts})
        return out
    if bas is None or bas_why.startswith("conflict"):
        out.issues.append({"page": None, "code": "BASIS_NOT_STATED", "detail": bas_why})
        if bas_why.startswith("conflict"):
            out.basis = None

    by_stmt: dict[str, dict[tuple, list[tuple[Fact, Context]]]] = {}
    for f in inst.facts:
        entry = table.get(f.local)
        if entry is None:
            if f.unit_ref is not None:  # numeric concepts without a mapping are reported
                out.unmapped.append(f.qname)
            continue
        ctx = inst.context(f)
        if ctx is None:
            continue
        if ctx.dimensions:
            out.dimensional += 1
            continue
        statement, key = entry
        if (statement in _NON_FLOW) != ctx.is_instant:
            out.issues.append(
                {
                    "page": None,
                    "code": "PERIOD_KIND",
                    "detail": f"{f.qname} is a {statement} item but its context is "
                    f"{'an instant' if ctx.is_instant else 'a duration'}; left raw",
                }
            )
            continue
        if f.nil or f.numeric is None:
            continue
        col_key = (ctx.start, ctx.end, ctx.instant)
        by_stmt.setdefault(statement, {}).setdefault((key, col_key), []).append((f, ctx))
        out.mapped[f.index] = (statement, key)

    for statement in (INCOME, BALANCE, CASH, RATIOS):
        cells = by_stmt.get(statement)
        if not cells:
            continue
        col_keys = sorted(
            {ck for (_k, ck) in cells},
            key=lambda c: (c[2] or c[1] or date.min, c[0] or date.min),
            reverse=True,
        )
        columns: list[ColumnInfo] = []
        for i, ck in enumerate(col_keys):
            ctx = next(
                c for lst in cells.values() for (_f, c) in lst if (c.start, c.end, c.instant) == ck
            )
            period, header = _period(ctx, audited)
            info = ColumnInfo(i, 0.0, 0.0, header, period)
            info.problem = period.problem
            columns.append(info)
        keys_in_order = sorted({k for (k, _ck) in cells})
        facts: list[CellFact] = []
        for row, key in enumerate(keys_in_order):
            for ci, ck in enumerate(col_keys):
                found = cells.get((key, ck))
                if not found:
                    continue
                values = {f.numeric for f, _c in found}
                first = found[0][0]
                value = first.numeric
                scale_problem = None
                if statement == RATIOS and taxonomy.item_kind(key) == "percent":
                    unit_label = _percent_unit(first, inst)
                    if value == 0:
                        scale_problem = "a ratio of exactly zero is a filer's placeholder"
                    elif unit_label in _PERCENT_UNITS or (
                        unit_label == "pure" and pure_means_fraction is False
                    ):
                        pass
                    elif unit_label == "pure" and pure_means_fraction:
                        value = value * 100
                    else:
                        scale_problem = (
                            "the ratio's unit does not say whether it is a percentage or a "
                            "fraction; supply pure_means_fraction to confirm"
                        )
                cell = CellFact(
                    key=key,
                    raw_label=first.qname,
                    row_index=row,
                    column_index=ci,
                    raw_value=first.text[:60],
                    value=None if scale_problem else value,
                    decimals=decimals_of(first.text),
                    bbox=None,
                    duplicate=len(values) > 1,
                    page_number=1,
                )
                if scale_problem:
                    out.issues.append(
                        {
                            "page": 1,
                            "code": "RATIO_REFUSED",
                            "detail": f"{first.qname}: {scale_problem}",
                        }
                    )
                facts.append(cell)
        out.blocks.append(
            StatementBlock(
                page_number=1,
                statement=statement,
                title=f"XBRL {fam} {statement.lower().replace('_', ' ')}",
                detection=DETECTION,
                basis=out.basis,
                unit=unit,
                columns=columns,
                facts=facts,
            )
        )
    out.unmapped = sorted(set(out.unmapped))
    return out
