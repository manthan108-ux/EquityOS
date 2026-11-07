"""Standalone quarters from cumulative figures, and consistency checks between them.

Indian companies report year-to-date figures: in the September results, "six months ended" is
April-September; in December, "nine months ended"; the March results give the full year. A
standalone quarter is the difference between two cumulative figures:

    Q2 = H1 (Apr-Sep)  - Q1 (Apr-Jun)
    Q3 = 9M (Apr-Dec)  - H1 (Apr-Sep)
    Q4 = FY (Apr-Mar)  - 9M (Apr-Dec)

A subtraction is performed ONLY when all of this holds, otherwise nothing is derived and the reason
is reported:

* both figures are validated facts of the same company, line item, statement and basis
  (consolidated and standalone are never mixed);
* the later figure ends on the target quarter's end and the earlier one on the previous quarter's
  end, with durations exactly 3 months apart (so the periods nest: same fiscal year);
* both have a unit that converts to rupees;
* the line item is an additive amount (per-share figures and balances are never subtracted).

Reported figures are never overwritten. A derived quarter is a separate fact (provider
`DERIVED_QUARTER`) with the exact inputs recorded in `derived_from`. If the company ALSO reported
that quarter, nothing is derived; instead the reported value and the derived value are compared and
the result is returned (a useful independent check on both documents).
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.docintel.layout import decimals_of
from app.models import Basis, ExtractedFact, FinancialFact, PeriodType, Statement
from app.normalization import UNIT_TO_INR, month_end, months_between, period_label

PROVIDER_CODE = "DERIVED_QUARTER"
RELATIVE_TOLERANCE = Decimal("0.001")  # 0.1% when both sides are rounded independently

# Items that are NOT additive over time (per-share values, share capital) or are balances.
NOT_ADDITIVE = {"eps_basic", "eps_diluted", "paid_up_equity_share_capital"}

# target quarter month -> (cumulative type that ends there, its months, previous cumulative type,
# its months, previous period end month)
_RULES: dict[int, tuple[PeriodType, int, PeriodType | None, int, int]] = {
    9: (PeriodType.HALF_YEAR, 6, PeriodType.QUARTERLY, 3, 6),
    12: (PeriodType.NINE_MONTH, 9, PeriodType.HALF_YEAR, 6, 9),
    3: (PeriodType.ANNUAL, 12, PeriodType.NINE_MONTH, 9, 12),
}


@dataclass
class DerivedItem:
    line_item: str
    basis: Basis
    quarter: str
    outcome: str  # DERIVED | CONSISTENT | INCONSISTENT | SKIPPED
    detail: str


@dataclass
class DeriveReport:
    items: list[DerivedItem] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for i in self.items if i.outcome == outcome)


def _unit_factor(unit: str) -> Decimal:
    return UNIT_TO_INR.get(unit.lower(), Decimal(1))


def _decimals(session: Session, fact: FinancialFact) -> int:
    """Printed decimals of the source cell (trailing zeros are not kept in the database)."""
    raw = session.scalar(
        select(ExtractedFact.raw_value)
        .where(
            ExtractedFact.document_id == fact.document_id,
            ExtractedFact.canonical_item == fact.line_item,
            ExtractedFact.period_end == fact.period_end,
            ExtractedFact.basis == fact.basis.value,
            ExtractedFact.promoted.is_(True),
        )
        .order_by(ExtractedFact.id.desc())
        .limit(1)
    )
    return decimals_of(raw) if raw else 0


def _rounding_unit(session: Session, fact: FinancialFact) -> Decimal:
    """Half a printed unit in rupees: the most a printed figure can be off by."""
    return _unit_factor(fact.original_unit) * Decimal(1).scaleb(-_decimals(session, fact)) / 2


def _index(session: Session, company_id: uuid.UUID) -> dict[tuple, FinancialFact]:
    facts = session.scalars(
        select(FinancialFact).where(
            FinancialFact.company_id == company_id,
            FinancialFact.statement == Statement.INCOME,
            FinancialFact.provider_code != PROVIDER_CODE,
        )
    )
    return {
        (f.line_item, f.basis, f.period_end, f.period_type): f
        for f in facts
        if f.value_inr is not None
    }


def derive_quarters(
    session: Session, company_id: uuid.UUID, *, write: bool = True, now: datetime | None = None
) -> DeriveReport:
    """Derive missing standalone quarters and check reported ones (see module docstring)."""
    report = DeriveReport()
    facts = _index(session, company_id)
    now = now or datetime.now(UTC)
    for (line, basis, end, ptype), cumulative in sorted(
        facts.items(), key=lambda kv: (kv[0][0], kv[0][1].value, kv[0][2])
    ):
        rule = _RULES.get(end.month)
        if rule is None or line in NOT_ADDITIVE:
            continue
        cum_type, _cum_months, prev_type, prev_months, prev_month = rule
        if ptype is not cum_type:
            continue
        quarter = period_label(end, 3)
        previous_end = month_end(end.year if prev_month < end.month else end.year - 1, prev_month)
        if months_between(previous_end, end) != 3:
            continue  # defensive: periods must be exactly one quarter apart
        previous = facts.get((line, basis, previous_end, prev_type))
        if previous is None:
            report.items.append(
                DerivedItem(
                    line,
                    basis,
                    quarter,
                    "SKIPPED",
                    f"the earlier cumulative figure ({prev_type.value.lower()} to {previous_end}) "
                    "is not available",
                )
            )
            continue
        value = cumulative.value_inr - previous.value_inr
        uncertainty = _rounding_unit(session, cumulative) + _rounding_unit(session, previous)
        reported = facts.get((line, basis, end, PeriodType.QUARTERLY))
        if reported is not None:
            allowed = max(
                abs(reported.value_inr) * RELATIVE_TOLERANCE,
                uncertainty + _rounding_unit(session, reported),
            )
            diff = abs(reported.value_inr - value)
            outcome = "CONSISTENT" if diff <= allowed else "INCONSISTENT"
            report.items.append(
                DerivedItem(
                    line,
                    basis,
                    quarter,
                    outcome,
                    f"reported {reported.value_inr} vs {cumulative.period_label} - "
                    f"{previous.period_label} = {value} (difference {diff}, allowed {allowed})",
                )
            )
            continue
        report.items.append(
            DerivedItem(
                line,
                basis,
                quarter,
                "DERIVED",
                f"{cumulative.period_label} {cumulative.original_value} {cumulative.original_unit}"
                f" - {previous.period_label} {previous.original_value} {previous.original_unit}"
                f" = {value} INR",
            )
        )
        if write:
            _store(session, company_id, cumulative, previous, value, uncertainty, quarter, now)
    return report


def _store(
    session: Session,
    company_id: uuid.UUID,
    cumulative: FinancialFact,
    previous: FinancialFact,
    value: Decimal,
    uncertainty: Decimal,
    quarter: str,
    now: datetime,
) -> None:
    row = session.scalar(
        select(FinancialFact).where(
            FinancialFact.company_id == company_id,
            FinancialFact.provider_code == PROVIDER_CODE,
            FinancialFact.statement == Statement.INCOME,
            FinancialFact.line_item == cumulative.line_item,
            FinancialFact.period_end == cumulative.period_end,
            FinancialFact.period_type == PeriodType.QUARTERLY,
            FinancialFact.basis == cumulative.basis,
        )
    )
    if row is None:
        row = FinancialFact(
            company_id=company_id,
            provider_code=PROVIDER_CODE,
            statement=Statement.INCOME,
            line_item=cumulative.line_item,
            period_end=cumulative.period_end,
            period_type=PeriodType.QUARTERLY,
            basis=cumulative.basis,
        )
        session.add(row)

    def describe(f: FinancialFact) -> dict:
        return {
            "fact_id": f.id,
            "period": f.period_label,
            "value": str(f.original_value),
            "unit": f.original_unit,
            "value_inr": str(f.value_inr),
            "document_id": str(f.document_id) if f.document_id else None,
            "page": f.source_page,
            "provider": f.provider_code,
        }

    row.original_value = value
    row.original_unit = "inr"  # the calculation is in rupees; the inputs keep their own units
    row.value_inr = value
    row.period_label = quarter
    row.source_url = f"derived:{cumulative.period_label}-{previous.period_label}"
    row.retrieved_at = now
    row.payload_id = None
    row.document_id = None
    row.source_page = None
    row.derived_from = {
        "method": f"{cumulative.period_label} minus {previous.period_label}",
        "minuend": describe(cumulative),
        "subtrahend": describe(previous),
        "rounding_uncertainty_inr": str(uncertainty),
        "mixed_documents": cumulative.document_id != previous.document_id,
    }
    session.flush()


__all__ = ["DeriveReport", "DerivedItem", "derive_quarters"]
