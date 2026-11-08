"""Checks that an INTERPRETATION introduces no number of its own.

The methodology rule (docs/RESEARCH_METHODOLOGY.md section 1): an interpretation may never use a
number that is not a FACT, CALCULATION or ASSUMPTION. This module is the mechanical half of that
rule. It finds every figure written in a sentence and looks for it among the values of the evidence
the sentence cites. A figure that matches nothing is *untraced* and the sentence is refused.

How a figure matches:

* "₹2,345 crore" matches a cited rupee value of 23,450,000,000 (any of thousand / lakh / million /
  crore / billion / lakh crore). A figure with no money word only matches a value of the same
  size, so "12,345" never silently becomes 12,345 crore.
* "18.2%" matches a cited percent value of 18.2, or a cited ratio of 0.182.
* The comparison allows exactly the rounding the writer used: "18.2%" accepts 18.15 to 18.25.
* The sign is ignored ("fell 12%" matches -12): words carry direction, and this check does not
  judge them. Number WORDS ("three segments") are not checked either. See KNOWN_LIMITATIONS.md.

Not counted as figures: calendar years, fiscal tokens (FY26, Q2, H1, 9M), dates, ordinals, list
numbering and references such as "page 228" or "Note 12".
"""

import re
from dataclasses import dataclass
from decimal import Decimal

# Order matters: longer words first.
_SCALES: list[tuple[str, Decimal]] = [
    ("lakh crore", Decimal(10) ** 12),
    ("lakh crores", Decimal(10) ** 12),
    ("trillion", Decimal(10) ** 12),
    ("billion", Decimal(10) ** 9),
    ("crores", Decimal(10) ** 7),
    ("crore", Decimal(10) ** 7),
    ("million", Decimal(10) ** 6),
    ("lakhs", Decimal(10) ** 5),
    ("lakh", Decimal(10) ** 5),
    ("thousand", Decimal(10) ** 3),
    ("bn", Decimal(10) ** 9),
    ("mn", Decimal(10) ** 6),
    ("cr", Decimal(10) ** 7),
]
_SCALE_WORDS = "|".join(re.escape(w) for w, _ in _SCALES)

_NOT_FIGURES = [
    re.compile(r"(?i)\bFY\s?\d{2,4}(?:\s?[-–/]\s?\d{2,4})?"),
    re.compile(r"(?i)\b[QH][1-4]\b|\b9M\b|\b[1-4]Q\b|\bH[12]FY\d{2,4}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(
        r"(?i)\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
        r"[a-z]*\.?,?(?:\s+\d{4})?"
    ),
    re.compile(
        r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
        r"(?:,?\s+\d{4})?"
    ),
    re.compile(r"(?i)\b(?:page|pages|pp?\.|note|schedule|section|table|figure|annexure)\s*\d+"),
    re.compile(r"\b\d+(?:st|nd|rd|th)\b"),
    re.compile(r"(?m)^\s*\d+[.)]\s"),
]
_YEAR = re.compile(r"^(?:19|20)\d{2}$")

_FIGURE = re.compile(
    r"(?P<currency>(?:₹|Rs\.?|INR)\s*)?"
    r"(?P<num>\d+(?:,\d{2,3})*(?:\.\d+)?)"
    rf"(?:\s*(?P<scale>{_SCALE_WORDS})\b)?"
    r"(?:\s*(?P<suffix>%|per\s?cent|percent|x\b|times\b|days\b|bps\b|basis points))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Figure:
    text: str
    value: Decimal  # as written, scale not applied
    decimals: int
    scale: Decimal | None
    kind: str  # money | percent | multiple | days | bps | plain


@dataclass(frozen=True)
class CitedValue:
    evidence_id: int
    value: Decimal
    unit: str  # inr | inr_per_share | percent | ratio | shares | x | days | ...


@dataclass
class ClaimCheck:
    matched: list[tuple[Figure, int]]
    untraced: list[Figure]

    @property
    def ok(self) -> bool:
        return not self.untraced


def find_figures(text: str) -> list[Figure]:
    """Every figure in `text` that must be traceable (see module docstring for exclusions)."""
    cleaned = text
    for pattern in _NOT_FIGURES:
        cleaned = pattern.sub(lambda m: " " * len(m.group(0)), cleaned)
    figures: list[Figure] = []
    for m in _FIGURE.finditer(cleaned):
        raw = m.group("num")
        number = raw.replace(",", "")
        if not number or number.endswith("."):
            continue
        decimals = len(number.split(".")[1]) if "." in number else 0
        scale = None
        if m.group("scale"):
            word = m.group("scale").lower()
            scale = next(s for w, s in _SCALES if w == word)
        suffix = (m.group("suffix") or "").lower()
        if suffix == "%" or (suffix.startswith("per") and suffix.endswith("cent")):
            kind = "percent"
        elif suffix in ("x", "times"):
            kind = "multiple"
        elif suffix == "days":
            kind = "days"
        elif suffix in ("bps", "basis points"):
            kind = "bps"
        elif scale is not None or m.group("currency"):
            kind = "money"
        else:
            kind = "plain"
        if kind == "plain" and decimals == 0 and "," not in raw and _YEAR.match(number):
            continue
        figures.append(
            Figure(
                text=m.group(0).strip(),
                value=Decimal(number),
                decimals=decimals,
                scale=scale,
                kind=kind,
            )
        )
    return figures


def _half_unit(decimals: int) -> Decimal:
    return Decimal(5) * Decimal(10) ** -(decimals + 1)


def _close(written: Decimal, decimals: int, scale: Decimal, actual: Decimal) -> bool:
    """`actual` equals `written` x `scale` up to the rounding the writer used."""
    slack = _half_unit(decimals) * scale
    return abs(abs(written) * scale - abs(actual)) <= slack + Decimal("1e-9")


def _matches(figure: Figure, cited: CitedValue) -> bool:
    unit, value = cited.unit, cited.value
    if figure.kind == "money":
        if unit != "inr" and unit != "inr_per_share":
            return False
        scale = figure.scale or Decimal(1)
        return _close(figure.value, figure.decimals, scale, value)
    if figure.kind == "percent":
        if unit == "percent":
            return _close(figure.value, figure.decimals, Decimal(1), value)
        if unit == "ratio":
            return _close(figure.value, figure.decimals, Decimal("0.01"), value)
        return False
    if figure.kind == "bps":
        if unit == "percent":
            return _close(figure.value, figure.decimals, Decimal("0.01"), value)
        if unit == "ratio":
            return _close(figure.value, figure.decimals, Decimal("0.0001"), value)
        return False
    if figure.kind == "multiple":
        return unit in ("x", "ratio") and _close(figure.value, figure.decimals, Decimal(1), value)
    if figure.kind == "days":
        return unit in ("days", "ratio") and _close(
            figure.value, figure.decimals, Decimal(1), value
        )
    # A plain number matches only a value of the same size, whatever its unit.
    return _close(figure.value, figure.decimals, Decimal(1), value)


def check_figures(text: str, cited: list[CitedValue]) -> ClaimCheck:
    matched: list[tuple[Figure, int]] = []
    untraced: list[Figure] = []
    for figure in find_figures(text):
        hit = next((c for c in cited if _matches(figure, c)), None)
        if hit is None:
            untraced.append(figure)
        else:
            matched.append((figure, hit.evidence_id))
    return ClaimCheck(matched=matched, untraced=untraced)


__all__ = ["ClaimCheck", "CitedValue", "Figure", "check_figures", "find_figures"]
