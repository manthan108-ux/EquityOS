"""A subsidiary's accounts printed in the parent's annual report are not the parent's."""

import pytest

from app.docintel.entity import check_entity, names_longer_entity

TECHM = {"tech mahindra", "tech mahindra limited"}
TMPV = {"tata motors", "tata motors limited", "tata motors passenger vehicles"}


@pytest.mark.parametrize(
    ("header", "names", "longer"),
    [
        ("TECH MAHINDRA CERIUM PRIVATE LIMITED\nCONSOLIDATED BALANCE SHEET", TECHM, True),
        ("Tech Mahindra (Americas) Inc.\nStatement of profit and loss", TECHM, True),
        ("Tata Motors Finance Limited\nBalance sheet", TMPV, True),
        ("TECH MAHINDRA LIMITED\nConsolidated Balance Sheet", TECHM, False),
        ("Tech Mahindra Ltd.", TECHM, False),
        ("Tech Mahindra Limited and its subsidiaries", TECHM, False),
        ("Tech Mahindra\nConsolidated statement of cash flows", TECHM, False),
        ("Tata Motors Passenger Vehicles Limited\nStatement of profit and loss", TMPV, False),
        ("Tata Motors Limited", TMPV, False),
    ],
)
def test_a_longer_entity_name_is_told_from_the_company_itself(header, names, longer):
    assert names_longer_entity(header, names) is longer


def test_a_subsidiary_statement_page_is_not_confirmed_as_the_parent():
    page = (
        "TECH MAHINDRA CERIUM PRIVATE LIMITED\nCONSOLIDATED STATEMENT OF PROFIT AND LOSS\nI Revenue"
    )
    cover = "Tech Mahindra Limited\nAnnual Report 2022-23\nTech Mahindra Limited"
    result = check_entity(page, cover, TECHM, first_page_text=cover)
    assert not result.confirmed and "longer entity" in result.how


def test_the_parents_own_statement_page_is_still_confirmed():
    page = "TECH MAHINDRA LIMITED\nCONSOLIDATED STATEMENT OF PROFIT AND LOSS"
    assert check_entity(page, "", TECHM).confirmed


def test_a_document_issued_by_a_longer_named_entity_is_not_the_parents():
    first = "Tech Mahindra Cerium Private Limited\nFinancial statements"
    result = check_entity("Balance sheet\n", "Tech Mahindra mentioned once", TECHM, first)
    assert not result.confirmed


def test_a_page_with_no_header_still_falls_back_to_the_cover_as_before():
    cover = "Tech Mahindra Limited annual report"
    assert check_entity("Balance sheet\nAs at 31 March", cover, TECHM).confirmed
