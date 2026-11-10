# ruff: noqa: E501 - layout code with long literal strings
"""PDF rendering of a stored research report (ReportLab).

The PDF is a rendering of the report's stored content and nothing else: no figure is calculated,
estimated or added here, and every number keeps the period, basis and source the report gave it.
The structure is the same for an available-evidence and a complete report, so a reader can see at
once what is missing: every gap is printed in the section it affects, in a visibly different box,
and again in the Limitations section.

Text is restricted to what the standard PDF fonts can draw (Latin-1 plus common punctuation);
anything else a source document contained is replaced rather than printed as a broken glyph.
"""

import io
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

CRORE = Decimal(10) ** 7
INK = colors.HexColor("#1f2937")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d1d5db")
GAP_FILL = colors.HexColor("#fef3c7")
GAP_EDGE = colors.HexColor("#d97706")
OK_FILL = colors.HexColor("#ecfdf5")
STATUS_COLOUR = {
    "COMPLETE": colors.HexColor("#047857"),
    "PARTIAL": colors.HexColor("#b45309"),
    "UNAVAILABLE": colors.HexColor("#6b7280"),
    "DERIVED": colors.HexColor("#0369a1"),
    "NOT_REPORTED": colors.HexColor("#6b7280"),
}


def clean(text: object) -> str:
    """Text the base-14 fonts can draw, XML-escaped for Paragraph markup."""
    value = "" if text is None else str(text)
    value = value.replace("₹", "Rs. ").replace("‑", "-").replace(" ", " ")
    value = value.encode("cp1252", "replace").decode("cp1252")
    return escape(value)


def money(value: Decimal) -> str:
    """Indian digit grouping: 12,34,567.89."""
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join([*parts, tail])
    return f"{sign}{whole}.{frac}"


def fmt_value(value: str | None, unit: str | None) -> str:
    if value is None:
        return "not available"
    try:
        number = Decimal(value)
    except InvalidOperation:
        return clean(value)
    if unit == "inr":
        return f"Rs. {money(number / CRORE)} crore"
    if unit == "inr_per_share":
        return f"Rs. {money(number)}"
    if unit == "percent":
        return f"{number.quantize(Decimal('0.01'))}%"  # display only; the stored value is exact
    if unit in ("x", "ratio"):
        return f"{number.quantize(Decimal('0.01'))}x"
    if unit == "shares":
        return f"{number:,.0f} shares"
    return f"{number.normalize():f}" + (f" {unit}" if unit else "")


def styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "body", parent=base["BodyText"], fontName="Helvetica", fontSize=9, leading=12,
        textColor=INK, alignment=TA_LEFT,
    )  # fmt: skip
    return {
        "body": body,
        "small": ParagraphStyle("small", parent=body, fontSize=7.5, leading=10, textColor=MUTED),
        "title": ParagraphStyle(
            "title", parent=body, fontName="Helvetica-Bold", fontSize=20, leading=24, spaceAfter=4
        ),
        "h1": ParagraphStyle(
            "h1",
            parent=body,
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            spaceBefore=12,
            spaceAfter=4,
        ),  # fmt: skip
        "h2": ParagraphStyle(
            "h2", parent=body, fontName="Helvetica-Bold", fontSize=10, leading=13, spaceBefore=6
        ),
        "cell": ParagraphStyle("cell", parent=body, fontSize=8.5, leading=11),
        "cellb": ParagraphStyle(
            "cellb", parent=body, fontSize=8.5, leading=11, fontName="Helvetica-Bold"
        ),
        "quote": ParagraphStyle(
            "quote", parent=body, fontSize=8, leading=10.5, leftIndent=8, textColor=MUTED
        ),
    }


class _Numbered(canvas.Canvas):
    """Adds 'page X of Y' and the report identity to every page."""

    footer = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont("Helvetica", 7)
            self.setFillColor(MUTED)
            self.drawString(18 * mm, 10 * mm, self.footer[:150])
            self.drawRightString(A4[0] - 18 * mm, 10 * mm, f"page {self._pageNumber} of {total}")
            self.setStrokeColor(RULE)
            self.line(18 * mm, 13 * mm, A4[0] - 18 * mm, 13 * mm)
            super().showPage()
        super().save()


def _box(paragraphs: list, fill, edge) -> Table:
    t = Table([[paragraphs]], colWidths=[A4[0] - 36 * mm])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), fill),
                ("BOX", (0, 0), (-1, -1), 0.8, edge),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return t


def _source(item: dict) -> str:
    src = item.get("source") or {}
    bits = []
    if src.get("page"):
        bits.append(f"p.{src['page']}")
    if src.get("evidence_id"):
        bits.append(f"evidence #{src['evidence_id']}")
    if src.get("provider") and not src.get("document_id"):
        bits.append(str(src["provider"]).lower().replace("_", " "))
    return ", ".join(bits)


def _items_table(items: list[dict], st: dict) -> Table | None:
    if not items:
        return None
    rows = [[Paragraph(h, st["cellb"]) for h in ("Item", "Value", "Period / basis", "Source")]]
    for it in items:
        period = " ".join(
            x for x in (it.get("period_end"), (it.get("basis") or "").lower() or None) if x
        )
        if it.get("dimension"):
            period += f" ({str(it['dimension']).replace('_', ' ')})"
        if it.get("period_note"):
            period += f" - {it['period_note']}"
        rows.append(
            [
                Paragraph(clean(it["label"]), st["cell"]),
                Paragraph(clean(fmt_value(it.get("value"), it.get("unit"))), st["cell"]),
                Paragraph(clean(period or "-"), st["cell"]),
                Paragraph(clean(_source(it) or "-"), st["cell"]),
            ]
        )
    t = Table(rows, colWidths=[52 * mm, 38 * mm, 50 * mm, 30 * mm], repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
                ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ]
        )
    )
    return t


def _gap_flowables(gaps: list[dict], st: dict) -> list:
    out = []
    for g in gaps:
        lines = [
            Paragraph(
                f"<b>{clean(g['label'])}</b> - {clean(g['importance']).lower()}, NOT AVAILABLE",
                st["cell"],
            )
        ]
        if g.get("reason"):
            lines.append(Paragraph(clean(g["reason"]), st["small"]))
        if g.get("affects"):
            lines.append(Paragraph("Affects: " + clean(g["affects"]), st["small"]))
        out += [Spacer(1, 3), _box(lines, GAP_FILL, GAP_EDGE)]
    return out


def _section(sec: dict, st: dict) -> list:
    colour = STATUS_COLOUR.get(sec["status"], MUTED).hexval()[2:]
    head = Paragraph(
        f"{clean(sec['title'])} <font size=8 color='#{colour}'>[{clean(sec['status']).lower().replace('_', ' ')}]</font>",
        st["h1"],
    )
    parts: list = [head]
    table = _items_table(sec.get("items", []), st)
    if table is not None:
        parts.append(table)
    company = sec.get("company")
    if company:
        parts.append(
            Paragraph(
                clean(
                    f"{company['name']} ({company['legal_name']}) - sector {company.get('sector') or 'not classified'}, "
                    f"industry {company.get('industry') or 'not classified'}; research profile: {company['profile']}. "
                    f"Listings: {', '.join(company.get('symbols') or []) or 'n/a'}."
                ),
                st["body"],
            )
        )
    series = {k: v for k, v in (sec.get("series") or {}).items() if v}
    if series:
        years = sorted({r["fiscal_year"] for rows in series.values() for r in rows})
        rows = [
            [Paragraph("Annual series", st["cellb"])] + [Paragraph(y, st["cellb"]) for y in years]
        ]
        for name, vals in series.items():
            by = {r["fiscal_year"]: r for r in vals}
            rows.append(
                [Paragraph(clean(name.replace("_", " ")), st["cell"])]
                + [
                    Paragraph(
                        clean(
                            fmt_value(by[y]["value"], by[y].get("unit")).replace(" crore", "")
                            if y in by
                            else "-"
                        ),
                        st["cell"],
                    )
                    for y in years
                ]
            )
        t = Table(rows, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]
            )
        )
        parts += [
            Spacer(1, 4),
            t,
            Paragraph("Values in Rs. crore, consolidated-first by the chosen basis.", st["small"]),
        ]
    segments = sec.get("segments") or []
    if segments:
        rows = [[Paragraph(h, st["cellb"]) for h in ("Segment", "Measure", "Value")]]
        for g in segments[:30]:
            rows.append(
                [
                    Paragraph(clean(g.get("segment") or "Total"), st["cell"]),
                    Paragraph(
                        clean(str(g["metric"]).replace("SEGMENT.", "").replace("_", " ")),
                        st["cell"],
                    ),
                    Paragraph(clean(fmt_value(g["value"], g.get("unit"))), st["cell"]),
                ]
            )
        t = Table(rows, colWidths=[70 * mm, 55 * mm, 45 * mm], repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
                ]
            )
        )
        parts += [Spacer(1, 4), t]
    peers = sec.get("peers")
    if peers:
        if peers.get("available"):
            names = ", ".join(p["name"] for p in peers.get("peers", []))
            text = f"{peers.get('peer_count')} peer(s): {names}." + (
                " Thin group: fewer peers than the minimum for a reliable comparison."
                if peers.get("thin")
                else ""
            )
        else:
            text = peers.get("reason") or "No peer set is available."
        parts.append(Paragraph(clean(text), st["body"]))
    score = sec.get("score")
    if score:
        if score.get("available"):
            text = (
                f"{score.get('methodology')} score {score.get('overall')} (confidence "
                f"{(score.get('confidence') or 'n/a').lower()}). This is the financial score; it is "
                "unrelated to research readiness."
            )
        else:
            text = f"Score unavailable: {score.get('reason') or 'not calculated'}"
        parts.append(Paragraph(clean(text), st["body"]))
    for key, label in (
        ("management_discussion", "Management discussion"),
        ("auditor_report", "Auditor's report"),
    ):
        node = sec.get(key)
        if node:
            page = (node.get("source") or {}).get("page")
            parts.append(
                Paragraph(
                    f"<b>{label}</b> (p.{page or '?'}; quoted from the document, not summarised)",
                    st["small"],
                )
            )
            if node.get("excerpt"):
                parts.append(Paragraph(clean(node["excerpt"][:900]), st["quote"]))
    parts += _gap_flowables(sec.get("gaps", []), st)
    return parts


def render(report: dict, *, version: int, mode: str, report_id: str, content_hash: str) -> bytes:
    """The PDF bytes of a stored report's content."""
    meta, ready, lim = report["meta"], report["readiness"], report["limitations"]
    st = styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
        title=f"{meta['company']} - research report v{version}", author="EquityOS", invariant=1,
    )  # fmt: skip
    complete = mode == "COMPLETE" and meta.get("complete")
    story: list = [
        Paragraph(clean(meta["company"]), st["title"]),
        Paragraph(
            "COMPLETE RESEARCH REPORT" if complete else "AVAILABLE-EVIDENCE REPORT (not complete)",
            st["h2"],
        ),
        Spacer(1, 4),
        _box(
            [Paragraph(clean(meta["banner"]), st["body"])],
            OK_FILL if complete else GAP_FILL,
            colors.HexColor("#047857") if complete else GAP_EDGE,
        ),
        Spacer(1, 6),
    ]
    prefs = meta.get("preferences") or {}
    facts = [
        ["As of", clean(meta["as_of"])],
        ["Report version", f"v{version}"],
        ["Accounting basis", clean(str(prefs.get("basis", "CONSOLIDATED")).lower())],
        ["Reporting period", clean(prefs.get("period_label", "latest available quarter"))],
        ["Optional items", "included" if prefs.get("include_optional") else "not requested"],
        [
            "Research readiness",
            clean(
                f"{ready['score']} / 100 - {ready['eligibility'].lower()} research, evidence confidence "
                f"{ready['confidence'].lower()}"
            ),
        ],
    ]
    t = Table(
        [[Paragraph(a, st["cellb"]), Paragraph(b, st["cell"])] for a, b in facts],
        colWidths=[40 * mm, 130 * mm],
    )
    t.setStyle(
        TableStyle(
            [("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        )
    )
    story += [t, Spacer(1, 4)]
    story.append(
        Paragraph(
            "Research readiness measures the evidence behind this report, not the company; it is "
            "unrelated to the company's financial score.",
            st["small"],
        )
    )
    comps = ready.get("components") or {}
    if comps:
        rows = [[Paragraph(h, st["cellb"]) for h in ("Readiness component", "Score", "Basis")]]
        for c in comps.values():
            rows.append(
                [
                    Paragraph(clean(c["label"]), st["cell"]),
                    Paragraph("n/a" if c["score"] is None else clean(c["score"]), st["cell"]),
                    Paragraph(clean(c["detail"]), st["cell"]),
                ]
            )
        ct = Table(rows, colWidths=[55 * mm, 18 * mm, 97 * mm], repeatRows=1)
        ct.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
                ]
            )
        )
        story += [Spacer(1, 6), ct]
    crit = lim.get("missing_critical", [])
    if crit:
        story += [Spacer(1, 6)] + _gap_flowables(
            [
                {
                    "label": "Critical items not available",
                    "importance": "critical",
                    "reason": ", ".join(g["label"] for g in crit),
                }
            ],
            st,
        )
    story.append(Spacer(1, 8))
    for sec in report["sections"]:
        parts = _section(sec, st)
        story.append(KeepTogether(parts[:2]))  # a heading never ends a page alone
        story += parts[2:]
    story.append(Paragraph("Limitations", st["h1"]))
    blocks = [
        ("Critical items not available", lim.get("missing_critical", [])),
        ("Important items not available", lim.get("missing_important", [])),
        ("Optional items not available", lim.get("missing_optional", [])),
        ("Calculations that could not be made", lim.get("unavailable_calculations", [])),
        ("Incomplete valuation components", lim.get("incomplete_valuation", [])),
    ]
    for title, rows in blocks:
        if rows:
            story.append(Paragraph(f"<b>{title}</b>", st["body"]))
            story.append(
                Paragraph(
                    clean(
                        "; ".join(
                            r["label"] + (f" ({r['reason']})" if r.get("reason") else "")
                            for r in rows
                        )
                    ),
                    st["small"],
                )
            )
            story.append(Spacer(1, 3))
    if lim.get("unavailable_scores"):
        story.append(Paragraph("<b>Score</b>", st["body"]))
        story.append(Paragraph(clean(lim["unavailable_scores"]), st["small"]))
    if lim.get("limits_on_conclusions"):
        story.append(Paragraph("<b>Limits on conclusions</b>", st["body"]))
        for line in lim["limits_on_conclusions"]:
            story.append(Paragraph("- " + clean(line), st["small"]))
    if not any(rows for _, rows in blocks) and not lim.get("unavailable_scores"):
        story.append(Paragraph("No limitations were recorded for the requested items.", st["body"]))
    story += [
        Spacer(1, 10),
        Paragraph(
            "EquityOS produces research assistance, not investment advice or a promise of returns. "
            "Every figure above is read from validated evidence; none is estimated.",
            st["small"],
        ),
    ]
    footer = f"EquityOS research report - {clean(meta['company'])} - v{version} - {report_id[:8]} - sha {content_hash[:12]}"

    class _Canvas(_Numbered):
        pass

    _Canvas.footer = footer.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    doc.build(story, canvasmaker=_Canvas)
    return buf.getvalue()
