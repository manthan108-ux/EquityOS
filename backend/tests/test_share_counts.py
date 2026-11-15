from datetime import date
from decimal import Decimal as D

from app.docintel import share_counts as sc

HDFC = """HDFC Bank Limited Integrated Annual Report 2025-26
SCHEDULE 1 - CAPITAL (C in ?000)
Schedule As at March 31, 2026 As at March 31, 2025
Authorised capital
20,00,00,00,000 (31 March, 2025: 11,90,61,00,000) Equity Shares of ` 1/- each
20,000,000 11,906,100
Issued, subscribed and paid-up capital
15,39,33,68,328 (31 March, 2025: 7,65,22,21,674) Equity Shares of ` 1/- each  fully paid-up
18 (3) 15,393,368 7,652,222
Total 15,393,368 7,652,222"""

KOTAK = """(` in thousands) As at 31st March, 2024 As at 31st March, 2023 Authorised Capital
2,800,000,000 Equity Shares of ` 5/- each 14,000,000 14,000,000
Issued, Subscribed and Paid-up Capital 1,987,920,898 (31st March, 2023: 1,986,556,582)
Equity Shares of ` 5/- each fully paid-up 9,939,604 9,932,783"""

SBI = """As at 31.03.2026 (Current Year) ` As at 31.03.2025 (Previous Year) `
Authorised Capital : 5000,00,00,000 shares of ` 1 each (Previous Year 5000,00,00,000 shares of ` 1 each) 5000,00,00 5000,00,00
Issued Capital : 923,14,02,716 Equity Shares of ` 1 each (Previous Year 892,54,05,164 Equity Shares of ` 1 each) 923,14,03 892,54,05
Subscribed and Paid up Capital : 923,06,17,586 Equity Shares of ` 1 each (Previous Year 892,46,20,034 Equity Shares of ` 1 each) 923,06,18 892,46,20"""


def one(text):
    return sc.find_share_counts([(1, text, "text_layer")])


def test_the_paid_up_count_is_read_with_its_date_and_the_authorised_capital_is_skipped():
    (c,) = one(HDFC)
    assert (c.count, c.as_at, c.face_value) == (15_393_368_328, date(2026, 3, 31), D(1))
    assert c.unit == "thousand" and c.prior_count == 7_65_22_21_674  # the pre-bonus count, as then


def test_a_count_in_thousands_of_rupees_at_face_value_five():
    (c,) = one(KOTAK)
    assert (c.count, c.as_at, c.face_value) == (1_987_920_898, date(2024, 3, 31), D(5))
    assert c.prior_count == 1_986_556_582


def test_the_subscribed_and_paid_up_line_is_used_not_the_issued_line():
    (c,) = one(SBI)
    assert c.count == 9_230_617_586 and c.as_at == date(2026, 3, 31)
    assert c.prior_count == 8_924_620_034


def test_a_count_that_does_not_close_with_the_printed_capital_is_refused():
    # one digit misread: 15,39,33,68,328 -> 15,39,38,68,328 no longer equals 15,393,368
    assert one(HDFC.replace("15,39,33,68,328", "15,39,38,68,328")) == []


def test_a_line_without_the_word_paid_up_is_not_read():
    assert one(HDFC.replace("Issued, subscribed and paid-up capital", "Issued capital")) == []


def test_no_balance_sheet_date_means_no_figure():
    assert one(HDFC.replace("As at March 31, 2026", "").replace("As at March 31, 2025", "")) == []
