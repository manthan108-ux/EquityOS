"""A table with no stated basis gets one only when a figure in the same document proves it."""

from datetime import date
from decimal import Decimal as D

from app.docintel import taxonomy as tx
from app.docintel.basis import infer_missing_basis
from app.docintel.periods import ColumnPeriod
from app.docintel.statements import CellFact, ColumnInfo, StatementBlock

Q2 = date(2026, 9, 30)


def column(index=1, end=Q2, months=3):
    return ColumnInfo(
        index=index,
        x0=0,
        x1=1,
        header="h",
        period=ColumnPeriod(end, months, False, None),
    )


def block(statement, basis, unit, values, *, end=Q2, months=3, page=1, dimension=None):
    cols = [column(1, end, months)]
    facts = [
        CellFact(
            key=key, raw_label=key, row_index=i, column_index=1, raw_value=str(v), value=D(v),
            decimals=0, bbox=None, page_number=page, dimension=dimension,
        )
        for i, (key, v) in enumerate(values.items())
    ]  # fmt: skip
    return StatementBlock(
        page_number=page, statement=statement, title=None, detection="t", basis=basis, unit=unit,
        columns=cols, facts=facts,
    )  # fmt: skip


def income(basis, revenue, unit="crore", page=1, **kw):
    return block(tx.INCOME, basis, unit, {"revenue_from_operations": revenue}, page=page, **kw)


def segment(revenue, unit="crore", page=5, **kw):
    return block(
        tx.SEGMENT, None, unit, {"segment_revenue_after_elimination": revenue}, page=page, **kw
    )


def test_a_segment_table_takes_the_basis_of_the_income_statement_it_matches():
    seg = segment(12345)
    notes = infer_missing_basis([income("CONSOLIDATED", 12345), income("STANDALONE", 9000), seg])
    assert seg.basis == "CONSOLIDATED"
    assert notes[0]["basis"] == "CONSOLIDATED" and "revenue_from_operations" in notes[0]["why"]
    assert "basis inferred" in seg.detection


def test_it_matches_the_standalone_statement_just_as_well():
    seg = segment(9000)
    infer_missing_basis([income("CONSOLIDATED", 12345), income("STANDALONE", 9000), seg])
    assert seg.basis == "STANDALONE"


def test_when_both_bases_have_the_same_figure_the_basis_is_left_unstated():
    seg = segment(5000)
    infer_missing_basis([income("CONSOLIDATED", 5000), income("STANDALONE", 5000), seg])
    assert seg.basis is None, "equal on both bases proves nothing"


def test_when_no_figure_matches_the_basis_is_left_unstated():
    seg = segment(12346)
    assert (
        infer_missing_basis([income("CONSOLIDATED", 12345), income("STANDALONE", 9000), seg]) == []
    )
    assert seg.basis is None


def test_a_figure_that_is_off_by_one_in_the_same_unit_is_not_a_match():
    seg = segment(12346)
    infer_missing_basis([income("CONSOLIDATED", 12345), seg])
    assert seg.basis is None


def test_different_units_allow_only_the_coarser_tables_rounding():
    # 12,345 crore printed as 123,450,000 thousand-rupees-equivalent in lakhs: exact
    exact = segment(1234500, unit="lakh")
    infer_missing_basis([income("CONSOLIDATED", 12345), exact])
    assert exact.basis == "CONSOLIDATED"
    # 12,345.4 crore in lakhs rounds to 12345 crore at the coarser table: within half a crore
    rounded = segment(1234540, unit="lakh")
    infer_missing_basis([income("CONSOLIDATED", 12345), rounded])
    assert rounded.basis == "CONSOLIDATED"
    off = segment(1235100, unit="lakh")  # 12,351 crore: six crore away
    infer_missing_basis([income("CONSOLIDATED", 12345), off])
    assert off.basis is None


def test_a_different_period_does_not_count_as_a_match():
    seg = segment(12345)
    other = income("CONSOLIDATED", 12345, end=date(2025, 9, 30))
    infer_missing_basis([other, seg])
    assert seg.basis is None
    wrong_length = income("CONSOLIDATED", 12345, months=12)
    infer_missing_basis([wrong_length, seg])
    assert seg.basis is None


def test_units_are_scaled_before_comparing():
    seg = segment(1234500, unit="lakh")  # 12,345 crore
    infer_missing_basis([income("CONSOLIDATED", 12345, unit="crore"), seg])
    assert seg.basis == "CONSOLIDATED"
    seg2 = segment(12345, unit="million")  # 1,234.5 crore: not the same
    infer_missing_basis([income("CONSOLIDATED", 12345, unit="crore"), seg2])
    assert seg2.basis is None


def test_a_table_without_a_unit_is_never_inferred():
    seg = segment(12345, unit=None)
    infer_missing_basis([income("CONSOLIDATED", 12345), seg])
    assert seg.basis is None


def test_small_numbers_prove_nothing():
    seg = segment(12)
    infer_missing_basis([income("CONSOLIDATED", 12), seg])
    assert seg.basis is None


def test_a_stated_basis_is_never_overwritten():
    seg = segment(12345)
    seg.basis = "STANDALONE"
    infer_missing_basis([income("CONSOLIDATED", 12345), seg])
    assert seg.basis == "STANDALONE"


def test_a_segment_dimension_value_is_not_an_anchor():
    """Only the table's own total row counts; a single segment's figure could match anything."""
    seg = block(
        tx.SEGMENT, None, "crore", {"segment_revenue_after_elimination": 12345},
        dimension="Retail",
    )  # fmt: skip
    infer_missing_basis([income("CONSOLIDATED", 12345), seg])
    assert seg.basis is None


def test_a_cash_flow_takes_its_basis_from_the_income_statement_profit_before_tax():
    pbt = block(tx.INCOME, "STANDALONE", "crore", {"profit_before_tax": 4321}, page=2)
    other = block(tx.INCOME, "CONSOLIDATED", "crore", {"profit_before_tax": 4999}, page=3)
    cf = block(tx.CASH_FLOW, None, "crore", {"profit_before_tax": 4321}, page=9)
    infer_missing_basis([other, pbt, cf])
    assert cf.basis == "STANDALONE"


def test_a_basis_found_for_one_table_can_anchor_the_next():
    """segment <- income (stated), then cash flow <- that segment is not an anchor, but balance
    sheet <- cash flow is: cash at end equals balance-sheet cash."""
    pbt = block(tx.INCOME, "CONSOLIDATED", "crore", {"profit_before_tax": 4321}, page=2)
    cf = block(
        tx.CASH_FLOW, None, "crore", {"profit_before_tax": 4321, "cash_at_end": 7777}, page=9
    )
    bs = block(tx.BALANCE_SHEET, None, "crore", {"cash_and_cash_equivalents": 7777}, page=4)
    infer_missing_basis([pbt, cf, bs])
    assert cf.basis == "CONSOLIDATED" and bs.basis == "CONSOLIDATED"


def test_conflicting_evidence_leaves_the_basis_unstated():
    seg = block(
        tx.SEGMENT,
        None,
        "crore",
        {"segment_revenue_after_elimination": 12345, "segment_assets_total": 777777},
    )
    infer_missing_basis(
        [
            income("CONSOLIDATED", 12345),
            block(tx.BALANCE_SHEET, "STANDALONE", "crore", {"total_assets": 777777}),
            seg,
        ]
    )
    assert seg.basis is None, "revenue says consolidated, assets say standalone"
