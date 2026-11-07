"""Recognise statement titles, reporting units and consolidated/standalone basis from text.

Indian PDFs often print the rupee sign with an old font that extracts as a backtick (`), so "`"
is treated as a rupee marker alongside "₹", "Rs." and "INR".
"""

import re

from app.docintel.taxonomy import BALANCE_SHEET, CASH_FLOW, INCOME

_CUR = r"(?:₹|\brs\b\.?|\binr\b|`|\brupees\b)"
_UNIT_WORD = r"(crores?|cr\b|lakhs?|lacs?|millions?|mn\b|billions?|bn\b|thousands?|'000s?|000s)"
_UNIT_PATTERNS = [
    re.compile(_CUR + r"\s*(?:in\s+)?" + _UNIT_WORD),
    re.compile(r"\bin\s+" + _CUR + r"?\s*" + _UNIT_WORD),
    re.compile(
        r"\b(?:amounts?|figures|all amounts are)\s+(?:are\s+)?in\s+" + _CUR + r"?\s*" + _UNIT_WORD
    ),
]
_UNIT_CANON = {
    "crore": "crore",
    "crores": "crore",
    "cr": "crore",
    "lakh": "lakh",
    "lakhs": "lakh",
    "lac": "lakh",
    "lacs": "lakh",
    "million": "million",
    "millions": "million",
    "mn": "million",
    "billion": "billion",
    "billions": "billion",
    "bn": "billion",
    "thousand": "thousand",
    "thousands": "thousand",
    "'000": "thousand",
    "'000s": "thousand",
    "000s": "thousand",
}


# OCR drops or garbles the rupee sign: "(fin crore", "(f in crore}", "{t in crore". Accepted ONLY
# for text that came from OCR: one stray character glued to "in" before the unit word.
_OCR_UNIT = re.compile(r"[\(\{\[~]\s*\S?\s?in\s+" + _UNIT_WORD)


def find_units(text: str, ocr: bool = False) -> set[str]:
    """All reporting units stated in the text ('crore', 'lakh', 'million', ...)."""
    low = text.lower().replace("’", "'").replace("‘", "'")
    found = set()
    patterns = [*_UNIT_PATTERNS, _OCR_UNIT] if ocr else _UNIT_PATTERNS
    for pattern in patterns:
        for match in pattern.finditer(low):
            unit = _UNIT_CANON.get(match.group(1).strip())
            if unit:
                found.add(unit)
    return found


def find_basis(text: str) -> set[str]:
    low = text.lower()
    found = set()
    if re.search(r"\bconsolidated\b", low):
        found.add("CONSOLIDATED")
    if re.search(r"\bstand[\s-]?alone\b", low):
        found.add("STANDALONE")
    return found


_TITLES = {
    INCOME: [
        r"statement of (the )?(standalone |consolidated )?profit and loss",
        r"profit and loss account",
        r"statement of (standalone |consolidated )?income\b",
        r"\b(un)?audited\b.*\b(financial )?results\b",
        r"\bfinancial results\b",
        r"\bresults for the (quarter|half|year|period|nine months|three months|six months)\b",
    ],
    BALANCE_SHEET: [
        r"\bbalance sheet\b",
        r"statement of (standalone |consolidated )?assets and liabilities",
    ],
    CASH_FLOW: [
        r"\bcash flows? statements?\b",
        r"statement of (standalone |consolidated )?cash flows?\b",
    ],
}
_TITLE_RE = {k: [re.compile(p) for p in v] for k, v in _TITLES.items()}
# Sentences in notes mention "financial results" too; these words mark prose, not a title.
_NOT_TITLE = re.compile(
    r"\b(segment|reviewed|approved|taken on record|in accordance|pursuant|regulation 33|"
    r"have been|has been|were|notes? to|forming part|refer|significant accounting|"
    r"integral part|referred to|carried (over|forward)|brought forward|balance in|transfer(red)? to"
    r"|published|circular)\b"
)
# A title never carries an amount; "Balance carried over to balance sheet 1,285,442,549" is a row.
_AMOUNT = re.compile(r"(?<![\d.])\d{1,3}(,\d{2,3})+(\.\d+)?(?![\d.])|\b\d+\.\d{2}\b(?!\.)")
MAX_TITLE_WORDS = 22


def classify_title(text: str) -> str | None:
    """Statement type if the line reads like a statement title, else None.

    Order matters for lines such as "Statement of assets and liabilities as per the financial
    results": balance-sheet and cash-flow titles are checked before the generic results title.
    """
    low = " ".join(text.lower().replace("&", " and ").split())
    if not low or len(low.split()) > MAX_TITLE_WORDS or _NOT_TITLE.search(low):
        return None
    if _AMOUNT.search(low):
        return None
    for statement in (BALANCE_SHEET, CASH_FLOW, INCOME):
        if any(p.search(low) for p in _TITLE_RE[statement]):
            return statement
    return None


_DISCLOSURE_TITLES = {
    "SEGMENT": [
        re.compile(r"\bsegment(al)?\s+(results?|information|reporting|revenues?|wise)\b"),
        re.compile(r"\bsegment(al)?[- ]wise\b"),
    ],
    "RATIOS": [
        re.compile(r"\banalytical ratios\b"),
        re.compile(r"\bkey (financial )?ratios\b"),
        re.compile(r"\badditional information\b.*\bratios?\b"),
    ],
    # "SCHEDULE 4 - BORROWINGS", "Note 20: Borrowings" (a title, not a sentence)
    "NOTE": [
        re.compile(r"^schedule\s+\d+[a-z]?\s*[-\u2013:]\s*\S"),
        re.compile(r"^notes?\s+(no\.?\s*)?\d+[a-z]?\s*[-\u2013:.]\s*\S"),
    ],
}
_NOT_DISCLOSURE_TITLE = re.compile(
    r"\b(reviewed|approved|taken on record|in accordance|pursuant|regulation 33|have been|"
    r"has been|were|forming part|refer|significant accounting|integral part|referred to|"
    r"published|circular)\b"
)


def classify_disclosure_title(text: str) -> str | None:
    """SEGMENT / RATIOS / NOTE if the line reads like the title of such a table, else None."""
    low = " ".join(text.lower().replace("&", " and ").split())
    if not low or len(low.split()) > MAX_TITLE_WORDS or _NOT_DISCLOSURE_TITLE.search(low):
        return None
    if _AMOUNT.search(low):
        return None
    for kind, patterns in _DISCLOSURE_TITLES.items():
        if any(p.search(low) for p in patterns):
            return kind
    return None


# Header of a column that holds note references ("Note 24"), matched against a whole phrase.
NOTE_HEADER = re.compile(r"notes?|notes? no\.?|note ref\.?|schedules?|sch\.?")
