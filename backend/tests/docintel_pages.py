"""Synthetic statement pages (word boxes) for document-intelligence tests.

Coordinates mimic a PDF page in points: characters are 5 pt wide, words are separated by one
character, values are right-aligned at fixed column edges. Nothing here is real company data.
"""

from app.docintel.layout import Word

CW = 5.0
LABEL_X = 60.0
RIGHTS = (380.0, 460.0, 540.0)


def words(y: float, x: float, text: str, *, right: bool = False) -> list[Word]:
    """Words of `text` starting at x (or ending at x when right=True) on line y."""
    start = x - len(text) * CW if right else x
    out, cur = [], start
    for token in text.split(" "):
        width = len(token) * CW
        out.append(Word(cur, y, cur + width, y + 8, token))
        cur += width + CW
    return out


def centred(y: float, centre: float, text: str) -> list[Word]:
    return words(y, centre - len(text) * CW / 2, text)


class Page:
    def __init__(self) -> None:
        self.y = 30.0
        self.items: list[Word] = []

    def text(self, text: str, x: float = LABEL_X, *, right: bool = False) -> "Page":
        self.items += words(self.y, x, text, right=right)
        self.y += 14
        return self

    def raw(self, *parts: list[Word]) -> "Page":
        for part in parts:
            self.items += [Word(w.x0, self.y, w.x1, self.y + 8, w.text) for w in part]
        self.y += 14
        return self

    def row(self, label: str, *values: str, rights=RIGHTS, note: str | None = None) -> "Page":
        if label:
            self.items += words(self.y, LABEL_X, label)
        if note:
            self.items += words(self.y, 250, note, right=True)
        for value, right in zip(values, rights, strict=False):
            if value:
                self.items += words(self.y, right, value, right=True)
        self.y += 14
        return self


INCOME_ROWS = [
    ("Revenue from operations", "1,000.00", "900.00", "3,800.00"),
    ("Other income", "50.00", "40.00", "200.00"),
    ("Total income (I+II)", "1,050.00", "940.00", "4,000.00"),
    ("Expenses", None, None, None),
    ("(a) Cost of materials consumed", "400.00", "380.00", "1,600.00"),
    ("(b) Employee benefits expense", "100.00", "95.00", "390.00"),
    ("(c) Finance costs", "20.00", "20.00", "80.00"),
    ("(d) Depreciation and amortisation expense", "30.00", "28.00", "120.00"),
    ("(e) Other expenses", "200.00", "177.00", "810.00"),
    ("Total expenses", "750.00", "700.00", "3,000.00"),
    ("Profit before exceptional items and tax (III-IV)", "300.00", "240.00", "1,000.00"),
    ("Exceptional items", "-", "-", "(50.00)"),
    ("Profit/(loss) before tax", "300.00", "240.00", "950.00"),
    ("Tax expense", None, None, None),
    ("Current tax", "70.00", "55.00", "240.00"),
    ("Deferred tax", "5.00", "5.00", "10.00"),
    ("Total tax expense", "75.00", "60.00", "250.00"),
    ("Net profit for the period", "225.00", "180.00", "700.00"),
    ("Other comprehensive income (net of tax)", "5.00", "(2.00)", "10.00"),
    ("Total comprehensive income for the period", "230.00", "178.00", "710.00"),
    ("Net profit attributable to:", None, None, None),
    ("Owners of the Company", "220.00", "175.00", "690.00"),
    ("Non-controlling interests", "5.00", "5.00", "10.00"),
    ("Paid-up equity share capital (Face value ₹ 10 each)", "100.00", "100.00", "100.00"),
    ("Earnings per share (not annualised for quarters)", None, None, None),
    ("(a) Basic (₹)", "22.00", "17.50", "69.00"),
    ("(b) Diluted (₹)", "21.90", "17.40", "68.80"),
]


def income_page(
    *,
    company: str = "Reliance Industries Limited",
    title: str = "Statement of Consolidated Unaudited Financial Results for the quarter ended 30 September 2026",
    unit: str | None = "(₹ in crore)",
    rows=INCOME_ROWS,
    second_header: tuple[str, str, str] = ("30.09.2026", "30.09.2025", "31.03.2026"),
    spanning: tuple[tuple[str, float], ...] = (
        ("Three months ended", 400.0),
        ("Year ended", 520.0),
    ),
) -> list[Word]:
    page = Page()
    page.text(company, 200)
    page.text(title)
    if unit:
        page.text(unit, 540, right=True)
    page.raw(words(0, LABEL_X, "Particulars"), *[centred(0, c, t) for t, c in spanning])
    page.raw(*[words(0, r, t, right=True) for t, r in zip(second_header, RIGHTS, strict=True)])
    page.raw(
        *[
            words(0, r, t, right=True)
            for t, r in zip(("(Unaudited)", "(Unaudited)", "(Audited)"), RIGHTS, strict=True)
        ]
    )
    for label, *values in rows:
        page.row(label, *[v or "" for v in values])
    page.text("Notes: The above results were reviewed by the Audit Committee on 20 October 2026.")
    return page.items


BS_RIGHTS = (400.0, 490.0)


def balance_sheet_page(*, total_equity_liab: str = "8,500.00") -> list[Word]:
    page = Page()
    page.text("Standalone Balance Sheet as at 31 March 2026")
    page.text("(₹ in lakh)", 490, right=True)
    page.raw(
        words(0, LABEL_X, "Particulars"),
        words(0, 250, "Note", right=True),
        words(0, 400, "As at", right=True),
        words(0, 490, "As at", right=True),
    )
    page.raw(words(0, 400, "31 March 2026", right=True), words(0, 490, "31 March 2025", right=True))
    rows = [
        ("ASSETS", None, None, None),
        ("Non-current assets", None, None, None),
        ("Property, plant and equipment", "3", "5,000.00", "4,500.00"),
        ("Investments", "4", "1,000.00", "900.00"),
        ("Total non-current assets", None, "6,000.00", "5,400.00"),
        ("Current assets", None, None, None),
        ("Inventories", None, "800.00", "700.00"),
        ("Trade receivables", "5", "1,200.00", "1,100.00"),
        ("Cash and cash equivalents", "6", "500.00", "300.00"),
        ("Total current assets", None, "2,500.00", "2,100.00"),
        ("Total assets", None, "8,500.00", "7,500.00"),
        ("EQUITY AND LIABILITIES", None, None, None),
        ("Equity", None, None, None),
        ("Equity share capital", "7", "1,000.00", "1,000.00"),
        ("Other equity", None, "4,000.00", "3,500.00"),
        ("Total equity", None, "5,000.00", "4,500.00"),
        ("Liabilities", None, None, None),
        ("Non-current liabilities", None, None, None),
        ("Borrowings", None, "1,500.00", "1,400.00"),
        ("Total non-current liabilities", None, "1,500.00", "1,400.00"),
        ("Current liabilities", None, None, None),
        ("Borrowings", None, "600.00", "500.00"),
        ("Trade payables", None, "1,400.00", "1,100.00"),
        ("Total current liabilities", None, "2,000.00", "1,600.00"),
        ("Total equity and liabilities", None, total_equity_liab, "7,500.00"),
    ]
    for label, note, *values in rows:
        page.row(label, *[v or "" for v in values], rights=BS_RIGHTS, note=note)
    return page.items


def bank_results_page(*, company: str = "Reliance Industries Limited") -> list[Word]:
    """A bank-format results page shaped like ICICI Bank's filings (synthetic numbers).

    Real-world traits reproduced: the date of each column split over two header lines ("September"
    above "30, 2026"), a fiscal label without "FY" ("(Q2-2027)"), repeated "(Unaudited)" flags,
    a header row of years under "Particulars", upper-case labels wrapped over three lines with
    serial numbers, formula references "(4)+(5)" and ".." for nil.
    """
    r = (380.0, 460.0, 540.0)
    page = Page()
    page.text(company, 200)
    page.text("STANDALONE FINANCIAL RESULTS", 220)
    page.text("(₹ in crore)", 540, right=True)
    page.raw(centred(0, 400, "Three months ended"), centred(0, 520, "Year ended"))
    page.raw(
        words(0, 40, "Sr."),
        *[
            words(0, x, t, right=True)
            for t, x in zip(("September", "September", "March 31,"), r, strict=True)
        ],
    )
    page.raw(
        words(0, 40, "No."),
        words(0, 95, "Particulars"),
        *[
            words(0, x, t, right=True)
            for t, x in zip(("30, 2026", "30, 2025", "2026"), r, strict=True)
        ],
    )
    page.raw(
        *[
            words(0, x, t, right=True)
            for t, x in zip(("(Q2-2027)", "(Q2-2026)", "(FY2026)"), r, strict=True)
        ]
    )
    # "(Unaudited) (Unaudited) (Audited)" printed close together, so they merge into one phrase
    flags = [
        Word(338, 0, 378, 8, "(Unaudited)"),
        Word(381, 0, 421, 8, "(Unaudited)"),
        Word(424, 0, 459, 8, "(Audited)"),
    ]
    # put the flags over the right columns: shift so each sits under its column
    flags = [
        Word(w.x0 + dx, 0, w.x1 + dx, 8, w.text) for w, dx in zip(flags, (0, 40, 80), strict=True)
    ]
    page.raw(flags)
    rows = [
        ("1. Interest earned", "100.00", "90.00", "380.00"),
        ("2. Other income", "20.00", "18.00", "75.00"),
        ("3. TOTAL INCOME (1)+(2)", "120.00", "108.00", "455.00"),
        ("4. Interest expended", "50.00", "45.00", "190.00"),
        ("5. Operating expenses", "30.00", "28.00", "115.00"),
        ("TOTAL EXPENDITURE", None, None, None),
        ("6. EXCLUDING PROVISIONS AND", None, None, None),
        ("CONTINGENCIES (4)+(5)", "80.00", "73.00", "305.00"),
        ("OPERATING PROFIT BEFORE", None, None, None),
        ("7. PROVISIONS AND", None, None, None),
        ("CONTINGENCIES (3)–(6)", "40.00", "35.00", "150.00"),
        ("8. Provisions (other than tax) and", None, None, None),
        ("contingencies", "5.00", "4.00", "20.00"),
        ("9. Exceptional items", "..", "..", ".."),
        ("10. PROFIT FROM ORDINARY", None, None, None),
        ("ACTIVITIES BEFORE TAX (7)–(8)", "35.00", "31.00", "130.00"),
        ("11. Tax expense (a)+(b)", "9.00", "8.00", "33.00"),
        ("12. NET PROFIT FROM ORDINARY", None, None, None),
        ("ACTIVITIES AFTER TAX (10)–(11)", "26.00", "23.00", "97.00"),
        ("13. Extraordinary items (net of tax expense)", "..", "..", ".."),
        ("14. NET PROFIT FOR THE PERIOD", None, None, None),
        ("(12)–(13)", "26.00", "23.00", "97.00"),
    ]
    for label, *values in rows:
        page.row(label, *[v or "" for v in values], rights=r)
    return page.items


def cash_flow_pages() -> tuple[list[Word], list[Word]]:
    """A cash flow statement that continues on a second page (both pages repeat the header)."""

    def header(page: Page) -> None:
        page.text("Reliance Industries Limited", 200)
        page.text("Standalone Cash Flow Statement for the year ended 31 March 2026")
        page.text("(₹ in crore)", 540, right=True)
        page.raw(words(0, 460, "Year ended", right=True), words(0, 540, "Year ended", right=True))
        page.raw(words(0, 460, "31.03.2026", right=True), words(0, 540, "31.03.2025", right=True))

    rights = (460.0, 540.0)
    first, second = Page(), Page()
    header(first)
    for label, *values in [
        ("Net cash from operating activities (A)", "500.00", "450.00"),
        ("Purchase of property, plant and equipment", "(200.00)", "(180.00)"),
        ("Net cash used in investing activities (B)", "(300.00)", "(250.00)"),
        ("Dividends paid", "(50.00)", "(40.00)"),
    ]:
        first.row(label, *values, rights=rights)
    header(second)
    for label, *values in [
        ("Net cash used in financing activities (C)", "(100.00)", "(90.00)"),
        ("Net increase/(decrease) in cash and cash equivalent (A+B+C)", "100.00", "110.00"),
        ("Cash and cash equivalents at the beginning of the year", "400.00", "290.00"),
        ("Cash and cash equivalents at the end of the year", "500.00", "400.00"),
    ]:
        second.row(label, *values, rights=rights)
    return first.items, second.items
