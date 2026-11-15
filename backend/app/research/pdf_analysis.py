"""Flowables for the analytical sections of the research PDF (phases 2-6).

Pure rendering of `report["analysis"]`: nothing is computed here except display formatting. Every
statement keeps its label (VERIFIED FACT / DERIVED / INTERPRETATION / ANALYTICAL HYPOTHESIS /
MANAGEMENT STATEMENT / ASSUMPTION / SECTOR TEMPLATE) and, where it has one, its evidence ids.
"""

from __future__ import annotations

import re
from decimal import Decimal

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

from app.research.pdf import CRORE, clean, dmy, fmt_value
from app.research.pdf_pro import (
    BAND,
    CHART_A,
    CHART_B,
    INK,
    MUTED,
    NAVY,
    RULE,
    WARN_EDGE,
    WARN_FILL,
    W,
    _table,
)

KIND_COLOUR = {
    "VERIFIED FACT": "#2f7d5b",
    "FACT": "#2f7d5b",
    "DERIVED": "#1d4e89",
    "INTERPRETATION": "#c77d0a",
    "ANALYTICAL HYPOTHESIS": "#9a3412",
    "MANAGEMENT STATEMENT": "#6b21a8",
    "ASSUMPTION": "#9a3412",
    "SECTOR TEMPLATE": "#6b7280",
}


def tag(kind: str) -> str:
    """Inline evidence-status labels were removed. The classification of each figure is in the sources
    appendix and the methodology; qualifications that matter are written into the sentence itself."""
    return ""


def fv(value, unit, kind=None) -> str:
    if value is None:
        return "n/a"
    return clean(fmt_value(str(value), unit))


def f2(x) -> str:
    return str(Decimal(str(x)).quantize(Decimal("0.01")))


def compact(value, unit) -> str:
    """Table figure without its unit suffix: crore amounts as whole numbers (the heading states the unit)."""
    if value is None:
        return "n/a"
    if unit == "inr":
        from app.research.pdf import money

        return money((Decimal(str(value)) / CRORE).quantize(Decimal("1")))[:-3]
    return fv(value, unit)


def _chg(ch: dict | None, kind: str | None = None) -> str:
    if not ch:
        return "-"
    v = Decimal(ch["value"]).quantize(Decimal("0.1"))
    sign = "+" if v > 0 else ""
    if ch["type"] == "pct":
        return f"{sign}{v}%"
    return f"{sign}{Decimal(ch['value']).quantize(Decimal('0.01'))} pts"


def _ids(ids) -> str:
    ids = [i for i in (ids or []) if i]
    return clean("evidence " + ", ".join(f"#{i}" for i in ids[:4])) if ids else ""


def _analysis(report: dict) -> dict | None:
    a = report.get("analysis")
    return a if a and a.get("available") else None


def _short(name: str) -> str:
    words = [
        w
        for w in name.replace("&", " ").split()
        if w.lower() not in ("of", "the", "ltd", "limited")
    ]
    if len(words) >= 3:
        return "".join(w[0] for w in words).upper()
    return words[0] if words else name


def _hdr(st, *names, right_from=1):
    return [
        Paragraph(clean(n), st["hdrr"] if i >= right_from else st["hdr"])
        for i, n in enumerate(names)
    ]


# ---------------------------------------------------------------- section 3: analysis


def financial_analysis(report: dict, st) -> list:
    a = _analysis(report)
    if not a:
        return []
    tr = a["trend"]
    out = []
    obs = tr["observations"]
    if obs:
        out.append(Paragraph("What the figures show", st["h2"]))
        for o in obs:
            out.append(
                Paragraph(
                    f"{clean(o['text'])} {tag(o['kind'])} {_ids(o['evidence_ids'])}".strip(),
                    st["bullet"],
                    bulletText="-",
                )
            )
    if tr["unusual"]:
        out.append(Paragraph("Unusual movements", st["h2"]))
        for u in tr["unusual"]:
            out.append(
                Paragraph(
                    f"{clean(u['text'])} {tag('INTERPRETATION')} {_ids(u['evidence_ids'])}",
                    st["bullet"],
                    bulletText="-",
                )
            )
    rows = tr["annual"]
    years = sorted({p["fy"] for r in rows for p in r["points"]})[-5:]
    if rows and years:
        out.append(Paragraph("Annual history", st["h2"]))
        head = _hdr(st, "Measure", *years, "Latest change")
        table = [head]
        group = None
        spans = []
        for r in rows:
            if r["group"] != group:
                group = r["group"]
                spans.append(len(table))
                table.append(
                    [Paragraph(f"<b>{clean(group)}</b>", st["small"])] + [""] * (len(years) + 1)
                )
            by = {p["fy"]: p for p in r["points"]}
            label = r["label"] + (" *" if r.get("basis_note") else "")
            cells = [
                Paragraph(
                    clean(f"{label} ({str(r['basis'] or '').lower()})" if r["basis"] else label),
                    st["cell"],
                )
            ]
            for y in years:
                p = by.get(y)
                cells.append(Paragraph(compact(p["value"], p["unit"]) if p else "-", st["cellr"]))
            cells.append(Paragraph(_chg(r["changes"][-1] if r["changes"] else None), st["cellr"]))
            table.append(cells)
        widths = [50 * mm] + [(W - 50 * mm - 22 * mm) / len(years)] * len(years) + [22 * mm]
        t = _table(table, widths)
        t.setStyle(
            TableStyle(
                [("SPAN", (0, i), (-1, i)) for i in spans]
                + [("BACKGROUND", (0, i), (-1, i), BAND) for i in spans]
            )
        )
        out.append(t)
        notes = [f"* {r['label']}: {r['basis_note']}" for r in rows if r.get("basis_note")]
        out.append(
            Paragraph(
                clean(
                    " ".join(notes)
                    + " Amounts in Rs. crore; per-share values in rupees. Change = percent change for amounts and per-share values, "
                    "points for ratios. " + tr["rules"]
                ),
                st["small"],
            )
        )
    q = tr["quarterly"]
    if q:
        quarter_head = Paragraph("Latest quarters (Rs. crore)", st["h2"])
        qs = sorted({p["period_end"] for r in q for p in r["points"]})[-4:]
        table = [_hdr(st, "Measure", *qs, "YoY", "QoQ")]
        for r in q:
            by = {p["period_end"]: p for p in r["points"]}
            cells = [Paragraph(clean(r["label"]), st["cell"])]
            for d in qs:
                p = by.get(d)
                cells.append(Paragraph(compact(p["value"], p["unit"]) if p else "-", st["cellr"]))
            cells += [
                Paragraph(_chg(r["yoy"]), st["cellr"]),
                Paragraph(_chg(r["qoq"]), st["cellr"]),
            ]
            table.append(cells)
        n = len(qs)
        out.append(
            KeepTogether(
                [
                    quarter_head,
                    _table(
                        table, [46 * mm] + [(W - 46 * mm - 34 * mm) / n] * n + [17 * mm, 17 * mm]
                    ),
                ]
            )
        )
        out.append(
            Paragraph(
                "YoY compares with the same quarter a year earlier and QoQ with the previous quarter, "
                "only where that earlier quarter is stored. Quarterly ratios are as disclosed.",
                st["small"],
            )
        )
    if a["sector"].get("notes"):
        for n in a["sector"]["notes"]:
            out.append(Paragraph(clean(n), st["small"]))
    return out


def bank_business(report: dict, st) -> list:
    """Section 2 addition for banks: the funding and lending structure that is stored."""
    a = _analysis(report)
    if not a or a["sector"]["code"] != "BANK":
        return []
    tr = {r["key"]: r for r in a["trend"]["annual"]}
    q = {r["key"]: r for r in a["trend"]["quarterly"]}
    rows = []

    def add(label, row, kind="DERIVED"):
        if row:
            p = row["points"][-1]
            period = p["period_end"]
            rows.append([Paragraph(clean(label), st["cell"]), Paragraph(fv(p["value"], p["unit"]), st["cellr"]),
                         Paragraph(clean(period), st["cell"]),
                         Paragraph(_ids([p["evidence_id"]]), st["small"])])  # fmt: skip

    for label, key, src in (
        ("Advances (loan book)", "advances", q),
        ("Deposits", "deposits", q),
        ("Credit-deposit ratio", "cd_ratio", tr),
        ("Net interest income", "nii", q),
        ("Net interest margin", "nim", q),
        ("Gross NPA ratio", "gnpa", q),
        ("Net NPA ratio", "nnpa", q),
        ("Capital adequacy ratio", "car", q),
        ("Pre-provision operating margin", "ppop_margin", tr),
    ):
        add(label, src.get(key))
    if not rows:
        return []
    out = [
        Paragraph("Lending, funding and balance-sheet quality", st["h2"]),
        KeepTogether(
            [
                _table(
                    [_hdr(st, "Measure", "Latest", "Period", "Evidence")] + rows,
                    [60 * mm, 40 * mm, 30 * mm, W - 130 * mm],
                )
            ]
        ),
        Paragraph(
            "Not available from stored evidence: CASA ratio, retail versus corporate mix, fee income "
            "share, provision coverage and slippages, CET1 and Tier 1. Retail/corporate and CASA "
            "composition need the investor presentation or Pillar 3 documents, which are not parsed "
            "into figures.",
            st["small"],
        ),
    ]
    return out


# ---------------------------------------------------------------- section 4: peers


def _peer_chart(row: dict, company_name: str):
    pts = [(company_name, Decimal(row["company"]["value"]), True)]
    for p in row["peers"]:
        if p.get("comparable"):
            pts.append((_short(p["name"]), Decimal(p["value"]), False))
    pts.sort(key=lambda t: t[1], reverse=(row.get("better") != "lower"))
    h = 14 * len(pts) + 30
    d = Drawing(W, h)
    d.add(
        String(
            0,
            h - 9,
            dmy(
                f"{row['label']} ({row['company']['period_end']}, {str(row.get('basis') or '').lower()})"
            ),
            fontName="Helvetica-Bold",
            fontSize=8,
            fillColor=INK,
        )
    )
    ch = HorizontalBarChart()
    ch.x, ch.y, ch.width, ch.height = 70, 6, W - 120, h - 26
    ch.data = [[float(t[1]) for t in reversed(pts)]]
    ch.categoryAxis.categoryNames = [t[0] for t in reversed(pts)]
    ch.categoryAxis.labels.fontSize = 7
    ch.valueAxis.labels.fontSize = 6.5
    ch.valueAxis.valueMin = 0
    ch.valueAxis.visibleGrid = True
    ch.valueAxis.gridStrokeColor = RULE
    ch.barWidth = 8
    ch.bars.strokeColor = None
    for i, t in enumerate(reversed(pts)):
        ch.bars[(0, i)].fillColor = NAVY if t[2] else CHART_B
    ch.barLabelFormat = "%.2f"
    ch.barLabels.fontSize = 6.5
    ch.barLabels.nudge = 8
    d.add(ch)
    return d


def peer_section(report: dict, st, company_name: str) -> list:
    a = _analysis(report)
    out = []
    if not a:
        return out
    pa = a["peers"]
    peers = pa["peers"]
    rows = pa["rows"]
    if not peers or not rows:
        return out
    names = [p["name"] for p in peers][:5]
    short = [_short(n) for n in names]
    out.append(Paragraph("Comparable metrics", st["h2"]))
    table = [_hdr(st, "Metric", "This co.", *short, "Median", "Position")]
    spans, group = [], None
    for r in rows:
        if r["group"] != group:
            group = r["group"]
            spans.append(len(table))
            table.append(
                [Paragraph(f"<b>{clean(group)}</b>", st["small"])] + [""] * (len(short) + 3)
            )
        by = {p["name"]: p for p in r["peers"]}
        label = f"{r['label']}"
        sub = f"{r['company']['period_end']}, {str(r.get('basis') or '').lower()}"
        cells = [Paragraph(f"{clean(label)}<br/><font size=6 color='#6b7280'>{clean(sub)}</font>", st["cell"]),
                 Paragraph(f"<b>{fv(r['company']['value'], r['company']['unit'])}</b>", st["cellr"])]  # fmt: skip
        for n in names:
            p = by.get(n, {})
            if p.get("comparable"):
                cells.append(Paragraph(fv(p["value"], r["company"]["unit"]), st["cellr"]))
            elif p.get("value") is not None:
                cells.append(
                    Paragraph(
                        f"<font color='#9ca3af'>{fv(p['value'], r['company']['unit'])}*</font>",
                        st["cellr"],
                    )
                )
            else:
                cells.append(Paragraph("<font color='#9ca3af'>-</font>", st["cellr"]))
        cells.append(
            Paragraph(
                fv(r["median"], r["company"]["unit"]) if r.get("median") else "-", st["cellr"]
            )
        )
        pos = r.get("position")
        if r.get("rank"):
            pos_text = f"{pos} ({r['rank']}/{r['of']})"
        else:
            pos_text = pos or "not ranked"
        cells.append(Paragraph(clean(pos_text), st["cellr"]))
        table.append(cells)
    n = len(short)
    first, comp, med, pos_w = 40 * mm, 18 * mm, 15 * mm, 22 * mm
    pw = (W - first - comp - med - pos_w) / n
    t = _table(table, [first, comp] + [pw] * n + [med, pos_w])
    t.setStyle(
        TableStyle(
            [("SPAN", (0, i), (-1, i)) for i in spans]
            + [("BACKGROUND", (0, i), (-1, i), BAND) for i in spans]
        )
    )
    out.append(t)
    out.append(
        Paragraph(
            clean(
                "Grey values marked * are a different period, basis or unit and are shown for "
                "reference only; they are not used in the median or rank. " + pa["rules"] + "."
            ),
            st["small"],
        )
    )
    notes = [f"{r['label']}: {r['basis_note']}" for r in rows if r.get("basis_note")]
    if notes:
        out.append(Paragraph(clean("Basis: " + "; ".join(notes)), st["small"]))
    if pa.get("not_available"):
        out.append(Paragraph(clean(pa["not_available"] + "."), st["small"]))
    ranked = [r for r in rows if r.get("rank") and r["n_comparable"] >= 3]
    charts = [
        r
        for r in ranked
        if r["key"]
        in (
            "roe",
            "roa",
            "nim",
            "gnpa",
            "car",
            "ebit_margin",
            "net_margin",
            "pb",
            "pe",
            "cost_to_income",
        )
    ][:3]
    if charts:
        out.append(Paragraph("Comparative charts", st["h2"]))
        for r in charts:
            out += [_peer_chart(r, "This co."), Spacer(1, 4)]
    elif any(r["n_comparable"] >= 3 for r in rows) is False:
        out.append(
            Paragraph(
                "No metric has three or more comparable peers, so no chart is drawn.", st["small"]
            )
        )
    s = pa["summary"]
    out.append(Paragraph("Competitive position", st["h2"]))
    if s["ranked_metrics"]:
        out.append(
            Paragraph(
                clean(
                    f"On {s['ranked_metrics']} rankable metric(s): better than the peer median on "
                    f"{', '.join(s['better']) or 'none'}; worse on {', '.join(s['worse']) or 'none'}; "
                    f"in line on {', '.join(s['in_line']) or 'none'}. "
                )
                + tag("INTERPRETATION"),
                st["body"],
            )
        )
    else:
        out.append(
            Paragraph(
                "Cannot be established: no metric has at least three comparable peers.", st["body"]
            )
        )
    if pa["unavailable_metrics"]:
        out.append(
            Paragraph(
                clean("No company value stored for: " + ", ".join(pa["unavailable_metrics"]) + "."),
                st["small"],
            )
        )
    return out


# ---------------------------------------------------------------- section 5: management


def management_section(report: dict, st) -> list:
    a = _analysis(report)
    if not a:
        return []
    m = a["management"]
    if not m.get("available"):
        return [
            Paragraph(
                clean(m.get("reason") or "No management commentary is available."), st["body"]
            )
        ]
    doc = m["document"]
    out = [
        Paragraph(
            clean(
                f"Source: {doc['title']} ({doc['type'].replace('_', ' ')}"
                + (f", {doc['period']}" if doc.get("period") else "")
                + f"); {m['pages_read']} page(s) of its management-discussion section read. "
                + m["method"]
                + "."
            ),
            st["small"],
        )
    ]
    for t in m["topics"]:
        block = [Paragraph(clean(t["title"]), st["h2"])]
        if t["forward_looking"]:
            block.append(
                Paragraph(
                    "Forward-looking: statements of intent or expectation, not verified facts.",
                    st["small"],
                )
            )
        for it in t["items"]:
            block.append(
                Paragraph(
                    f"&quot;{clean(it['text'])}&quot; <font size=7 color='#6b7280'>(p.{it['page']})</font>",
                    st["bullet"],
                    bulletText="-",
                )
            )
        out.append(KeepTogether(block))
    out.append(Paragraph(clean(m["comparison"]), st["small"]))
    out.append(
        Paragraph(
            "Quotes are verbatim and short; they are management's words, not EquityOS findings. "
            "Where a statement and a stored figure meet, the figure in sections 3 and 4 is the evidence.",
            st["small"],
        )
    )
    return out


# ---------------------------------------------------------------- section 6: valuation


def _scen_chart(title: str, price: Decimal | None, vals: dict, unit="Rs. per share"):
    cats = (["Price"] if price is not None else []) + [k.capitalize() for k in vals]
    data = ([float(price)] if price is not None else []) + [float(v) for v in vals.values()]
    h = 130
    d = Drawing(W, h + 22)
    d.add(String(0, h + 10, title, fontName="Helvetica-Bold", fontSize=8.5, fillColor=INK))
    d.add(String(0, h, unit, fontName="Helvetica", fontSize=7, fillColor=MUTED))
    ch = VerticalBarChart()
    ch.x, ch.y, ch.width, ch.height = 38, 20, W - 100, h - 28
    ch.data = [data]
    ch.categoryAxis.categoryNames = cats
    ch.categoryAxis.labels.fontSize = 7.5
    ch.valueAxis.labels.fontSize = 7
    ch.valueAxis.valueMin = 0
    ch.valueAxis.visibleGrid = True
    ch.valueAxis.gridStrokeColor = RULE
    ch.barWidth = 22
    ch.bars.strokeColor = None
    for i, c in enumerate(cats):
        ch.bars[(0, i)].fillColor = colors.HexColor("#9ca3af") if c == "Price" else CHART_A
    ch.barLabelFormat = "%.0f"
    ch.barLabels.fontSize = 7
    ch.barLabels.nudge = 7
    d.add(ch)
    return d


def valuation_section(report: dict, st) -> list:
    a = _analysis(report)
    if not a:
        return []
    v = a["valuation"]
    out = method_plan_block(v, st)
    # historical
    h = v["historical"]
    out.append(Paragraph("A. Historical valuation", st["h2"]))
    if h.get("available"):
        rows = [_hdr(st, "Fiscal year-end", "Price date", "Close", "Basic EPS", "P/E")]
        for r in h["rows"]:
            rows.append([Paragraph(clean(r["fy"]), st["cell"]), Paragraph(clean(r["price_date"]), st["cellr"]),
                         Paragraph(fv(r["price"], "inr_per_share"), st["cellr"]), Paragraph(fv(r["eps"], "inr_per_share"), st["cellr"]),
                         Paragraph(fv(Decimal(r["pe"]).quantize(Decimal("0.01")), "x"), st["cellr"])])  # fmt: skip
        out.append(KeepTogether([_table(rows, [34 * mm, 30 * mm, 34 * mm, 34 * mm, W - 132 * mm])]))
        pe_now = v["snapshot"].get("pe")
        if pe_now and len(h["rows"]) >= 2:
            vals = [Decimal(r["pe"]) for r in h["rows"]]
            now = Decimal(pe_now["value"])
            pos = "above" if now > max(vals) else "below" if now < min(vals) else "within"
            out.append(
                Paragraph(
                    clean(
                        f"Current P/E {now.quantize(Decimal('0.01'))}x is {pos} the range of {len(vals)} year-end observations "
                        f"({min(vals).quantize(Decimal('0.01'))}x to {max(vals).quantize(Decimal('0.01'))}x). "
                        "Too few observations for a percentile or a premium to the historical average. "
                    )
                    + tag("DERIVED"),
                    st["body"],
                )
            )
        out.append(Paragraph(clean(h["formula"] + "."), st["small"]))
    else:
        out.append(Paragraph(clean(h.get("reason") or "Not available."), st["body"]))
    for s in h.get("skipped", []):
        out.append(Paragraph(clean(f"{s['fy']}: not computed - {s['reason']}."), st["small"]))
    # peer valuation
    out += historical_more_block(v, st)
    out.append(Paragraph("B. Peer valuation", st["h2"]))
    pv = v["peer"]["rows"]
    if pv:
        rows = [
            _hdr(
                st, "Multiple", "This co.", "Peer median", "Premium / discount", "Comparable peers"
            )
        ]
        for r in pv:
            rows.append([Paragraph(clean(r["label"]), st["cell"]), Paragraph(fv(r["company"], "x"), st["cellr"]),
                         Paragraph(fv(r.get("median"), "x") if r.get("median") else "-", st["cellr"]),
                         Paragraph((f"{Decimal(r['premium_pct']).quantize(Decimal('0.1'))}%" if "premium_pct" in r else "-"), st["cellr"]),
                         Paragraph(str(r["n_comparable"]), st["cellr"])])  # fmt: skip
        out.append(KeepTogether([_table(rows, [40 * mm, 30 * mm, 32 * mm, 40 * mm, W - 142 * mm])]))
        for r in pv:
            if r.get("note"):
                out.append(Paragraph(clean(f"{r['label']}: {r['note']}."), st["small"]))
    out.append(Paragraph(clean(v["peer"]["note"]), st["small"]))
    if v.get("pb_vs_roe"):
        rows = [_hdr(st, "Company", "ROE", "P/B")]
        for r in v["pb_vs_roe"]:
            rows.append(
                [
                    Paragraph(clean(r["name"]), st["cell"]),
                    Paragraph(fv(r["roe"], "percent"), st["cellr"]),
                    Paragraph(fv(r["pb"], "x"), st["cellr"]),
                ]
            )
        out += [
            Paragraph("P/B beside ROE (same-period peers)", st["h2"]),
            KeepTogether([_table(rows, [70 * mm, 40 * mm, W - 110 * mm])]),
        ]
    # intrinsic
    out.append(Paragraph("C. Intrinsic valuation", st["h2"]))
    intr = v["intrinsic"]
    price = v["snapshot"].get("price")
    price_v = Decimal(price["value"]) if price else None
    if intr.get("completed"):
        inp = intr["inputs"]
        out.append(
            Paragraph(
                clean(
                    intr["method"]
                    + f". Inputs: latest annual ROE {Decimal(inp['roe']['value']).quantize(Decimal('0.1'))}% "
                    f"({inp['roe']['period_end']}, {str(inp['roe']['basis']).lower()}), price {fv(inp['price']['value'], 'inr_per_share')}, "
                    f"P/B {fv(inp['price_to_book']['value'], 'x')}, book value per share Rs. {inp['book_value_per_share']} ({inp['book_value_note']})."
                )
                + " "
                + tag("DERIVED"),
                st["body"],
            )
        )
        rows = [
            _hdr(
                st,
                "Scenario",
                "ROE",
                "Cost of equity",
                "Growth",
                "Justified P/B",
                "Value / share",
                "vs price",
            )
        ]
        for name, sc in intr["scenarios"].items():
            if not sc.get("completed", True):
                rows.append([Paragraph(name.capitalize(), st["cellb"]), Paragraph(clean("not completed: needs " + "; ".join(sc.get("missing") or ["entered inputs"])), st["small"]), "", "", "", "", ""])  # fmt: skip
                continue
            a_ = sc["assumptions"]
            rows.append([Paragraph(name.capitalize(), st["cellb"]), Paragraph(f"{Decimal(sc['roe']).quantize(Decimal('0.1'))}%", st["cellr"]),
                         Paragraph(f"{a_['cost_of_equity']}%", st["cellr"]), Paragraph(f"{a_['growth']}%", st["cellr"]),
                         Paragraph(f"{f2(sc['justified_pb'])}x" if sc.get("justified_pb") else "n/a", st["cellr"]),
                         Paragraph(f"Rs. {sc['value_per_share']}" if sc.get("value_per_share") else "n/a", st["cellr"]),
                         Paragraph(f"{sc['vs_price_pct']}%" if sc.get("vs_price_pct") else "-", st["cellr"])])  # fmt: skip
        out.append(
            KeepTogether(
                [
                    _table(
                        rows, [24 * mm, 20 * mm, 28 * mm, 20 * mm, 28 * mm, 30 * mm, W - 150 * mm]
                    ),
                    Paragraph(clean(v["assumption_note"]) + " " + tag("ASSUMPTION"), st["small"]),
                ]
            )
        )
        vals = {
            k: Decimal(s["value_per_share"])
            for k, s in intr["scenarios"].items()
            if s.get("value_per_share")
        }
        if vals:
            out += [
                Spacer(1, 6),
                _scen_chart("Residual-income scenarios against the current price", price_v, vals),
            ]
        sens = intr["sensitivity"]
        shifts = [c["roe"] for c in sens[0]["cells"]]
        rows = [
            _hdr(
                st,
                "Cost of equity \\ ROE",
                *[f"{Decimal(x).quantize(Decimal('0.1'))}%" for x in shifts],
            )
        ]
        for r in sens:
            rows.append(
                [Paragraph(f"{r['cost_of_equity']}%", st["cell"])]
                + [
                    Paragraph(
                        f"{f2(c['justified_pb'])}x" if c["justified_pb"] else "n/a", st["cellr"]
                    )
                    for c in r["cells"]
                ]
            )
        out.append(Paragraph("Sensitivity: justified P/B", st["h2"]))
        out.append(
            KeepTogether(
                [
                    _table(rows, [38 * mm] + [(W - 38 * mm) / len(shifts)] * len(shifts)),
                    Paragraph(clean(intr["sensitivity_note"] + "."), st["small"]),
                ]
            )
        )
        mi = intr["market_implied"]
        rows = [_hdr(st, "Cost of equity", "ROE implied by current P/B")]
        for r in mi["rows"]:
            rows.append(
                [
                    Paragraph(f"{r['cost_of_equity']}%", st["cell"]),
                    Paragraph(f"{r['implied_roe']}%", st["cellr"]),
                ]
            )
        out.append(
            KeepTogether(
                [_table(rows, [50 * mm, 60 * mm]), Paragraph(clean(mi["note"] + "."), st["small"])]
            )
        )
    else:
        out.append(
            Paragraph(
                clean(
                    f"Not completed: {intr.get('reason')}"
                    + (f" ({intr['method']})" if intr.get("method") else "")
                    + "."
                ),
                st["body"],
            )
        )
    out += dcf_block(v, st)
    out += sotp_block(v, st)
    # relative scenarios
    rel = v["relative"]
    out.append(Paragraph("D. Relative valuation scenarios", st["h2"]))
    if rel.get("completed"):
        rows = [_hdr(st, "Scenario", "Peer P/E applied", "Value / share", "vs price")]
        for name, sc in rel["scenarios"].items():
            rows.append(
                [
                    Paragraph(name.capitalize(), st["cellb"]),
                    Paragraph(f"{sc['peer_pe']}x", st["cellr"]),
                    Paragraph(f"Rs. {sc['value_per_share']}", st["cellr"]),
                    Paragraph(f"{sc['vs_price_pct']}%", st["cellr"]),
                ]
            )
        out.append(KeepTogether([_table(rows, [34 * mm, 44 * mm, 44 * mm, W - 122 * mm]),
                                 Paragraph(clean(f"{rel['method']}. Spread: {rel['spread']}. Assumes {rel['assumption']}. Scenario-implied values, not target prices.") + " " + tag("DERIVED") + " Evidence: " + _ids(rel["scenarios"]["base"].get("evidence_ids", [])), st["small"])]))  # fmt: skip
        vals = {k: Decimal(s["value_per_share"]) for k, s in rel["scenarios"].items()}
        out += [
            Spacer(1, 6),
            _scen_chart("Relative-valuation scenarios against the current price", price_v, vals),
        ]
    else:
        out.append(Paragraph(clean(f"Not completed: {rel.get('reason')}."), st["body"]))
    out += relative_more_block(v, st)
    out += input_ledger_block(v, st)
    if v.get(
        "method_plan"
    ):  # the method plan above already states every method's status and reason
        return out
    out.append(Paragraph("Models not completed", st["h2"]))
    nc = list(v.get("not_completed", []))
    if intr.get("completed") is False:
        nc.insert(0, f"{intr.get('method') or 'Intrinsic valuation'}: {intr.get('reason')}")
    for line in nc or ["None."]:
        out.append(Paragraph(clean(line), st["bullet"], bulletText="-"))
    return out


def method_plan_block(v: dict, st) -> list:
    plan = v.get("method_plan")
    if not plan:
        return []
    out = [Paragraph("Valuation methods for this sector", st["h2"])]
    rows = [_hdr(st, "Method", "Applies", "Status", "Why or what is missing")]
    for m in plan:
        rows.append([Paragraph(clean(m["method"]), st["cell"]), Paragraph("yes" if m["applies"] else "no", st["cell"]), Paragraph(m["status"].lower().replace("_", " "), st["cell"]), Paragraph(clean(m["reason"] or ""), st["small"])])  # fmt: skip
    out += [_table(rows, [58 * mm, 16 * mm, 28 * mm, W - 102 * mm]), Paragraph(clean("Methods that do not apply are not forced: a lender is not valued by enterprise value or free cash flow to the firm, and a sum of the parts is used only where several segments report a result."), st["small"])]  # fmt: skip
    return out


def historical_more_block(v: dict, st) -> list:
    out = []
    for key, title, col, head in (
        ("historical_pb", "Historical P/B", "pb", "P/B"),
        ("historical_ev_ebitda", "Historical EV/EBITDA", "ev_ebitda", "EV/EBITDA"),
    ):
        h = v.get(key)
        if not h:
            continue
        if h.get("available"):
            rows = [_hdr(st, "Fiscal year-end", "Price date", head, "Evidence", right_from=1)]
            for r in h["rows"]:
                rows.append([Paragraph(clean(r["fy"]), st["cell"]), Paragraph(clean(r["price_date"]), st["cellr"]), Paragraph(f"{Decimal(r[col]).quantize(Decimal('0.01'))}x", st["cellr"]), Paragraph(", ".join(map(str, r["evidence_ids"])), st["small"])])  # fmt: skip
            out += [Paragraph(title, st["small"]), _table(rows, [34 * mm, 34 * mm, 30 * mm, W - 98 * mm]), Paragraph(clean(h["formula"] + "."), st["small"])]  # fmt: skip
        else:
            out.append(Paragraph(clean(f"{title}: not available: {h.get('reason')}."), st["small"]))
    ctx = v.get("historical_context") or {}
    if ctx:
        rows = [
            _hdr(
                st,
                "Multiple",
                "Current",
                "Min",
                "Median",
                "Max",
                "At or below",
                "Position",
                right_from=1,
            )
        ]
        gaps = []
        for c in ctx.values():
            if not c["available"]:
                gaps.append(f"{c['label']}: {c['reason']}")
                continue
            rows.append([Paragraph(c["label"], st["cellb"]), Paragraph(f"{c['current']}x", st["cellr"]), Paragraph(f"{c['min']}x", st["cellr"]), Paragraph(f"{c['median']}x", st["cellr"]), Paragraph(f"{c['max']}x", st["cellr"]), Paragraph(f"{c['share_at_or_below_pct']}%", st["cellr"]), Paragraph(c["position"].lower().replace("_", " "), st["cellr"])])  # fmt: skip
        if len(rows) > 1:
            out += [Paragraph("Historical valuation comparison: today against the company's own year ends", st["small"]), _table(rows, [28 * mm, 22 * mm, 20 * mm, 22 * mm, 20 * mm, 26 * mm, W - 138 * mm]), Paragraph(clean("Year-end observations only; the position says where today's multiple sits in the company's own past and is not a verdict on cheapness."), st["small"])]  # fmt: skip
        out += [Paragraph(clean(g + "."), st["small"]) for g in gaps]
    return out


def _grid_table(st, grid: list, key: str, head: str, colkey: str, colfmt) -> list:
    cols = [c[colkey] for c in grid[0]["cells"]]
    rows = [_hdr(st, head, *[colfmt(c) for c in cols], right_from=1)]
    for r in grid:
        rows.append(
            [Paragraph(f"{r['multiple']}x", st["cell"])]
            + [Paragraph(f"Rs. {c['value_per_share']}", st["cellr"]) for c in r["cells"]]
        )
    return [_table(rows, [34 * mm] + [(W - 34 * mm) / len(cols)] * len(cols))]


def relative_more_block(v: dict, st) -> list:
    out = []
    for key, title, per in (
        ("relative_pb", "D2. Relative valuation on P/B", "book value"),
        ("relative_ev_ebitda", "D3. Relative valuation on EV/EBITDA", "EBITDA"),
        (
            "relative_hist_pe",
            "D4. Valuation on the company's own historical P/E (context; not in the verdict)",
            "EPS",
        ),
    ):
        b = v.get(key)
        if not b:
            continue
        out.append(Paragraph(title, st["h2"]))
        if not b.get("completed"):
            out.append(Paragraph(clean(f"Not completed: {b.get('reason')}."), st["body"]))
            continue
        rows = [
            _hdr(
                st,
                "Scenario",
                b.get("multiple_label", "Peer multiple"),
                "Value / share",
                "vs price",
                "Evidence",
            )
        ]
        for name, sc in b["scenarios"].items():
            rows.append([Paragraph(name.capitalize(), st["cellb"]), Paragraph(f"{sc['peer_multiple']}x", st["cellr"]), Paragraph(f"Rs. {sc['value_per_share']}", st["cellr"]), Paragraph(f"{sc['vs_price_pct']}%", st["cellr"]), Paragraph(", ".join(map(str, sc["evidence_ids"])), st["small"])])  # fmt: skip
        out += [_table(rows, [30 * mm, 34 * mm, 34 * mm, 24 * mm, W - 122 * mm]), Paragraph(clean(f"{b['method']}. Spread: {b['spread']} ({b['peer_count']} {b.get('count_label', 'peers')}). Assumes {b['assumption']}. Scenario-implied values, not target prices.") + " " + tag("DERIVED"), st["small"])]  # fmt: skip
        out += [Paragraph(clean("Flag: " + f + "."), st["small"]) for f in b.get("flags", [])]
        grid = b["sensitivity"]["grid"]
        if grid:
            colkey = "ebitda" if key == "relative_ev_ebitda" else "own_" + per.replace(" ", "_")
            fmt = (
                (lambda c: f"{Decimal(c):,.0f}")
                if key == "relative_ev_ebitda"
                else (lambda c: f"{c}")
            )
            out += [
                Paragraph(
                    clean(
                        f"Sensitivity: value per share, {b['sensitivity']['rows']} against {b['sensitivity']['cols']}"
                    ),
                    st["small"],
                )
            ]
            out += _grid_table(st, grid, "multiple", "Multiple \\ " + per, colkey, fmt)
    return out


def sotp_block(v: dict, st) -> list:
    s = v.get("sotp")
    if not s:
        return []
    out = [Paragraph("C3. Sum of the parts", st["h2"])]
    if not s.get("applicable"):
        out.append(Paragraph(clean("Not used: " + s["reason"] + "."), st["body"]))
        return out
    rows = [_hdr(st, "Segment", "Reported result (INR crore)", "Measure", right_from=1)]
    for r in s["segments"]:
        rows.append([Paragraph(clean(r["segment"]), st["cell"]), Paragraph(_cr(r["result"]), st["cellr"]), Paragraph(clean(r["measure"]), st["small"])])  # fmt: skip
    out += [_table(rows, [W - 90 * mm, 50 * mm, 40 * mm]), Paragraph(clean(f"Segment note for the year ended {s['period_end']}, {s['basis'].lower()} basis; results as the company defines them. Multiples are entered inputs (per segment and scenario) with their source or author; none is built in."), st["small"])]  # fmt: skip
    if not s["scenarios_complete"]:
        out.append(Paragraph(clean("Not completed: needs " + _needs(s["scenarios"]) + ", entered for the bear, base and bull scenarios. To enter the multiples: " + s["how_to_enter"] + "."), st["body"]))  # fmt: skip
        return out
    rows = [_hdr(st, "Scenario", "Enterprise value", "Equity value", "Value / share", "vs price")]
    for name, sc in s["scenarios"].items():
        if not sc["completed"]:
            rows.append([Paragraph(name.capitalize(), st["cellb"]), Paragraph(clean("not completed: needs " + "; ".join(sc["missing"][:4]) + (" ..." if len(sc["missing"]) > 4 else "")), st["small"]), "", "", ""])  # fmt: skip
            continue
        rows.append([Paragraph(name.capitalize(), st["cellb"]), Paragraph(f"{_cr(sc['enterprise_value'])} cr", st["cellr"]), Paragraph(f"{_cr(sc['bridge']['equity_value'])} cr", st["cellr"]), Paragraph(f"Rs. {sc['value_per_share']}" if sc.get("value_per_share") else "n/a", st["cellr"]), Paragraph(f"{sc['vs_price_pct']}%" if sc.get("vs_price_pct") else "-", st["cellr"])])  # fmt: skip
    out.append(_table(rows, [24 * mm, 56 * mm, 36 * mm, 30 * mm, W - 146 * mm]))
    base = s["scenarios"].get("base")
    if base and base["completed"]:
        rows = [
            _hdr(
                st,
                "Base case segment",
                "Result (cr)",
                "Multiple",
                "Value (cr)",
                "Source or author",
                "As of",
            )
        ]
        for p_ in base["parts"]:
            who = p_["source"] or (
                f"{p_['author']} (own assumption)" if p_["author"] else (p_["note"] or "")
            )
            rows.append([Paragraph(clean(p_["segment"]), st["cell"]), Paragraph(_cr(p_["result"]), st["cellr"]), Paragraph(f"{p_['multiple']}x" if p_["multiple"] else "n/a", st["cellr"]), Paragraph(_cr(p_["value"]), st["cellr"]), Paragraph(clean(who), st["small"]), Paragraph(clean(p_["as_of"] or ""), st["small"])])  # fmt: skip
        out += [_table(rows, [48 * mm, 24 * mm, 20 * mm, 26 * mm, W - 142 * mm, 24 * mm])]
    for f in sorted({f for sc in s["scenarios"].values() if sc["completed"] for f in sc["flags"]}):
        out.append(Paragraph(clean("Flag: " + f + "."), st["small"]))
    if s.get("sensitivity"):
        rows = [_hdr(st, "Every multiple offset by (turns)", "Value / share", right_from=1)]
        for r in s["sensitivity"]["rows"]:
            rows.append(
                [
                    Paragraph(r["multiple_offset"], st["cell"]),
                    Paragraph(f"Rs. {r['value_per_share']}", st["cellr"]),
                ]
            )
        out += [
            _table(rows, [60 * mm, 40 * mm]),
            Paragraph(clean(s["sensitivity"]["note"] + "."), st["small"]),
        ]
    if not s["scenarios_complete"]:
        out.append(
            Paragraph(
                clean("Not completed. To enter the multiples: " + s["how_to_enter"] + "."),
                st["small"],
            )
        )
    return out


def _cr(x) -> str:
    return f"{(Decimal(x) / Decimal(10_000_000)):,.0f}"


def _needs(scenarios: dict) -> str:
    """The distinct things missing across the scenarios, said once (without the scenario name)."""
    seen: list[str] = []
    for sc in scenarios.values():
        items = list(sc.get("missing") or [])
        if sc.get("reason"):
            items.append(sc["reason"])
        for m in items:
            m = re.sub(r"\s*(in|for) the (bear|base|bull) scenario", "", m)
            m = re.sub(r"\s*\(or an entered discount rate\)", "", m)
            if m and m not in seen:
                seen.append(m)
    return "; ".join(seen)


def dcf_block(v: dict, st) -> list:
    """Discounted cash flow: scenarios, assumptions with provenance, projection, bridge, sensitivity."""
    d = v.get("dcf")
    if not d:
        return []
    out = [Paragraph("C2. Discounted cash flow (FCFF)", st["h2"])]
    if not d["applicable"]:
        out.append(Paragraph(clean(d["reason"] + "."), st["body"]))
        return out
    done = d["scenarios_complete"]
    if not done:
        out.append(Paragraph(clean("Not completed: needs " + _needs(d["scenarios"]) + ", entered for the bear, base and bull scenarios; none is assumed."), st["body"]))  # fmt: skip
        return out
    out.append(
        Paragraph(
            clean(
                "Every figure in a scenario is an entered input with its source or author, date and rationale (see the input table); none is built in. "
                f"{d['valuation_date_note']}. Results are scenario-implied values, not target prices. "
            )
            + tag("ASSUMPTION"),
            st["body"],
        )
    )
    rows = [
        _hdr(
            st,
            "Scenario",
            "Discount rate",
            "Long-run growth",
            "Enterprise value",
            "Equity value",
            "Value / share",
            "vs price",
            "Terminal share",
        )
    ]
    for name, sc in d["scenarios"].items():
        if not sc["completed"]:
            need = sc.get("reason") or "needs " + "; ".join(sc["missing"][:6]) + (
                " ..." if len(sc["missing"]) > 6 else ""
            )
            rows.append(
                [
                    Paragraph(name.capitalize(), st["cellb"]),
                    Paragraph(clean("not completed: " + need), st["small"]),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
            continue
        rows.append([
            Paragraph(name.capitalize(), st["cellb"]),
            Paragraph(f"{sc['discount_rate']['value']}%", st["cellr"]),
            Paragraph(f"{sc['assumptions']['terminal_growth']['value']}%", st["cellr"]),
            Paragraph(f"{_cr(sc['enterprise_value'])} cr", st["cellr"]),
            Paragraph(f"{_cr(sc['bridge']['equity_value'])} cr", st["cellr"]),
            Paragraph(f"Rs. {sc['value_per_share']}" if sc.get("value_per_share") else "n/a", st["cellr"]),
            Paragraph(f"{sc['vs_price_pct']}%" if sc.get("vs_price_pct") else "-", st["cellr"]),
            Paragraph(f"{sc['terminal']['share_of_enterprise_value_pct']}%" if sc["terminal"]["share_of_enterprise_value_pct"] else "n/a", st["cellr"]),
        ])  # fmt: skip
    out.append(
        _table(rows, [20 * mm, 22 * mm, 24 * mm, 26 * mm, 26 * mm, 24 * mm, 18 * mm, W - 160 * mm])
    )
    vals = {
        k: Decimal(s["value_per_share"])
        for k, s in d["scenarios"].items()
        if s["completed"] and s.get("value_per_share")
    }
    price = d.get("price")
    if vals:
        out += [
            Spacer(1, 6),
            _scen_chart(
                "DCF scenarios against the current price",
                Decimal(price["value"]) if price else None,
                vals,
            ),
        ]
    if d["flags"]:
        out += [Paragraph(clean("Flag: " + f + "."), st["small"]) for f in d["flags"]]
    base = d["scenarios"].get("base")
    if base and base["completed"]:
        a = base["assumptions"]
        rows = [_hdr(st, "Base-case input", "Value", "Source or author", "As of", "Why")]
        for key, x in a.items():
            who = x["source"] or f"{x['author']} (own assumption)"
            rows.append([Paragraph(clean(key.replace("_", " ")), st["cell"]), Paragraph(f"{x['value']}%", st["cellr"]), Paragraph(clean(who), st["small"]), Paragraph(clean(x["as_of"] or ""), st["small"]), Paragraph(clean(x["rationale"] or ""), st["small"])])  # fmt: skip
        dr = base["discount_rate"]
        rows.append([Paragraph("discount rate", st["cell"]), Paragraph(f"{dr['value']}%", st["cellr"]), Paragraph(clean(dr["label"]), st["small"]), "", Paragraph("evidence " + ", ".join(map(str, dr["input_ids"])), st["small"])])  # fmt: skip
        out += [
            Paragraph("Base-case assumptions and their provenance", st["small"]),
            _table(rows, [40 * mm, 18 * mm, 42 * mm, 22 * mm, W - 122 * mm]),
        ]
        rows = [_hdr(st, "INR crore", *[r["fy"] for r in base["projection"]], right_from=1)]
        for label, key in (("Revenue", "revenue"), ("EBIT", "ebit"), ("Tax", "tax"), ("Depreciation", "depreciation"), ("Capital expenditure", "capex"), ("Change in working capital", "change_in_working_capital"), ("FCFF", "fcff"), ("Present value", "pv")):  # fmt: skip
            rows.append(
                [Paragraph(label, st["cellb"] if key == "fcff" else st["cell"])]
                + [Paragraph(_cr(r[key]), st["cellr"]) for r in base["projection"]]
            )
        n = len(base["projection"])
        out += [
            Paragraph("Base-case projection", st["small"]),
            _table(rows, [W - 22 * mm * n] + [22 * mm] * n),
        ]
        b, tm = base["bridge"], base["terminal"]
        out.append(Paragraph(clean(f"Base case: present value of the explicit cash flows INR {_cr(base['pv_explicit'])} crore plus present value of the terminal value INR {_cr(tm['pv_terminal_value'])} crore ({tm['share_of_enterprise_value_pct']}% of enterprise value) gives enterprise value INR {_cr(b['enterprise_value'])} crore; less net debt INR {_cr(b['net_debt'])} crore and non-controlling interests INR {_cr(b['non_controlling_interests'])} crore gives equity value INR {_cr(b['equity_value'])} crore over {Decimal(b['shares']):,.0f} shares.") + " " + tag("DERIVED"), st["small"]))  # fmt: skip
    s = d.get("sensitivity")
    if s:
        cols = [c["terminal_growth"] for c in s["wacc_vs_growth"][0]["cells"]]
        rows = [_hdr(st, "WACC \\ long-run growth", *[f"{c}%" for c in cols], right_from=1)]
        for r in s["wacc_vs_growth"]:
            rows.append(
                [Paragraph(f"{r['wacc']}%", st["cell"])]
                + [
                    Paragraph(
                        f"Rs. {c['value_per_share']}" if c["value_per_share"] else "n/a",
                        st["cellr"],
                    )
                    for c in r["cells"]
                ]
            )
        out += [
            Paragraph(
                "Sensitivity: value per share, discount rate against long-run growth", st["small"]
            ),
            _table(rows, [34 * mm] + [(W - 34 * mm) / len(cols)] * len(cols)),
        ]
        cols = [c["revenue_growth"] for c in s["margin_vs_growth"][0]["cells"]]
        rows = [
            _hdr(st, "EBIT margin \\ first-year growth", *[f"{c}%" for c in cols], right_from=1)
        ]
        for r in s["margin_vs_growth"]:
            rows.append(
                [Paragraph(f"{r['operating_margin']}%", st["cell"])]
                + [
                    Paragraph(
                        f"Rs. {c['value_per_share']}" if c["value_per_share"] else "n/a",
                        st["cellr"],
                    )
                    for c in r["cells"]
                ]
            )
        out += [Paragraph("Sensitivity: value per share, operating margin against first-year revenue growth", st["small"]), _table(rows, [44 * mm] + [(W - 44 * mm) / len(cols)] * len(cols)), Paragraph(clean(s["note"] + "."), st["small"])]  # fmt: skip
    if not done:
        out.append(
            Paragraph(
                clean("Not completed. To enter the inputs: " + d["how_to_enter"] + "."), st["small"]
            )
        )
    out += [Paragraph(clean("Method: " + " ".join(d["method"])), st["small"])]
    return out


def _input_value(x: dict) -> str:
    unit = "%" if x["unit"] == "percent" else ""
    return f"{x['value']}{unit}"


def input_ledger_block(v: dict, st) -> list:
    """The valuation input ledger: actuals, sourced inputs, user assumptions, derived outputs, each with provenance."""
    led = v.get("input_ledger")
    if not led:
        return []
    out = [Paragraph("Valuation inputs and their provenance", st["h2"])]
    out.append(
        Paragraph(
            clean(
                "Four kinds of input are kept apart: historical actuals (validated evidence), sourced inputs (a named, dated source), "
                "user assumptions (a person's judgement with author and rationale) and derived outputs (calculations over the others). "
                "No risk-free rate, equity risk premium, beta, cost of equity or growth rate is built in."
            ),
            st["body"],
        )
    )
    if led["historical_actuals"]:
        rows = [_hdr(st, "Historical actual", "Value", "As of", "Evidence", right_from=1)]
        for a_ in led["historical_actuals"]:
            rows.append([Paragraph(clean(a_["label"]), st["cell"]), Paragraph(clean(f"{Decimal(a_['value']).quantize(Decimal('0.01'))}"), st["cellr"]), Paragraph(clean(a_.get("period_end") or ""), st["cellr"]), Paragraph(str(a_["evidence_id"]), st["cellr"])])  # fmt: skip
        out.append(_table(rows, [W - 100 * mm, 30 * mm, 36 * mm, 34 * mm]))
    entered = [("Sourced input", x) for x in led["sourced_inputs"]] + [
        ("User assumption", x) for x in led["user_assumptions"]
    ]
    if entered:
        rows = [_hdr(st, "Kind", "Input", "Value", "As of", "Source / author", "Why", "Id")]
        for kind, x in entered:
            who = x["source"] if x["source_backed"] else f"{x['author']} (own assumption)"
            label = x["label"] + (f" ({x['scenario']})" if x["scenario"] else "")
            flag = " STALE" if x["stale"] else ""
            rows.append([Paragraph(kind, st["small"]), Paragraph(clean(label), st["cell"]), Paragraph(_input_value(x), st["cellr"]), Paragraph(clean((x["as_of"] or "") + flag), st["small"]), Paragraph(clean(who), st["small"]), Paragraph(clean(x["rationale"] or ""), st["small"]), Paragraph(str(x["evidence_id"]), st["cellr"])])  # fmt: skip
        out.append(
            _table(rows, [24 * mm, 30 * mm, 18 * mm, 24 * mm, 36 * mm, W - 150 * mm, 12 * mm])
        )
    else:
        out.append(Paragraph("No valuation input has been entered for this company.", st["body"]))
    if led["capm"].get("completed"):
        c = led["capm"]
        out.append(Paragraph(clean(f"Derived: cost of equity {c['value']}% = {c['formula']} (evidence {c['evidence_id']}, from inputs {', '.join(map(str, c['input_ids']))}).") + " " + tag("DERIVED"), st["small"]))  # fmt: skip
    if led["market_inputs_missing"]:
        out.append(Paragraph(clean("Market inputs not entered (none is assumed): " + ", ".join(led["market_inputs_missing"]).lower() + "."), st["small"]))  # fmt: skip
    for f in led["flags"]:
        out.append(Paragraph(clean("Flag: " + f + "."), st["small"]))
    if led["derived_outputs"]:
        rows = [
            _hdr(
                st,
                "Derived output",
                "Justified P/B",
                "Value / share",
                "Rests on evidence",
                right_from=1,
            )
        ]
        for o in led["derived_outputs"]:
            rows.append([Paragraph(o["scenario"].capitalize(), st["cellb"]), Paragraph(f"{f2(o['justified_pb'])}x" if o.get("justified_pb") else "n/a", st["cellr"]), Paragraph(f"Rs. {o['value_per_share']}" if o.get("value_per_share") else "n/a", st["cellr"]), Paragraph(", ".join(map(str, o["evidence_ids"])), st["small"])])  # fmt: skip
        out.append(_table(rows, [28 * mm, 30 * mm, 34 * mm, W - 92 * mm]))
    out.append(
        Paragraph(
            clean("How to enter or change an input: " + led["how_to_enter"] + "."), st["small"]
        )
    )
    return out


# ---------------------------------------------------------------- verdict

VCOL = {"BUY": "#2f7d5b", "HOLD": "#3b6ea5", "SELL": "#b3442f", "WATCH": "#c77d0a"}


def verdict_block(report: dict, st) -> list:
    a = _analysis(report)
    v = a.get("verdict") if a else None
    if not v:
        return []
    lv, conf = v["levels"], v["confidence"]
    col = VCOL[v["verdict"]]
    head = Table(
        [
            [
                Paragraph(f'<font color="{col}"><b>{v["verdict"]}</b></font>', st["kpi_v"]),
                Paragraph(
                    clean(f"Evidence confidence: {conf['band']} ({conf['points']} / 100)"),
                    st["cellb"],
                ),
                Paragraph(
                    clean(
                        f"Net points {v['net_points']:+d} (BUY at +3 or more, SELL at -3 or less)"
                    ),
                    st["cell"],
                ),
            ]
        ],
        colWidths=[34 * mm, 62 * mm, W - 96 * mm],
    )
    head.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor(col)),
                ("BACKGROUND", (0, 0), (-1, -1), BAND),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    out = [Paragraph("EquityOS verdict", st["h2"]), head, Spacer(1, 6)]
    rows = [_hdr(st, "Component", "Reading", "Points", "Weight", right_from=2)]
    for c in v["components"]:
        pts = "n/a" if not c["available"] else f"{c['points']:+d}"
        rows.append(
            [
                Paragraph(clean(c["label"]), st["cellb"]),
                Paragraph(clean(c["reading"]), st["cell"]),
                Paragraph(pts, st["cellr"]),
                Paragraph(str(c["weight"]), st["cellr"]),
            ]
        )
    out.append(KeepTogether([_table(rows, [34 * mm, W - 62 * mm, 14 * mm, 14 * mm])]))
    out.append(Spacer(1, 6))
    if lv.get("target"):
        side = "BUY" if v["verdict"] == "BUY" else "SELL"
        rows = [
            _hdr(st, "Trade level", "Value", "Basis", right_from=1),
            [Paragraph("Reference price", st["cell"]), Paragraph(f"Rs. {lv['price']}", st["cellr"]), Paragraph(clean(f"close on {lv['price_date']}" + (f"; STALE: {lv['price_lag_days']} trading day(s) older than the latest expected close ({lv['price_expected_date']}), so the levels below are not current" if lv.get("price_stale") else "")), st["cell"])],
            [Paragraph("Target" if side == "BUY" else "Downside target", st["cellb"]), Paragraph(f"Rs. {lv['target']} ({'+' if Decimal(lv['target_pct']) > 0 else ''}{lv['target_pct']}%)", st["cellr"]),
             Paragraph(clean(("mean base-case " if side == "BUY" else "mean bear-case ") + "scenario value (" + ", ".join(lv["value_sources"]) + "); no time horizon"
                   + ("; one method only, so half of the gap to that value is counted" if lv.get("single_method") else "")), st["cell"])],
            [Paragraph("Stop-loss" if side == "BUY" else "Invalidation level", st["cellb"]), Paragraph(f"Rs. {lv['stop_loss']} ({lv['stop_pct']}%)", st["cellr"]),
             Paragraph(clean(f"2 x 14-day average true range (Rs. {Decimal(lv["atr14"]).quantize(Decimal("0.01"))}), kept within 5% to 15% of the price"), st["cell"])],
            [Paragraph("Reward-to-risk", st["cell"]), Paragraph(lv["reward_to_risk"], st["cellr"]), Paragraph("target distance / stop distance; at least 1.5 required", st["cell"])],
        ]  # fmt: skip
        out.append(KeepTogether([_table(rows, [34 * mm, 42 * mm, W - 76 * mm])]))
    else:
        out.append(
            Paragraph(f"No trade levels are given: the verdict is {v['verdict']}.", st["body"])
        )
        if lv.get("would_turn_buy_below"):
            out.append(
                Paragraph(
                    clean(
                        f"With the other components unchanged, the valuation component alone would turn positive at a price of "
                        f"Rs. {lv['would_turn_buy_below']} or below, and negative at Rs. {lv['would_turn_sell_above']} or above "
                        f"(base-case value Rs. {lv['base_value']}; bear Rs. {lv['bear_value']}, bull Rs. {lv['bull_value']}). "
                        "A BUY would still need net points of +3 and a reward-to-risk of 1.5."
                    ),
                    st["body"],
                )
            )
    for r in v["reasons"]:
        out.append(Paragraph(clean(f"Why not a trade: {r}."), st["small"]))
    f = conf["factors"]
    out.append(
        Paragraph(
            clean(
                f"Confidence: {f['coverage']}; {f['agreement']}; {f['evidence_quality']}. {conf['note'].capitalize()}."
            ),
            st["small"],
        )
    )
    out += verdict_more_block(v, st)
    out.append(Paragraph(clean(v["rules"] + ". " + v["disclosure"]), st["small"]))
    return out


def verdict_more_block(v: dict, st) -> list:
    out = []
    tp = v["levels"].get("target_price") or {}
    if tp.get("available"):
        rng = tp["range"]
        out.append(Paragraph("Valuation-linked target price", st["h2"]))
        rows = [_hdr(st, "Item", "Value", "Basis", right_from=1),
                [Paragraph(clean(tp["kind"].capitalize()), st["cellb"]), Paragraph(f"Rs. {tp['value']} ({'+' if Decimal(tp['vs_price_pct']) > 0 else ''}{tp['vs_price_pct']}%)", st["cellr"]), Paragraph(clean("from " + ", ".join(tp["sources"]) + ("; one method only, so half of the gap is counted" if tp["single_method"] else "")), st["cell"])],
                [Paragraph("Bear to bull range", st["cell"]), Paragraph(f"Rs. {rng['bear'] or 'n/a'} to Rs. {rng['bull'] or 'n/a'}", st["cellr"]), Paragraph("mean of each completed method's bear and bull values", st["cell"])],
                [Paragraph("Review horizon", st["cell"]), Paragraph(f"{tp['review_horizon_months']} months", st["cellr"]), Paragraph("a review convention, not a forecast of timing", st["cell"])]]  # fmt: skip
        out += [
            _table(rows, [40 * mm, 46 * mm, W - 86 * mm]),
            Paragraph(clean(tp["note"] + "."), st["small"]),
        ]
    else:
        out.append(
            Paragraph(
                clean(
                    "Valuation-linked target price: " + (tp.get("reason") or "not available") + "."
                ),
                st["small"],
            )
        )
    sc = v.get("scenarios") or {}
    if sc.get("available"):
        out.append(Paragraph("Upside and downside scenarios", st["h2"]))
        rows = [
            _hdr(
                st,
                "Case",
                "Value / share",
                "vs price",
                "Methods",
                "Lowest",
                "Highest",
                right_from=1,
            )
        ]
        for r in sc["rows"]:
            rows.append([Paragraph(r["case"].capitalize(), st["cellb"]), Paragraph(f"Rs. {r['value_per_share']}" if r["value_per_share"] else "n/a", st["cellr"]), Paragraph(f"{r['vs_price_pct']}%" if r["vs_price_pct"] else "-", st["cellr"]), Paragraph(str(r["methods"]), st["cellr"]), Paragraph(f"Rs. {r['low']}" if r["low"] else "-", st["cellr"]), Paragraph(f"Rs. {r['high']}" if r["high"] else "-", st["cellr"])])  # fmt: skip
        out += [
            _table(rows, [26 * mm, 32 * mm, 24 * mm, 22 * mm, 30 * mm, W - 134 * mm]),
            Paragraph(clean(sc["note"]), st["small"]),
        ]
    hz = v.get("horizon") or {}
    if hz:
        out.append(Paragraph("Investment horizon", st["h2"]))
        out.append(Paragraph(clean(hz["statement"]), st["body"]))
        for p_ in hz["method_horizons"]:
            out.append(Paragraph(clean(p_.capitalize() + "."), st["bullet"], bulletText="-"))
    rk = v.get("risk") or {}
    if rk:
        out.append(
            Paragraph(
                clean(
                    f"Risk assessment: {rk['level'].lower()} ({rk['points']} points; {rk['measurable']})"
                ),
                st["h2"],
            )
        )
        rows = [_hdr(st, "Factor", "Reading", "Points", right_from=2)]
        for f in rk["factors"]:
            rows.append([Paragraph(clean(f["label"]), st["cellb"]), Paragraph(clean(f["reading"]), st["cell"]), Paragraph("n/a" if not f["available"] else str(f["points"]), st["cellr"])])  # fmt: skip
        out += [
            KeepTogether([_table(rows, [52 * mm, W - 68 * mm, 16 * mm])]),
            Paragraph(clean(rk["rule"]), st["small"]),
        ]
    trg = v.get("triggers") or []
    if trg:
        out.append(Paragraph("What would change this verdict (thesis monitoring)", st["h2"]))
        rows = [_hdr(st, "Item", "Now", "Changes when", right_from=9)]
        for t_ in trg:
            rows.append([Paragraph(clean(t_["component"]), st["cellb"]), Paragraph(clean(str(t_["now"])), st["cell"]), Paragraph(clean(t_["changes_when"]), st["cell"])])  # fmt: skip
        out.append(_table(rows, [34 * mm, 22 * mm, W - 56 * mm]))
    bt = v.get("backtest") or {}
    out.append(Paragraph("Back-test and threshold validation", st["h2"]))
    if bt.get("available"):
        b = bt["buckets"]
        parts = []
        for k in ("BUY", "WATCH", "SELL"):
            x = b[k]
            parts.append(f"{k}: n={x['n']}" + (f", mean excess return {x['mean_excess_pct']}%, hit rate {x['hit_rate_pct']}%" if x["n"] else "") + ("" if x["sufficient"] else " (too few to read)"))  # fmt: skip
        out.append(Paragraph(clean(f"A three-component subset of the rule (trend, trajectory and a P/E-history valuation proxy; the score, peer and scenario components cannot be rebuilt for past dates) was tested on {bt['observations']} month-end observations of the pilot companies ({bt['first_as_of']} to {bt['last_as_of']}), forward horizon {bt['horizon_trading_days']} trading days: " + "; ".join(parts) + f". Reading: {bt['reading'].lower()}. Generated {bt['generated_on']}."), st["small"]))  # fmt: skip
    else:
        out.append(
            Paragraph(clean(bt.get("reason", "The back-test has not been run") + "."), st["small"])
        )
    tv = bt.get("threshold_validation") or {}
    for c in tv.get("checks", []):
        if not c["ok"]:
            out.append(
                Paragraph(
                    clean("Threshold finding: " + c["check"] + ": " + c["detail"] + "."),
                    st["small"],
                )
            )
    out.append(
        Paragraph(clean("Limitations: " + " ".join(bt.get("limitations", [])[:5])), st["small"])
    )
    return out


# ---------------------------------------------------------------- section 7: thesis


def _qualified(p: dict) -> str:
    """The point's sentence with the qualification that matters written in words, not as a label."""
    text = clean(p["text"])
    if "HYPOTHESIS" in p.get("kind", ""):
        return "Hypothesis: " + text[:1].lower() + text[1:]
    if "MANAGEMENT" in p.get("kind", ""):
        return f"&quot;{text}&quot; (management&#39;s own statement; {clean(p.get('basis', '').replace('management statement, ', ''))})"
    return text


def _points(title: str, points: list[dict], st) -> list:
    block = [Paragraph(clean(title), st["h2"])]
    if not points:
        block.append(Paragraph("None established from the evidence.", st["body"]))
    for p in points:
        block.append(
            Paragraph(
                f"{_qualified(p)} <font size=6.5 color='#6b7280'>{_ids(p['evidence_ids'])}</font>",
                st["bullet"],
                bulletText="-",
            )
        )
    return block


def thesis_section(report: dict, st) -> list:
    a = _analysis(report)
    if not a:
        return []
    t = a["thesis"]
    out = [Paragraph(clean(t["rules"].capitalize() + "."), st["small"])]
    out += _points("Bull case", t["bull"], st)
    out += _points("Bear case", t["bear"], st)
    out += verdict_block(report, st)
    out += _points("Key catalysts", t["catalysts"], st)
    reg = t.get("risk_register")
    if reg:
        out.append(Paragraph("Risks by family", st["h2"]))
        for title, key in (
            ("Business and financial", "business_financial"),
            ("Management and governance", "management_governance"),
            ("Industry and regulatory", "industry_regulatory"),
            ("Valuation", "valuation"),
        ):
            out.append(Paragraph(f"<b>{clean(title)}</b>", st["small"]))
            if not reg[key]:
                out.append(
                    Paragraph(
                        "None established from the stored evidence.", st["bullet"], bulletText="-"
                    )
                )
            for p in reg[key]:
                tagtxt = (
                    " (sector context, not evidence about this company)"
                    if "SECTOR" in p.get("kind", "")
                    else ""
                )
                out.append(
                    Paragraph(
                        _qualified(p)
                        + clean(tagtxt)
                        + f" <font size=6.5 color='#6b7280'>{_ids(p['evidence_ids'])}</font>",
                        st["bullet"],
                        bulletText="-",
                    )
                )
    r = t["risks"]
    out.append(Paragraph("Data and evidence risks" if reg else "Key risks", st["h2"]))
    for title, pts in (
        ("Data and evidence", r["data"]),
        *(
            ()
            if reg
            else (
                ("Company-specific (from the evidence)", r["company"]),
                ("Valuation", r["valuation"]),
                ("Governance", r["governance"]),
            )
        ),
    ):
        if pts:
            out.append(Paragraph(f"<b>{clean(title)}</b>", st["small"]))
            for p in pts:
                out.append(Paragraph(_qualified(p), st["bullet"], bulletText="-"))
    if not reg:
        out.append(
            Paragraph(
                "<b>Sector risks</b> <font size=7 color='#6b7280'>(industry-level, not evidence about this company)</font>",
                st["small"],
            )
        )
    for s in [] if reg else r["sector"]:
        out.append(
            Paragraph(
                f"<b>{clean(s['title'])}.</b> {clean(s['text'])}", st["bullet"], bulletText="-"
            )
        )
    mon = t["monitoring"]
    out.append(Paragraph("Monitoring indicators", st["h2"]))
    rows = [_hdr(st, "Indicator", "Latest", "As of", "Change", "Peer median", "Watch for")]
    for m in mon:
        if m["available"]:
            ch = m.get("yoy") or m.get("change")
            better = {"higher": "a fall", "lower": "a rise"}.get(m.get("better"), "a sharp move")
            rows.append([Paragraph(clean(m["label"]), st["cell"]), Paragraph(fv(m["value"], m["unit"]), st["cellr"]), Paragraph(clean(m["period_end"]), st["cell"]),
                         Paragraph(_chg(ch) + (" YoY" if m.get("yoy") else ""), st["cellr"]), Paragraph(fv(m.get("peer_median"), m["unit"]) if m.get("peer_median") else "-", st["cellr"]),
                         Paragraph(better, st["cell"])])  # fmt: skip
        else:
            rows.append(
                [
                    Paragraph(clean(m["label"]), st["cell"]),
                    Paragraph("not available", st["cellr"]),
                    "",
                    "",
                    "",
                    Paragraph("-", st["cell"]),
                ]
            )
    out.append(
        KeepTogether([_table(rows, [44 * mm, 28 * mm, 24 * mm, 26 * mm, 24 * mm, W - 146 * mm])])
    )
    return out


def conclusion_section(report: dict, st) -> list:
    a = _analysis(report)
    if not a:
        return []
    t = a["thesis"]
    traj = t["trajectory"]
    out = [
        Paragraph(
            clean(
                f"Direction of financial performance on the latest annual evidence: {traj['label']}. "
            )
            + tag("INTERPRETATION"),
            st["body"],
        ),
        Spacer(1, 4),
    ]
    for qa in t["conclusion"]["answers"]:
        out.append(
            KeepTogether(
                [
                    Paragraph(f"<b>{clean(qa['question'])}</b>", st["body"]),
                    Paragraph(
                        f"{clean(qa['answer'])} <font size=6.5 color='#6b7280'>{_ids(qa['evidence_ids'])}</font>",
                        st["body"],
                    ),
                    Spacer(1, 3),
                ]
            )
        )
    t2 = Table(
        [
            [
                Paragraph(
                    clean(
                        "The verdict, target and stop-loss in the Investment thesis and verdict section come from a stated rule set (methodology v2). This conclusion is an evidence summary, not investment advice or a forecast."
                    ),
                    st["body"],
                )
            ]
        ],
        colWidths=[W],
    )
    t2.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), WARN_FILL),
                ("LINEBEFORE", (0, 0), (0, -1), 2.2, WARN_EDGE),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    out.append(t2)
    return out


# ---------------------------------------------------------------- appendix


def analysis_register(report: dict, st) -> list:
    a = _analysis(report)
    if not a:
        return []
    rows = [
        _hdr(st, "Measure", "Period", "Basis", "Value", "Type", "Ev. id", "Document", right_from=3)
    ]
    seen = set()
    for r in a["trend"]["annual"]:
        for p in r["points"]:
            if p["evidence_id"] in seen:
                continue
            seen.add(p["evidence_id"])
            doc = (p.get("document_id") or "")[:8]
            page = f" p.{p['page']}" if p.get("page") else ""
            rows.append([Paragraph(clean(r["label"]), st["small"]), Paragraph(clean(p["period_end"]), st["small"]), Paragraph(clean((p.get("basis") or "").lower()), st["small"]),
                         Paragraph(fv(p["value"], p["unit"]), st["small"]), Paragraph(clean(p["kind"].title()), st["small"]), Paragraph(f"#{p['evidence_id']}", st["small"]),
                         Paragraph(clean(doc + page), st["small"])])  # fmt: skip
    for r in a["peers"]["rows"]:
        for who, p in [("This co.", {**r["company"], "name": "This co."})] + [
            (x["name"], x) for x in r["peers"] if x.get("evidence_id")
        ]:
            ev = p.get("evidence_id")
            if not ev or ev in seen:
                continue
            seen.add(ev)
            rows.append([Paragraph(clean(f"{r['label']} - {_short(who)}"), st["small"]), Paragraph(clean(p.get("period_end") or ""), st["small"]), Paragraph(clean((r.get("basis") or "").lower()), st["small"]),
                         Paragraph(fv(p.get("value"), r["company"]["unit"]), st["small"]), Paragraph("Derived", st["small"]), Paragraph(f"#{ev}", st["small"]), Paragraph("", st["small"])])  # fmt: skip
    if len(rows) == 1:
        return []
    return [
        Paragraph("Appendix A2 - Evidence behind the analysis", st["h1"]),
        _table(rows, [44 * mm, 22 * mm, 24 * mm, 30 * mm, 17 * mm, 16 * mm, W - 153 * mm]),
        Paragraph(
            "Peer values are listed with the peer's evidence id; every id can be opened in EquityOS.",
            st["small"],
        ),
    ]
