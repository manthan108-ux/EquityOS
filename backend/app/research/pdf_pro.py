"""Institutional-style PDF of a stored research report (Phase 1: structure and presentation).

A pure rendering of the stored report content: nothing is fetched, estimated or invented here. The only
computation is arithmetic on stored values (year-on-year growth), and each such figure is labelled
DERIVED with its formula. Three kinds of statement are kept apart everywhere:

* VERIFIED FACT        - a value read from a document or the market feed, with its evidence id.
* DERIVED              - calculated by EquityOS from verified inputs (a stated formula).
* INTERPRETATION       - a rule-based reading of derived figures, labelled as such.

Sections whose analysis engines do not exist yet (management-commentary engine, valuation models,
investment thesis, peer metric tables) are not faked: the report states plainly that they are not part
of this edition. The previous renderer (`pdf.render`) is kept for the plain evidence register.
"""

from __future__ import annotations

import io
from decimal import Decimal, InvalidOperation

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    CondPageBreak,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from app.research.pdf import CRORE, clean, dmy, fmt_value

NAVY = colors.HexColor("#14213d")
ACCENT = colors.HexColor("#1d4e89")
INK = colors.HexColor("#1f2937")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d9dee5")
BAND = colors.HexColor("#f3f5f8")
WARN_FILL = colors.HexColor("#fff7e6")
WARN_EDGE = colors.HexColor("#c77d0a")
GOOD = colors.HexColor("#2f7d5b")
CHART_A = colors.HexColor("#1d4e89")
CHART_B = colors.HexColor("#8aa8c8")

W = A4[0] - 36 * mm  # usable width

FACT, DERIVED, INTERP = "VERIFIED FACT", "DERIVED", "INTERPRETATION"
TYPE_COLOUR = {FACT: GOOD, DERIVED: ACCENT, INTERP: WARN_EDGE}

# What each sector profile leads with. Only items that exist in the report are shown.
PROFILE_KPIS = {
    "BANK": ("net_interest_income", "net_profit", "nim", "gross_npa", "capital_adequacy", "pb"),
    "IT_SERVICES": ("revenue", "operating_margin", "net_profit", "roe", "pe", "market_cap"),
    "FMCG": ("revenue", "operating_margin", "net_profit", "roce", "pe", "market_cap"),
    "AUTOMOBILES": ("revenue", "operating_margin", "net_profit", "debt_to_equity", "pe", "pb"),
    "ENERGY": ("revenue", "operating_margin", "net_profit", "debt_to_equity", "roe", "pe"),
}
DEFAULT_KPIS = ("revenue", "operating_margin", "net_profit", "debt_to_equity", "roe", "pe")

PROFILE_LENS = {
    "BANK": (
        "A bank is read through net interest income and margin, the cost of its funding (CASA), "
        "asset quality (gross and net NPA, provisions) and capital adequacy, not through "
        "operating margin or industrial cash-flow measures."
    ),
    "IT_SERVICES": (
        "An IT-services company is read through revenue growth, operating margin, return on "
        "equity and cash conversion; operating indicators such as utilisation and attrition are "
        "used only where a source supplies them."
    ),
    "FMCG": (
        "An FMCG company is read through revenue growth, operating margin, return on capital "
        "employed and working capital."
    ),
    "AUTOMOBILES": (
        "An automobile company is read through volume and revenue growth, operating margin, "
        "leverage and capital intensity."
    ),
    "ENERGY": (
        "An energy company is read through revenue, operating margin, leverage and capital "
        "spending; commodity-price sensitivity is not modelled in this edition."
    ),
}

PILLAR_NAMES = {
    "GROWTH": "Growth",
    "RETURNS": "Returns",
    "PROFITABILITY": "Profitability",
    "BALANCE_SHEET": "Balance sheet",
    "FINANCIAL_HEALTH": "Financial health",
    "VALUATION": "Valuation",
    "MOMENTUM": "Price momentum",
    "ASSET_QUALITY": "Asset quality",
    "CAPITAL": "Capital",
}


# ---------------------------------------------------------------- styles


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK)
    bold = {"fontName": "Helvetica-Bold"}
    return {
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=MUTED),
        "h1": ParagraphStyle(
            "h1",
            parent=base,
            fontSize=15,
            leading=19,
            textColor=NAVY,
            spaceBefore=4,
            spaceAfter=6,
            **bold,
        ),  # fmt: skip
        "h2": ParagraphStyle(
            "h2",
            parent=base,
            fontSize=10.5,
            leading=14,
            textColor=ACCENT,
            spaceBefore=8,
            spaceAfter=3,
            **bold,
        ),  # fmt: skip
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.2, leading=10.5),
        "cellb": ParagraphStyle("cellb", parent=base, fontSize=8.2, leading=10.5, **bold),
        "cellr": ParagraphStyle(
            "cellr", parent=base, fontSize=8.2, leading=10.5, alignment=TA_RIGHT
        ),
        "hdr": ParagraphStyle(
            "hdr", parent=base, fontSize=7.6, leading=10, textColor=colors.white, **bold
        ),
        "hdrr": ParagraphStyle(
            "hdrr",
            parent=base,
            fontSize=7.6,
            leading=10,
            textColor=colors.white,
            alignment=TA_RIGHT,
            **bold,
        ),  # fmt: skip
        "cover_name": ParagraphStyle(
            "cn", parent=base, fontSize=30, leading=34, textColor=colors.white, **bold
        ),
        "cover_sub": ParagraphStyle(
            "cs", parent=base, fontSize=11, leading=15, textColor=colors.HexColor("#c9d6e8")
        ),
        "kpi_v": ParagraphStyle(
            "kv", parent=base, fontSize=14, leading=17, textColor=NAVY, alignment=TA_LEFT, **bold
        ),
        "kpi_l": ParagraphStyle("kl", parent=base, fontSize=7.5, leading=9.5, textColor=MUTED),
        "bullet": ParagraphStyle(
            "bullet", parent=base, leftIndent=9, bulletIndent=0, spaceAfter=2.5
        ),
    }


# ---------------------------------------------------------------- data helpers


def _dec(value) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except InvalidOperation:
        return None


def _facts(report: dict) -> dict[str, dict]:
    """First item for each requirement across all sections (the same figure repeats between them)."""
    out: dict[str, dict] = {}
    for sec in report["sections"]:
        for item in sec.get("items", []):
            out.setdefault(item["requirement"], item)
    return out


def _section(report: dict, key: str) -> dict:
    return next((s for s in report["sections"] if s["key"] == key), {})


def _kind(item: dict) -> str:
    """VERIFIED FACT when read from a document or the market feed; DERIVED when EquityOS computed it."""
    provider = (item.get("source") or {}).get("provider") or ""
    return DERIVED if provider == "EVIDENCE_ENGINE" else FACT


def _value(item: dict | None) -> str:
    return "not available" if not item else clean(fmt_value(item.get("value"), item.get("unit")))


def _period(item: dict) -> str:
    bits = [item.get("period_end") or "", (item.get("basis") or "").lower()]
    return clean(" - ".join(b for b in bits if b))


def _cite(item: dict) -> str:
    src = item.get("source") or {}
    where = []
    if src.get("page"):
        where.append(f"p.{src['page']}")
    if src.get("evidence_id"):
        where.append(f"evidence #{src['evidence_id']}")
    text = ", ".join(where) or src.get("provider") or ""
    # a calculated figure says so in its source note; reported figures cite their page
    return clean(("calculated, " + text) if _kind(item) == DERIVED and text else text)


def _growth(series: list[dict]) -> list[tuple[str, Decimal]]:
    """Year-on-year growth of consecutive fiscal years: (this / previous - 1) x 100."""
    out = []
    for prev, cur in zip(series, series[1:], strict=False):
        a, b = _dec(prev["value"]), _dec(cur["value"])
        if a and b is not None and a > 0:
            out.append((cur["fiscal_year"], (b / a - 1) * 100))
    return out


def _pct(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.1'))}%"


def _chip(kind: str, st) -> Paragraph:
    colour = TYPE_COLOUR[kind].hexval()[2:]
    return Paragraph(f'<font color="#{colour}" size="6.3"><b>{kind}</b></font>', st["cell"])


def _thousand_crore(value) -> float:
    return float((_dec(value) or Decimal(0)) / CRORE / 1000)


# ---------------------------------------------------------------- building blocks


def _table(rows, widths, *, header=True, zebra=True) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
    ]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), NAVY))
    if zebra:
        style.append(("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, BAND]))
    t.setStyle(TableStyle(style))
    return t


def _callout(text: str, st, *, warn=True) -> Table:
    t = Table([[Paragraph(clean(text), st["body"])]], colWidths=[W])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), WARN_FILL if warn else BAND),
                ("LINEBEFORE", (0, 0), (0, -1), 2.2, WARN_EDGE if warn else ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return t


def _kpi_strip(items: list[tuple[str, dict]], st) -> Table | None:
    if not items:
        return None
    cells = [
        [
            Paragraph(_value(item), st["kpi_v"]),
            Paragraph(clean(label), st["kpi_l"]),
            Paragraph(clean(item.get("period_end") or ""), st["kpi_l"]),
        ]
        for label, item in items
    ]
    cols = 3
    rows = [cells[i : i + cols] for i in range(0, len(cells), cols)]
    for r in rows:
        r.extend([""] * (cols - len(r)))
    t = Table(rows, colWidths=[W / cols] * cols)
    t.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, RULE),
                ("BACKGROUND", (0, 0), (-1, -1), BAND),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )
    return t


def _bar_chart(title: str, unit: str, cats: list[str], series: list[tuple[str, list[float]]]):
    """Grouped vertical bars. Values are in the unit named under the title."""
    h = 150
    d = Drawing(W, h + 24)
    d.add(String(0, h + 12, title, fontName="Helvetica-Bold", fontSize=8.5, fillColor=INK))
    d.add(String(0, h + 2, unit, fontName="Helvetica", fontSize=7, fillColor=MUTED))
    ch = VerticalBarChart()
    ch.x, ch.y, ch.width, ch.height = 38, 22, W - 150, h - 22
    ch.data = [s[1] for s in series]
    ch.categoryAxis.categoryNames = cats
    ch.categoryAxis.labels.fontSize = 7.5
    ch.valueAxis.labels.fontSize = 7
    ch.valueAxis.labelTextFormat = "%.0f"
    ch.valueAxis.strokeColor = RULE
    ch.valueAxis.gridStrokeColor = RULE
    ch.valueAxis.visibleGrid = True
    ch.valueAxis.valueMin = 0
    ch.barWidth = 11
    ch.groupSpacing = 10
    ch.bars.strokeColor = None
    for i, colour in enumerate((CHART_A, CHART_B)[: len(series)]):
        ch.bars[i].fillColor = colour
    ch.barLabelFormat = "%.0f"
    ch.barLabels.fontSize = 6.5
    ch.barLabels.nudge = 6
    d.add(ch)
    for i, (name, _) in enumerate(series):
        y = h - 10 - i * 12
        d.add(Rect(W - 118, y - 1, 8, 8, fillColor=(CHART_A, CHART_B)[i], strokeColor=None))
        d.add(String(W - 105, y, name, fontName="Helvetica", fontSize=7.5, fillColor=INK))
    return d


def _pillar_chart(pillars: dict[str, str]):
    names = [PILLAR_NAMES.get(k, k.replace("_", " ").title()) for k in pillars]
    vals = [float(v) for v in pillars.values()]
    h = 16 * len(vals) + 46
    d = Drawing(W, h)
    d.add(
        String(
            0,
            h - 10,
            "Pillar scores (0-100, relative to peers)",
            fontName="Helvetica-Bold",
            fontSize=8.5,
            fillColor=INK,
        )  # fmt: skip
    )
    ch = HorizontalBarChart()
    ch.x, ch.y, ch.width, ch.height = 90, 18, W - 140, h - 40
    ch.data = [vals[::-1]]
    ch.categoryAxis.categoryNames = names[::-1]
    ch.categoryAxis.labels.fontSize = 7.5
    ch.valueAxis.valueMin, ch.valueAxis.valueMax, ch.valueAxis.valueStep = 0, 100, 25
    ch.valueAxis.labels.fontSize = 7
    ch.valueAxis.gridStrokeColor = RULE
    ch.valueAxis.visibleGrid = True
    ch.bars[0].fillColor = CHART_A
    ch.bars.strokeColor = None
    ch.barWidth = 9
    ch.barLabelFormat = "%.0f"
    ch.barLabels.fontSize = 7
    ch.barLabels.nudge = 8
    d.add(ch)
    return d


# ---------------------------------------------------------------- page furniture


class _Pages(canvas.Canvas):
    """Header, footer and 'page X of Y' on every page but the cover."""

    company = ""
    as_of = ""
    footer = ""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._saved: list[dict] = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            n = self._pageNumber
            if n > 1:
                self.setFont("Helvetica-Bold", 8)
                self.setFillColor(NAVY)
                self.drawString(18 * mm, A4[1] - 11 * mm, self.company)
                self.setFont("Helvetica", 8)
                self.setFillColor(MUTED)
                self.drawRightString(
                    A4[0] - 18 * mm, A4[1] - 11 * mm, f"Equity research | {self.as_of}"
                )
                self.setStrokeColor(RULE)
                self.line(18 * mm, A4[1] - 13.5 * mm, A4[0] - 18 * mm, A4[1] - 13.5 * mm)
                self.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
                self.setFont("Helvetica", 7)
                self.drawString(18 * mm, 10 * mm, self.footer[:140])
                self.drawRightString(A4[0] - 18 * mm, 10 * mm, f"page {n} of {total}")
            super().showPage()
        super().save()


# ---------------------------------------------------------------- sections


def _cover(report, facts, st, version, mode) -> list:
    meta, ready = report["meta"], report["readiness"]
    ov = _section(report, "overview").get("company", {})
    score = _section(report, "scorecard").get("score", {})
    prefs = meta.get("preferences") or {}
    symbols = sorted({s for s in ov.get("symbols", []) if s})
    complete = mode == "COMPLETE" and meta.get("complete")
    sector = " / ".join(
        x.replace("_", " ").title() for x in (ov.get("sector"), ov.get("industry")) if x
    )
    band = Table(
        [
            [Paragraph("EQUITY RESEARCH", st["cover_sub"])],
            [Paragraph(clean(meta["company"]), st["cover_name"])],
            [
                Paragraph(
                    clean(f"NSE/BSE: {', '.join(symbols) or 'n/a'}   |   {sector}"), st["cover_sub"]
                )
            ],
        ],
        colWidths=[W],
    )
    band.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("LEFTPADDING", (0, 0), (-1, -1), 16),
                ("TOPPADDING", (0, 0), (0, 0), 38),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 34),
            ]
        )
    )
    price, mcap = facts.get("price"), facts.get("market_cap")
    price_text = (
        f"{_value(price)} ({clean(price.get('period_end') or '')})" if price else "not available"
    )
    rows = [
        ["Report date", clean(meta["as_of"])],
        ["Latest reporting period", clean(prefs.get("period_label", "latest available quarter"))],
        ["Accounting basis", clean(str(prefs.get("basis", "CONSOLIDATED")).title())],
        ["Share price", price_text],
        ["Market capitalisation", _value(mcap)],
        [
            "Research coverage",
            "Complete (every critical requirement met)"
            if complete
            else "Available-evidence report",
        ],
    ]
    info = _table(
        [[Paragraph(a, st["cellb"]), Paragraph(b, st["cell"])] for a, b in rows],
        [48 * mm, W - 48 * mm],
        header=False,
        zebra=False,
    )
    s_val = (
        f"{Decimal(score['overall']).quantize(Decimal('0.1'))}" if score.get("overall") else "n/a"
    )
    meth = (score.get("methodology") or "").title()
    two = Table(
        [
            [
                [
                    Paragraph(f"{s_val}<font size=9> / 100</font>", st["kpi_v"]),
                    Paragraph("FINANCIAL SCORE", st["kpi_l"]),
                    Paragraph(
                        clean(
                            f"{meth} peer scoring, confidence {(score.get('confidence') or 'n/a').lower()}; "
                            "measures the company's financial standing against peers."
                        ),
                        st["small"],
                    ),
                ],
                [
                    Paragraph(f"{clean(ready['score'])}<font size=9> / 100</font>", st["kpi_v"]),
                    Paragraph("RESEARCH READINESS", st["kpi_l"]),
                    Paragraph(
                        clean(
                            f"{ready['eligibility'].title()} research, evidence confidence "
                            f"{ready['confidence'].lower()}; measures the evidence, not the company."
                        ),
                        st["small"],
                    ),
                ],
                *_verdict_cell(report, st),
            ]
        ],
        colWidths=[W / (3 if _has_verdict(report) else 2)] * (3 if _has_verdict(report) else 2),
    )
    two.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                ("LINEAFTER", (0, 0), (-2, 0), 0.4, RULE),
                ("BACKGROUND", (0, 0), (-1, -1), BAND),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return [
        band,
        Spacer(1, 14),
        info,
        Spacer(1, 12),
        two,
        Spacer(1, 12),
        Paragraph(
            "The financial score and research readiness are separate measures. "
            + (
                "The verdict is the output of a stated rule set (methodology v1), not a forecast or "
                "investment advice; see section 7."
                if _has_verdict(report)
                else "The report contains no buy, sell or hold recommendation in this edition."
            ),
            st["small"],
        ),
        Spacer(1, 6),
        Paragraph(
            f"Report version v{version}. Figures are read from validated evidence; none is estimated.",
            st["small"],
        ),
        PageBreak(),
    ]


def _has_verdict(report: dict) -> bool:
    a = report.get("analysis") or {}
    return bool(a.get("available") and a.get("verdict"))


VERDICT_COLOUR = {"BUY": "#2f7d5b", "SELL": "#b3442f", "WATCH": "#c77d0a"}


def _verdict_cell(report: dict, st) -> list:
    if not _has_verdict(report):
        return []
    v = report["analysis"]["verdict"]
    colour = VERDICT_COLOUR[v["verdict"]]
    return [
        [
            Paragraph(f'<font color="{colour}"><b>{v["verdict"]}</b></font>', st["kpi_v"]),
            Paragraph("EQUITYOS VERDICT", st["kpi_l"]),
            Paragraph(
                clean(
                    f"Evidence confidence {v['confidence']['band']} ({v['confidence']['points']} / 100). "
                    "Rule-based; see section 7."
                ),
                st["small"],
            ),
        ]
    ]


def _rev_label(report: dict) -> str:
    return "Total income" if report["meta"].get("profile") == "BANK" else "Revenue"


def _observations(facts, series, rev_label="Total income") -> list[tuple[str, str]]:
    """Rule-based statements: (kind, text). Each is built only from stored values."""
    obs: list[tuple[str, str]] = []
    for label, key in ((rev_label, "revenue"), ("Net profit", "net_profit")):
        g = _growth(series.get(key, []))
        if g:
            last = g[-1]
            tail = f" (previous year {_pct(g[-2][1])})" if len(g) > 1 else ""
            verb = "grew" if last[1] >= 0 else "fell"
            obs.append((DERIVED, f"{label} {verb} {_pct(abs(last[1]))} in {last[0]}{tail}."))
    rev, prof = series.get("revenue", []), series.get("net_profit", [])
    if rev and prof and rev[-1]["fiscal_year"] == prof[-1]["fiscal_year"]:
        a, b = _dec(rev[-1]["value"]), _dec(prof[-1]["value"])
        if a and b is not None and a > 0:
            share = _pct(b / a * 100)
            obs.append(
                (
                    DERIVED,
                    f"Net profit was {share} of {rev_label.lower()} in {rev[-1]['fiscal_year']}.",
                )
            )
    q = facts.get("net_profit")
    if q and q.get("period_end"):
        obs.append((FACT, f"Net profit for the period ended {q['period_end']} was {_value(q)}."))
    for key, text in (
        ("nim", "Net interest margin"),
        ("gross_npa", "Gross NPA ratio"),
        ("net_npa", "Net NPA ratio"),
        ("capital_adequacy", "Capital adequacy ratio"),
    ):
        it = facts.get(key)
        if it:
            obs.append((FACT, f"{text} was {_value(it)} ({it.get('period_end')})."))
    return obs


def _tag(kind: str) -> str:
    """Inline evidence-status labels were removed; the classification lives in the sources appendix."""
    return ""


def _executive_synthesis(report, st) -> list:
    a = report["analysis"]
    th = a["thesis"]
    peers = a["peers"]["summary"]
    parts = [
        f"Financial performance on the latest annual evidence is {th['trajectory']['label']}.",
        f"{len(th['bull'])} supportive and {len(th['bear'])} adverse evidence-based points were found.",
    ]
    if peers["ranked_metrics"]:
        parts.append(
            f"Against peers, the company is better than the median on {len(peers['better'])} and "
            f"worse on {len(peers['worse'])} of {peers['ranked_metrics']} rankable metrics."
        )
    else:
        parts.append("Peer standing cannot be established: too few comparable peers.")
    v = a.get("verdict")
    if v:
        lv = v["levels"]
        text = f"EquityOS verdict: {v['verdict']} (evidence confidence {v['confidence']['band']})."
        if lv.get("target"):
            text += f" Target Rs. {lv['target']}, stop-loss Rs. {lv['stop_loss']}, reward-to-risk {lv['reward_to_risk']}."
        parts.append(text)
    return [Paragraph(clean(" ".join(parts)) + " " + _tag("INTERPRETATION"), st["body"])]


def _executive(report, facts, series, st, full=False) -> list:
    profile = report["meta"].get("profile")
    keys = PROFILE_KPIS.get(profile, DEFAULT_KPIS)
    kpis = [(facts[k]["label"], facts[k]) for k in keys if k in facts]
    out: list = [Paragraph("1. Executive summary", st["h1"])]
    strip = _kpi_strip(kpis, st)
    if strip:
        out += [strip, Spacer(1, 8)]
    out.append(Paragraph("Key observations", st["h2"]))
    obs = _observations(facts, series, _rev_label(report))
    if obs:
        for _, text in obs:
            out.append(Paragraph(clean(text), st["bullet"], bulletText="-"))
    else:
        out.append(Paragraph("No observation can be made from the stored evidence.", st["body"]))
    out.append(Paragraph("Valuation snapshot", st["h2"]))
    vals = [facts[k] for k in ("price", "market_cap", "pe", "pb") if k in facts]
    if vals:
        text = "; ".join(f"{clean(i['label'])} {_value(i)}" for i in vals)
        out.append(
            Paragraph(
                text
                + (
                    ". Historical, peer and intrinsic valuation are in section 6."
                    if full
                    else ". Intrinsic and historical valuation are not part of this edition."
                ),
                st["body"],
            )
        )
    else:
        out.append(Paragraph("No valuation inputs are available.", st["body"]))
    out.append(Paragraph("Overall research position", st["h2"]))
    if full:
        out += _executive_synthesis(report, st)
    score = _section(report, "scorecard").get("score", {})
    if score.get("available"):
        out.append(
            Paragraph(
                clean(
                    f"EquityOS's {str(score.get('methodology')).lower()} financial score is "
                    f"{Decimal(score['overall']).quantize(Decimal('0.1'))} / 100 against "
                    f"{score.get('eligible_peer_count')} peers (confidence "
                    f"{str(score.get('confidence')).lower()}). "
                    + (
                        "See section 8 for what drives it and sections 7 and 9 for the thesis and "
                        "conclusion."
                        if full
                        else "See section 6 for what drives it. A narrative conclusion and an "
                        "investment thesis are not part of this edition."
                    )
                ),
                st["body"],
            )
        )
    else:
        out.append(
            Paragraph(clean(score.get("reason") or "No financial score is available."), st["body"])
        )
    return out


def _overview(report, st, title="2. Company overview", segments=True, full=False) -> list:
    ov = _section(report, "overview").get("company", {})
    profile = report["meta"].get("profile")
    legal = ov.get("legal_name") or report["meta"]["company"]
    sector = (ov.get("sector") or "").replace("_", " ")
    industry = (ov.get("industry") or "").replace("_", " ")
    out = [
        Paragraph(title, st["h1"]),
        Paragraph(
            clean(
                f"{legal} is classified in EquityOS as {sector} / {industry} and is analysed with "
                f"the {str(ov.get('profile', '')).lower()} research profile."
            ),
            st["body"],
        ),
        Paragraph("How this company is read", st["h2"]),
        Paragraph(clean(PROFILE_LENS.get(profile, "Generic non-financial lens.")), st["body"]),
    ]
    seg = _section(report, "segments")
    if segments and seg.get("segments"):
        out.append(Paragraph("Business segments", st["h2"]))
        rows = [[Paragraph(h, st["hdr"]) for h in ("Segment", "Metric", "Period", "Value")]]
        for s in seg["segments"]:
            for m in s.get("metrics", []):
                rows.append(
                    [
                        Paragraph(clean(s.get("name")), st["cell"]),
                        Paragraph(clean(m.get("label")), st["cell"]),
                        Paragraph(clean(m.get("period_end")), st["cell"]),
                        Paragraph(clean(fmt_value(m.get("value"), m.get("unit"))), st["cellr"]),
                    ]
                )
        out.append(_table(rows, [60 * mm, 50 * mm, 30 * mm, W - 140 * mm]))
    if not full:
        out.append(
            Paragraph(
                "Business model, products, competitive advantages and (for banks) the lending and "
                "deposit mix need the business-analysis engine, which is not part of this edition.",
                st["small"],
            )
        )
    return out


def _fact_table(title: str, sec: dict, st) -> list:
    items = sec.get("items", [])
    if not items:
        return []
    heads = ("Metric", "Value", "Period / basis", "Source")
    rows = [[Paragraph(h, st["hdrr"] if h == "Value" else st["hdr"]) for h in heads]]
    for it in items:
        rows.append(
            [
                Paragraph(clean(it["label"]), st["cell"]),
                Paragraph(_value(it), st["cellr"]),
                Paragraph(_period(it), st["cell"]),
                Paragraph(_cite(it), st["small"]),
            ]
        )
    block = [
        Paragraph(title, st["h2"]),
        _table(rows, [50 * mm, 42 * mm, 40 * mm, W - 132 * mm]),
    ]
    for g in sec.get("gaps", []):
        block.append(
            Paragraph(
                f"<b>Not available: {clean(g['label'])}.</b> {clean(g.get('reason') or '')}",
                st["small"],
            )
        )
    return [KeepTogether(block)] if len(rows) <= 12 else block


def _trend_section(report, series, st) -> list:
    lab = _rev_label(report)
    rev, prof = series.get("revenue", []), series.get("net_profit", [])
    ta, eq = series.get("total_assets", []), series.get("total_equity", [])
    base = rev or prof or ta
    out: list = [CondPageBreak(95 * mm), Paragraph("Annual trends", st["h2"])]
    if not base:
        return out + [Paragraph("No annual history is stored for this company.", st["body"])]
    years = [x["fiscal_year"] for x in base]
    basis = str(report["meta"].get("preferences", {}).get("basis", "")).lower()
    by = {name: {x["fiscal_year"]: x for x in s} for name, s in
          (("rev", rev), ("prof", prof), ("ta", ta), ("eq", eq))}  # fmt: skip

    def col(name: str) -> list[float]:
        return [_thousand_crore(by[name][y]["value"]) if y in by[name] else 0.0 for y in years]

    if len(years) >= 2 and (rev or prof):
        out.append(
            _bar_chart(
                f"{lab} and net profit",
                f"Rs. thousand crore, {basis}, fiscal years",
                years,
                [(lab, col("rev")), ("Net profit", col("prof"))],
            )
        )
    if len(years) >= 2 and ta and eq:
        out += [
            Spacer(1, 6),
            _bar_chart(
                "Total assets and shareholders' equity",
                f"Rs. thousand crore, {basis}, fiscal years",
                years,
                [("Total assets", col("ta")), ("Equity", col("eq"))],
            ),
        ]
    rows = [
        [Paragraph("Annual series (Rs. crore)", st["hdr"])]
        + [Paragraph(y, st["hdrr"]) for y in years]
    ]
    for name, key in (
        (lab, "rev"),
        ("Net profit", "prof"),
        ("Total assets", "ta"),
        ("Equity", "eq"),
    ):
        if by[key]:
            cells = []
            for y in years:
                v = _dec(by[key][y]["value"]) if y in by[key] else None
                cells.append(Paragraph("n/a" if v is None else f"{v / CRORE:,.0f}", st["cellr"]))
            rows.append([Paragraph(name, st["cell"]), *cells])
    for name, s in ((f"{lab} growth", rev), ("Net profit growth", prof)):
        g = dict(_growth(s))
        if g:
            rows.append(
                [Paragraph(clean(name), st["cell"])]
                + [Paragraph(_pct(g[y]) if y in g else "-", st["cellr"]) for y in years]
            )
    n = len(years)
    out += [Spacer(1, 6), KeepTogether([_table(rows, [58 * mm] + [(W - 58 * mm) / n] * n)])]
    out.append(
        Paragraph(
            "Growth = (this year / previous year - 1) x 100, from the figures above. The cause of "
            "each movement is not established from the evidence held in this edition.",
            st["small"],
        )
    )
    return out


def _financials(report, series, st, extra=(), title="3. Financial performance") -> list:
    bank = report["meta"].get("profile") == "BANK"
    out: list = [Paragraph(title, st["h1"])]
    out += _fact_table("Latest results", _section(report, "financial_performance"), st)
    out += _fact_table("Profitability", _section(report, "profitability"), st)
    out += _fact_table(
        "Balance sheet and asset quality" if bank else "Balance sheet",
        _section(report, "balance_sheet"),
        st,
    )
    out += list(extra)
    out += _trend_section(report, series, st)
    if bank and not extra:
        out += [
            Paragraph("Cash flow", st["h2"]),
            Paragraph(
                "For a bank, operating cash flow is dominated by changes in deposits and advances, "
                "so industrial-company measures such as free cash flow and cash conversion are not "
                "meaningful and are not reported.",
                st["body"],
            ),
        ]
    return out


def _peers(report, st) -> list:
    sec = _section(report, "peer_comparison").get("peers", {})
    out = [Paragraph("4. Peer positioning", st["h1"])]
    peers = sec.get("peers") or []
    if not peers:
        return out + [Paragraph("No peer group is available for this company.", st["body"])]
    crit = sec.get("criteria", {})
    out.append(
        Paragraph(
            clean(
                f"Peers are same-industry companies closest in size, compared on the "
                f"{str(crit.get('basis', '')).lower()} basis ({crit.get('order', '')})."
            ),
            st["body"],
        )
    )
    rows = [[Paragraph(h, st["hdr"]) for h in ("#", "Peer", "Selection basis")]]
    for p in peers:
        rows.append(
            [
                Paragraph(clean(p["rank"]), st["cell"]),
                Paragraph(clean(p["name"]), st["cellb"]),
                Paragraph(clean(p["reason"]), st["cell"]),
            ]
        )
    out += [Spacer(1, 4), KeepTogether([_table(rows, [10 * mm, 50 * mm, W - 60 * mm])])]
    if sec.get("thin"):
        out.append(Paragraph("The peer group is thin; rankings are coarse.", st["small"]))
    out.append(
        Paragraph(
            "Side-by-side peer metrics (growth, NIM, ROA, ROE, asset quality, capital, valuation) "
            "with rankings are not part of this edition: they need a standardised peer-metric table "
            "that checks period, basis and definition for every peer. No peer comparison is implied "
            "beyond the relative score in section 6.",
            st["small"],
        )
    )
    return out


def _valuation(report, facts, st, title="5. Valuation snapshot", note=True) -> list:
    sec = _section(report, "valuation")
    out = [Paragraph(title, st["h1"])]
    heads = ("Measure", "Value", "As of", "Quality / source")
    rows = [[Paragraph(h, st["hdrr"] if h == "Value" else st["hdr"]) for h in heads]]
    for k in ("price", "market_cap", "pe", "pb"):
        it = facts.get(k)
        if it:
            quality = (it["source"].get("quality") or "").title()
            rows.append(
                [
                    Paragraph(clean(it["label"]), st["cell"]),
                    Paragraph(_value(it), st["cellr"]),
                    Paragraph(clean(it.get("period_end")), st["cell"]),
                    Paragraph(clean(f"{quality} - {_cite(it)}"), st["small"]),
                ]
            )
    if len(rows) > 1:
        out.append(KeepTogether([_table(rows, [44 * mm, 38 * mm, 28 * mm, W - 110 * mm])]))
    for g in sec.get("gaps", []):
        out.append(
            Paragraph(
                f"<b>Not available: {clean(g['label'])}.</b> {clean(g.get('reason') or '')}",
                st["small"],
            )
        )
    if note:
        out.append(
            Paragraph(
                "Historical multiples, peer premium or discount, residual-income / DCF models and "
                "bear-base-bull scenarios are not part of this edition. No target price is given.",
                st["small"],
            )
        )
    return out


def _scorecard(report, st, title="6. Financial scorecard") -> list:
    score = _section(report, "scorecard").get("score", {})
    ready = report["readiness"]
    out = [Paragraph(title, st["h1"])]
    if not score.get("available"):
        out.append(Paragraph(clean(score.get("reason") or "No score is available."), st["body"]))
    else:
        pillars = score.get("pillar_scores") or {}
        out.append(
            Paragraph(
                clean(
                    f"Methodology: {str(score['methodology']).title()} (position against "
                    f"{score.get('eligible_peer_count')} eligible same-industry peers). Score "
                    f"{Decimal(score['overall']).quantize(Decimal('0.1'))} / 100, confidence "
                    f"{str(score['confidence']).lower()}. An absolute or hybrid score is not "
                    "stored for this company in the current scoring run."
                ),
                st["body"],
            )
        )
        if pillars:
            out += [Spacer(1, 4), _pillar_chart(pillars)]
            strong = [PILLAR_NAMES.get(k, k) for k, v in pillars.items() if Decimal(v) >= 60]
            weak = [PILLAR_NAMES.get(k, k) for k, v in pillars.items() if Decimal(v) <= 40]
            out.append(
                Paragraph(
                    clean(
                        f"Pillars at or above 60: {', '.join(strong) or 'none'}. Pillars at or "
                        f"below 40: {', '.join(weak) or 'none'}. "
                    )
                    + " The 60 and 40 cut-offs are presentation labels, not scoring rules.",
                    st["body"],
                )
            )
    meaning = [Paragraph("What the two numbers mean", st["h2"])]
    meaning.append(
        Paragraph(
            clean(
                "The financial score ranks the company's metrics against its peers. Research "
                f"readiness ({ready['score']} / 100, {ready['eligibility'].lower()}) says how "
                "complete and reliable the evidence is. A high readiness does not mean the "
                "company is attractive."
            ),
            st["body"],
        )
    )
    comps = ready.get("components") or {}
    if comps:
        rows = [[Paragraph(h, st["hdr"]) for h in ("Readiness component", "Score", "Basis")]]
        for c in comps.values():
            rows.append(
                [
                    Paragraph(clean(c["label"]), st["cell"]),
                    Paragraph("n/a" if c["score"] is None else clean(c["score"]), st["cell"]),
                    Paragraph(clean(c["detail"]), st["cell"]),
                ]
            )
        meaning.append(_table(rows, [52 * mm, 16 * mm, W - 68 * mm]))
    out.append(KeepTogether(meaning))
    return out


def _management(report, st) -> list:
    n = _section(report, "risks_management").get("management_discussion")
    out = [Paragraph("7. Management commentary", st["h1"])]
    if n and n.get("source"):
        s = n["source"]
        out.append(
            Paragraph(
                clean(
                    f"A management-discussion section is stored from the annual report (page "
                    f"{s.get('page')}, evidence #{s.get('evidence_id')}). Structured commentary "
                    "(priorities, outlook, risks, guidance) is not extracted in this edition, and "
                    "the raw text is deliberately not reproduced."
                ),
                st["body"],
            )
        )
    else:
        out.append(
            Paragraph("No management-discussion text is stored for this company.", st["body"])
        )
    return out


def _appendix(report, st) -> list:
    out: list = [PageBreak(), Paragraph("Appendix A - Source register", st["h1"])]
    heads = ("Metric", "Value", "Period / basis", "Type", "Ev. id", "Document", "Quality")
    rows = [[Paragraph(h, st["hdr"]) for h in heads]]
    seen = set()
    for sec in report["sections"]:
        for it in sec.get("items", []):
            src = it.get("source") or {}
            key = (it["requirement"], src.get("evidence_id"))
            if key in seen:
                continue
            seen.add(key)
            doc = (src.get("document_id") or "")[:8]
            page = f" p.{src['page']}" if src.get("page") else ""
            rows.append(
                [
                    Paragraph(clean(it["label"]), st["small"]),
                    Paragraph(_value(it), st["small"]),
                    Paragraph(_period(it), st["small"]),
                    Paragraph(_kind(it).title(), st["small"]),
                    Paragraph(f"#{src.get('evidence_id')}", st["small"]),
                    Paragraph(clean((doc + page) or (src.get("provider") or "")), st["small"]),
                    Paragraph(clean((src.get("quality") or "").title()), st["small"]),
                ]
            )
    out.append(_table(rows, [38 * mm, 29 * mm, 33 * mm, 18 * mm, 16 * mm, 24 * mm, W - 158 * mm]))
    out.append(
        Paragraph(
            "Document names and filing dates are not yet carried in report content; the document "
            "column shows the first eight characters of its id.",
            st["small"],
        )
    )
    lim = report["limitations"]
    out.append(Paragraph("Appendix B - Limitations and unavailable information", st["h1"]))
    for title, key in (
        ("Critical items not available", "missing_critical"),
        ("Important items not available", "missing_important"),
        ("Optional items not available", "missing_optional"),
        ("Calculations that could not be made", "unavailable_calculations"),
    ):
        rows_ = lim.get(key) or []
        if rows_:
            text = "; ".join(
                r["label"] + (f" ({r['reason']})" if r.get("reason") else "") for r in rows_
            )
            out += [Paragraph(f"<b>{title}</b>", st["body"]), Paragraph(clean(text), st["small"])]
    if lim.get("applied_price_adjustments"):
        out.append(Paragraph("<b>Price adjustments applied</b>", st["body"]))
        for a in lim["applied_price_adjustments"]:
            line = f"{a['symbol']} {a['ex_date']}: {a.get('ratio') or ''} (factor {a['factor']})"
            out.append(Paragraph("- " + clean(line), st["small"]))
    if lim.get("price_history_notes"):
        out.append(Paragraph("<b>Price history</b>", st["body"]))
        for n in lim["price_history_notes"]:
            out.append(Paragraph("- " + clean(f"{n['date']}: {n['note']}"), st["small"]))
    for line in lim.get("limits_on_conclusions") or []:
        out.append(Paragraph("- " + clean(line), st["small"]))
    out += [
        Paragraph("Appendix C - Methodology", st["h1"]),
        Paragraph(
            "Reported figures are read from a company document or the market feed and cite their page "
            "and evidence number. Calculated figures are computed by EquityOS from stored figures "
            "(year-on-year growth = this year / previous year - 1; ratios and multiples by the "
            "calculation engine, each recorded as evidence) and say so in their source note. "
            "Observations, thesis points and the verdict are rule-based readings of stored figures "
            "under the rules printed in this report. Dates are shown as DD/MM/YYYY. Amounts are "
            "shown in Rs. crore with Indian digit grouping. Statement figures are used on one "
            "accounting basis only; the other basis is never substituted. Research readiness and "
            "the financial score are independent measures.",
            st["small"],
        ),
        Spacer(1, 8),
        Paragraph(
            "EquityOS produces research assistance, not investment advice or a promise of returns.",
            st["small"],
        ),
    ]
    return out


def _coverage_full(st) -> list:
    return [
        Spacer(1, 6),
        _callout(
            "How to read this report: figures cite their source page or evidence number; calculated "
            "figures say so in their source note. The thesis and conclusion are rule-based readings "
            "of the evidence, with the rules stated in the appendix, and hypotheses are written as "
            "hypotheses. Industry and sector-risk text is general sector context, not evidence about "
            "the company. Valuation scenarios rest on illustrative assumptions and are not target "
            "prices. The BUY / SELL / WATCH verdict follows the stated rule set (methodology v1); it is not a forecast or investment advice.",
            st,
            warn=False,
        ),
    ]


def _coverage(st) -> list:
    return [
        Spacer(1, 6),
        _callout(
            "Scope of this edition: structure, evidence-backed figures, annual trends, peer "
            "selection, valuation inputs and the scorecard. Not yet produced: structured management "
            "commentary, peer metric tables and rankings, historical and intrinsic valuation with "
            "scenarios, investment thesis, and a narrative conclusion. They are omitted rather "
            "than approximated.",
            st,
            warn=False,
        ),
    ]


# ---------------------------------------------------------------- entry point


def _full_story(report, facts, hist, st, version, mode) -> list:
    from app.research import pdf_analysis as pa
    from app.research import pdf_sections as ps

    a = report["analysis"]
    name = report["meta"]["company"]
    n = iter(range(1, 40))

    def h1(title: str) -> Paragraph:
        return Paragraph(f"{next(n)}. {title}", st["h1"])

    def head(title: str) -> str:
        return f"{next(n)}. {title}"

    story: list = []
    story += _cover(report, facts, st, version, mode)
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("toc1", fontName="Helvetica", fontSize=10, leading=17, textColor=INK)
    ]
    story += [Paragraph("Contents", st["h1"]), Spacer(1, 6), toc, PageBreak()]
    # the executive summary carries its own heading (number 1)
    next(n)
    story += _executive(report, facts, hist, st, full=True) + _coverage_full(st) + [PageBreak()]
    story += [h1("Industry overview")] + ps.industry_section(report, st) + [PageBreak()]
    story += _overview(
        report, st, title=head("Company and management profile"), segments=False, full=True
    )
    # a short profile does not leave a mostly empty page: the next section continues if there is room
    story += pa.bank_business(report, st) + ps.profile_section(report, st)
    story += [CondPageBreak(120 * mm)]
    story += _financials(
        report,
        hist,
        st,
        extra=pa.financial_analysis(report, st),
        title=head("Financial performance"),
    )
    # A section with nothing meaningful to say is left out entirely: no heading, no contents entry,
    # no empty page; the numbers that follow close up. Partial sections are kept.
    if ps.growth_has_content(a.get("growth")):
        story += [PageBreak(), h1("Growth versus the industry: CAGR and growth alpha")]
        story += ps.growth_section(report, st)
    if ps.cyclicality_has_content(a.get("cyclicality")):
        story += [PageBreak(), h1("Business cyclicality")] + ps.cyclicality_section(report, st)
    peers = a["peers"]["peers"]
    if peers:
        story += _peer_pages(report, st, name, peers, h1)
    if a["management"].get("available"):
        story += [PageBreak(), h1("Management commentary")] + pa.management_section(report, st)
    story += [PageBreak()]
    story += _valuation(report, facts, st, title=head("Valuation analysis"), note=False)
    story += pa.valuation_section(report, st)
    story += [PageBreak(), h1("Investment thesis and verdict")] + pa.thesis_section(report, st)
    story += [PageBreak()] + _scorecard(report, st, title=head("Financial scorecard"))
    story += [CondPageBreak(80 * mm), h1("Research conclusion")] + pa.conclusion_section(report, st)
    story += _appendix(report, st) + pa.analysis_register(report, st) + _method_full(report, st)
    story += ps.data_quality_section(report, st)
    return story


def _peer_pages(report, st, name, peers, h1) -> list:
    from app.research import pdf_analysis as pa

    intro = [PageBreak(), h1("Peer benchmarking and competitive positioning")]
    if peers:
        rows = [[Paragraph(h, st["hdr"]) for h in ("#", "Peer", "Selection basis")]]
        for p in peers:
            rows.append(
                [
                    Paragraph(clean(p["rank"]), st["cell"]),
                    Paragraph(clean(p["name"]), st["cellb"]),
                    Paragraph(clean(p["reason"]), st["cell"]),
                ]
            )
        intro += [
            Paragraph(
                "Peers are same-industry companies closest in size (selection rules in the appendix).",
                st["body"],
            ),
            Spacer(1, 4),
            KeepTogether([_table(rows, [10 * mm, 50 * mm, W - 60 * mm])]),
        ]
    return intro + pa.peer_section(report, st, name)


def _method_full(report, st) -> list:
    a = report["analysis"]
    th, val, peers = a["thesis"], a["valuation"], a["peers"]
    lines = [
        ("Trend and change rules", a["trend"]["rules"]),
        ("Peer comparison rules", peers["rules"]),
        ("Thesis rules", th["rules"]),
        ("Valuation assumptions", val["assumption_note"]),
        (
            "Commentary method",
            a["management"].get("method", "no management-discussion section is stored"),
        ),
    ]
    out = [Paragraph("Appendix D - Analytical methodology", st["h1"])]
    for k, v in lines:
        out.append(Paragraph(f"<b>{clean(k)}.</b> {clean(v)}", st["small"]))
    out.append(
        Paragraph(
            clean(
                f"Analysis version {a['version']}; sector profile {a['sector']['label']}; accounting "
                f"basis preference {a['basis'].lower()}."
            ),
            st["small"],
        )
    )
    return out


class _ContentsDoc(SimpleDocTemplate):
    """Feeds every level-1 heading to the contents page (second build pass fills in page numbers)."""

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "h1":
            text = flowable.getPlainText()
            if text != "Contents":
                self.notify("TOCEntry", (0, text, self.page))


def render(report: dict, *, version: int, mode: str, report_id: str, content_hash: str) -> bytes:
    """The professional PDF of a stored report's content (same signature as `pdf.render`)."""
    st = _styles()
    meta = report["meta"]
    facts = _facts(report)
    hist = _section(report, "historical").get("series", {})
    buf = io.BytesIO()
    doc = _ContentsDoc(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
        title=f"{meta['company']} - equity research report v{version}", author="EquityOS", invariant=1,
    )  # fmt: skip
    story: list = []
    analysis = report.get("analysis")
    if analysis and analysis.get("available"):
        story = _full_story(report, facts, hist, st, version, mode)
    else:
        story += _cover(report, facts, st, version, mode)
        story += _executive(report, facts, hist, st) + _coverage(st) + [PageBreak()]
        story += _overview(report, st)
        story += _financials(report, hist, st) + [PageBreak()]
        story += _peers(report, st)
        story += _valuation(report, facts, st)
        story += [CondPageBreak(90 * mm)] + _scorecard(report, st)
        story += _management(report, st)
        story += _appendix(report, st)

    class _Canvas(_Pages):
        pass

    _Canvas.company = clean(meta["company"]).replace("&amp;", "&")
    _Canvas.as_of = dmy(str(meta["as_of"]))
    _Canvas.footer = (
        f"EquityOS research report - v{version} - {report_id[:8]} - sha {content_hash[:12]}"
    )
    doc.multiBuild(story, canvasmaker=_Canvas)
    return buf.getvalue()
