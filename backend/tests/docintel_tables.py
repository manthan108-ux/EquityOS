"""Synthetic disclosure pages (segments, ratios, schedules, shareholding, related parties).

Same geometry conventions as `docintel_pages`. Nothing here is real company data; the shapes copy
the layouts found in real filings (see docs/DOCUMENT_INTELLIGENCE.md).
"""

from app.docintel.layout import Word
from tests.docintel_pages import BS_RIGHTS, LABEL_X, RIGHTS, Page, centred, words


def _period_header(page: Page) -> None:
    page.raw(centred(0, 400, "Three months ended"), centred(0, 520, "Year ended"))
    page.raw(
        *[
            words(0, r, t, right=True)
            for t, r in zip(("30.09.2026", "30.09.2025", "31.03.2026"), RIGHTS, strict=True)
        ]
    )


def segment_page(*, assets_total: str = "2,000.00") -> list[Word]:
    """Segment table shaped like ICICI Bank's (enumerated sections, a/b markers, eliminations)."""
    page = Page()
    page.text("Reliance Industries Limited", 200)
    page.text("STANDALONE SEGMENTAL RESULTS", 220)
    page.text("(₹ in crore)", 540, right=True)
    _period_header(page)
    page.text("1. Segment Revenue")
    page.row("a Retail Banking", "100.00", "90.00", "380.00")
    page.row("b Treasury", "60.00", "50.00", "230.00")
    page.row("Total segment revenue", "160.00", "140.00", "610.00")
    page.text("Less: Inter segment")
    page.row("revenue", "40.00", "35.00", "150.00")
    page.row("Income from operations", "120.00", "105.00", "460.00")
    page.text("2. Segment assets")
    page.row("a Retail Banking", "1,200.00", "1,100.00", "1,150.00")
    page.row("b Treasury", "700.00", "650.00", "680.00")
    page.row("c Unallocated", "100.00", "90.00", "95.00")
    page.row("Total segment assets", assets_total, "1,840.00", "1,925.00")
    page.text("3. Segment liabilities")
    page.row("a Retail Banking", "1,000.00", "950.00", "980.00")
    page.row("b Treasury", "500.00", "450.00", "470.00")
    page.row("Total segment liabilities", "1,500.00", "1,400.00", "1,450.00")
    page.row("4. Capital employed", "500.00", "440.00", "475.00")
    return page.items


def ratios_page() -> list[Word]:
    """A ratios table whose column headings sit ABOVE its title and which has no basis title."""
    page = Page()
    page.text("(₹ in crore)", 540, right=True)
    _period_header(page)
    page.text("18. Analytical ratios")
    page.text("i) Percentage of shares held")
    page.row("by Government of India", "0.22%", "0.22%", "0.22%")
    page.text("ii) Capital adequacy ratio")
    page.row("(Basel III)", "16.68%", "15.76%", "17.18%")
    page.row("iii) Return on assets (annualised)", "2.26%", "2.36%", "2.33%")
    page.row("Net worth2", "349,436.61", "301,627.90", "326,794.68")
    page.row("Debt-equity ratio", "0.35", "0.36", "0.37")
    return page.items


def schedule_page(*, other_total: str = "700") -> list[Word]:
    """Two schedules on one page, in whole units, each with TOTAL rows, footnotes below."""
    page = Page()
    page.text("STANDALONE FINANCIAL STATEMENTS OF XYZ LIMITED")
    page.text("(₹ in thousands)", 540, right=True)
    page.raw(words(0, 400, "At", right=True), words(0, 490, "At", right=True))
    page.raw(words(0, 400, "31.03.2026", right=True), words(0, 490, "31.03.2025", right=True))
    page.text("SCHEDULE 4 - BORROWINGS")
    page.text("I. Borrowings in India")
    for label, a, b in (
        ("i) Reserve Bank of India1", "-", "-"),
        ("ii) Other banks", "100", "90"),
        ("iii) Financial institutions", "200", "180"),
        ("TOTAL BORROWINGS IN INDIA", "300", "270"),
    ):
        page.row(label, a, b, rights=BS_RIGHTS)
    page.text("II. Borrowings outside India")
    page.row("i) Bonds and notes", "50", "40", rights=BS_RIGHTS)
    page.row("TOTAL BORROWINGS OUTSIDE INDIA", "50", "40", rights=BS_RIGHTS)
    page.row("TOTAL BORROWINGS", "350", "310", rights=BS_RIGHTS)
    page.text(
        "1. Includes borrowings of 1,234 million from the Reserve Bank in 2026 and 2025 as noted."
    )
    page.text("SCHEDULE 5 - OTHER LIABILITIES")
    page.row("I. Bills payable", "400", "300", rights=BS_RIGHTS)
    page.row("II. Others", "300", "250", rights=BS_RIGHTS)
    page.row("TOTAL OTHER LIABILITIES", other_total, "550", rights=BS_RIGHTS)
    return page.items


def shareholding_page(*, total: str = "1,000") -> list[Word]:
    page = Page()
    page.text("Shareholding pattern of XYZ Limited at March 31, 2026")
    page.raw(
        words(0, LABEL_X, "Shareholder Category"),
        words(0, 400, "No. of Shares", right=True),
        words(0, 490, "% holding", right=True),
    )
    for label, shares, pct in (
        ("Promoters", "400", "40.00"),
        ("FIIs/FPIs", "300", "30.00"),
        ("Mutual Funds", "150", "15.00"),
        ("Bodies Corporate (includes Government Companies,", None, None),
        ("Clearing Members) and others", "100", "10.00"),
        ("Individuals", "50", "5.00"),
        ("Total", total, "100.00"),
    ):
        page.row(label, shares or "", pct or "", rights=BS_RIGHTS)
    return page.items


def roster_page() -> list[Word]:
    """Related-party roster: entity names on the left, 'Nature of relationship' on the right."""
    page = Page()
    page.text("I. Related parties")
    page.raw(words(0, LABEL_X, "Name of the entity"), words(0, 330, "Nature of relationship"))
    for name, relation in (
        ("XYZ Asset Management Company Limited", "Subsidiary"),
        ("XYZ Home Finance Limited", "Subsidiary"),
        ("Arteria Technologies Private Limited", "Associate"),
        ("XYZ Pension Fund Management Limited (erstwhile XYZ", "Subsidiary"),
        ("Prudential Pension Funds Management Company Limited)", ""),
        ("Comm Trade Services Limited4", "Other related entity"),
    ):
        y = page.y
        page.items += words(y, LABEL_X, name)
        if relation:
            page.items += words(y, 330, relation)
        page.y += 14
    return page.items


def related_party_transactions_page() -> list[Word]:
    """A transaction table that also says 'Subsidiary': it must NOT be read as a roster."""
    page = Page()
    page.raw(
        words(0, LABEL_X, "Name of related party"),
        words(0, 200, "Relationship"),
        words(0, 300, "Nature of transaction"),
        words(0, 480, "Amount"),
    )
    for name, rel, nature, amount in (
        ("ABC Retail Limited", "Subsidiary", "Sale of goods or services", "15"),
        ("ABC Digital Limited", "Wholly-Owned Subsidiary", "Interest received", "4"),
    ):
        y = page.y
        page.items += (
            words(y, LABEL_X, name)
            + words(y, 200, rel)
            + words(y, 300, nature)
            + words(y, 480, amount)
        )
        page.y += 14
    return page.items
