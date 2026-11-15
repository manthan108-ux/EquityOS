# ruff: noqa: E501
"""Consolidates "not available" statements.

A report used to repeat "not stored / not available / cannot be established" under almost every heading.
This pass moves those standalone sentences out of the body into one appendix, grouped by the heading they were
under, so each gap is stated once, with the section it affects. Tables are left alone (a missing cell stays "n/a"
in its table); a heading left with nothing under it is dropped from the body.
"""

from __future__ import annotations

import re

from reportlab.platypus import CondPageBreak, PageBreak, Paragraph, Spacer

GAP = re.compile(
    r"(not available|not stored|not completed|cannot be established|none established|"
    r"no [^.]{0,60}\b(is|are|was|were)\b[^.]{0,20}stored|not assessed|could not be built|"
    r"is not used|not applied|not shown|withheld|no stored|are not stored|was not run|has not been run)",
    re.I,
)
MAX_LEN = 520
BODY_STYLES = {"body", "small", "bullet"}
HEADINGS = {"h1", "h2"}


def _is_gap(f) -> bool:
    if getattr(f, "_gap", False):
        return True
    if not isinstance(f, Paragraph) or f.style.name not in BODY_STYLES:
        return False
    text = f.getPlainText().strip()
    return 0 < len(text) < MAX_LEN and bool(GAP.search(text))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text)).strip().lower()


def consolidate(story: list, st, pruned: list[str] | None = None) -> list:
    out: list = []
    gaps: dict[str, list[str]] = {}
    seen: set[str] = set()
    heading = "Report"
    for f in story:
        if isinstance(f, Paragraph) and f.style.name in HEADINGS:
            heading = f.getPlainText().strip()
        if _is_gap(f):
            text = f.getPlainText().strip().lstrip("- ").strip()
            key = _norm(text)
            if key not in seen:
                seen.add(key)
                gaps.setdefault(heading, []).append(text)
            continue
        out.append(f)
    # a heading with nothing under it (next flowable is a heading, a break or the end) is dropped
    cleaned: list = []
    for i, f in enumerate(out):
        if isinstance(f, Paragraph) and f.style.name == "h2":
            nxt = next((g for g in out[i + 1 :] if not isinstance(g, Spacer)), None)
            if nxt is None or isinstance(nxt, PageBreak | CondPageBreak) or (
                isinstance(nxt, Paragraph) and nxt.style.name in HEADINGS
            ):
                continue
        cleaned.append(f)
    if not gaps and not pruned:
        return cleaned
    cleaned.append(PageBreak())
    cleaned.append(Paragraph("Appendix F - Evidence gaps by section", st["h1"]))
    cleaned.append(
        Paragraph(
            "Each gap is stated once here, under the section it affects, instead of repeating inside the body. "
            "A gap means the evidence is not stored or a method could not be completed; no figure has been assumed "
            "in its place. Missing documents are requested from the end user when a report is generated.",
            st["body"],
        )
    )
    for title, items in gaps.items():
        cleaned.append(Paragraph(f"<b>{title.replace('&', '&amp;')}</b>", st["small"]))
        for t in items:
            safe = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            cleaned.append(Paragraph(safe, st["bullet"], bulletText="-"))
    if pruned:
        names = "; ".join(sorted(set(x.replace("&", "&amp;") for x in pruned)))
        cleaned.append(Paragraph("<b>Table rows and columns left out</b>", st["small"]))
        cleaned.append(Paragraph(f"Left out of the report tables because no value is stored: {names}.", st["bullet"], bulletText="-"))  # fmt: skip
    return cleaned
