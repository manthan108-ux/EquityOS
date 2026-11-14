"""Give a statement its accounting basis when the page does not say, and a figure proves it.

A value is only promoted when its basis (consolidated or standalone) is stated, and many tables do
not state it: a segment table printed under "Segment reporting", a cash flow titled "Statement of
cash flows". The page text carries no marker to read it from, and guessing would break the rule
that the two bases are never confused.

What can be used is the document itself. A segment table's revenue after elimination equals the
income statement's revenue from operations (or total income); the cash flow starts from the income
statement's profit before tax; its closing cash equals the balance sheet's cash. If a table
without a basis has an anchor value that matches, to the unit, the same quantity in a table of THIS
document whose basis IS stated, and it matches only one basis, it is on that basis. If it matches
both (the two bases are equal), or neither, or the evidence points two ways, it stays without a
basis. Provider data is never used, so the inferred basis cannot be confirmed by the very figure it
came from. The inference is recorded on the statement ("basis inferred ...") and, by the caller,
in the extraction issues.
"""

from decimal import Decimal

from app.docintel.statements import ColumnInfo, StatementBlock
from app.docintel.taxonomy import BALANCE_SHEET, CASH_FLOW, INCOME, SEGMENT
from app.normalization import UNIT_TO_INR

# Groups of (statement, key) that name the same quantity in different tables of one document.
GROUPS: tuple[tuple[tuple[str, str], ...], ...] = (
    (
        (INCOME, "revenue_from_operations"),
        (SEGMENT, "segment_revenue_after_elimination"),
        (SEGMENT, "segment_revenue_total"),
    ),
    (
        (INCOME, "total_income"),
        (SEGMENT, "segment_revenue_after_elimination"),
        (SEGMENT, "segment_revenue_total"),
    ),
    ((INCOME, "profit_before_tax"), (CASH_FLOW, "profit_before_tax")),
    ((BALANCE_SHEET, "cash_and_cash_equivalents"), (CASH_FLOW, "cash_at_end")),
    ((BALANCE_SHEET, "total_assets"), (SEGMENT, "segment_assets_total")),
)
MIN_UNITS = Decimal(100)  # a match on a tiny number proves nothing
ROUNDS = 3  # a basis found for one table can anchor the next


def _scale(unit: str | None) -> Decimal | None:
    return UNIT_TO_INR.get((unit or "").strip().lower())


def _cell(block: StatementBlock, key: str, column: int) -> Decimal | None:
    values = {
        f.value
        for f in block.facts
        if f.key == key
        and f.column_index == column
        and f.value is not None
        and not f.duplicate
        and f.dimension is None
    }
    return next(iter(values)) if len(values) == 1 else None


def _same_period(a: ColumnInfo, b: ColumnInfo) -> bool:
    pa, pb = a.period, b.period
    return (
        pa.period_end is not None
        and pa.period_end == pb.period_end
        and pa.duration_months == pb.duration_months
        and bool(pa.point_in_time) == bool(pb.point_in_time)
    )


def _equal(x: Decimal, unit_x: str | None, y: Decimal, unit_y: str | None) -> bool:
    sx, sy = _scale(unit_x), _scale(unit_y)
    if sx is None or sy is None or abs(x) < MIN_UNITS or abs(y) < MIN_UNITS:
        return False
    if sx == sy:  # the same figure printed in the same unit has the same digits
        return x == y
    # different units: equal to the rounding of the coarser table
    return abs(x * sx - y * sy) <= max(sx, sy) / 2


def infer_missing_basis(blocks: list[StatementBlock]) -> list[dict]:
    """Set `basis` on tables that lack one where the document proves it; returns what was set."""
    notes: list[dict] = []
    for _ in range(ROUNDS):
        changed = False
        known = [b for b in blocks if b.basis and b.unit]
        for block in blocks:
            if block.basis or not block.unit or block.statement not in (
                INCOME, BALANCE_SHEET, CASH_FLOW, SEGMENT
            ):  # fmt: skip
                continue
            verdicts: list[tuple[str, str]] = []  # (basis, anchor description)
            ambiguous = False
            for column in block.columns:
                if not column.usable:
                    continue
                for group in GROUPS:
                    mine = [k for st, k in group if st == block.statement]
                    theirs = [(st, k) for st, k in group if st != block.statement]
                    for key in mine:
                        value = _cell(block, key, column.index)
                        if value is None:
                            continue
                        matched: dict[str, str] = {}
                        for other in known:
                            for st, other_key in theirs:
                                if other.statement != st:
                                    continue
                                for oc in other.columns:
                                    if not oc.usable or not _same_period(column, oc):
                                        continue
                                    w = _cell(other, other_key, oc.index)
                                    if w is not None and _equal(value, block.unit, w, other.unit):
                                        matched[other.basis] = (
                                            f"{key} equals {other.basis.lower()} "
                                            f"{other_key} on page {other.page_number}"
                                        )
                        if len(matched) == 1:
                            basis, why = next(iter(matched.items()))
                            verdicts.append((basis, why))
                        elif len(matched) > 1:
                            ambiguous = (
                                True  # equal on both bases: the figure cannot tell them apart
                            )
            bases = {b for b, _ in verdicts}
            if ambiguous or len(bases) != 1:
                continue
            basis = next(iter(bases))
            why = verdicts[0][1]
            block.basis = basis
            block.detection = f"{block.detection}; basis inferred ({why})"[:200]
            notes.append(
                {
                    "page": block.page_number,
                    "statement": block.statement,
                    "basis": basis,
                    "why": why,
                }
            )
            changed = True
        if not changed:
            break
    return notes
