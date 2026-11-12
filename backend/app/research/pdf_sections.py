"""PDF sections for the industry overview, company profile, growth comparison and cyclicality.

Pure rendering of `report["analysis"]` (see industry.py, profile.py, cyclicality.py). Text that is a
sector template says so in its own note; quoted passages carry their page; nothing is estimated here.
"""

from __future__ import annotations

from decimal import Decimal

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

from app.research.pdf import clean
from app.research.pdf_analysis import _analysis, _hdr, _ids, compact, fv
from app.research.pdf_pro import BAND, CHART_A, CHART_B, INK, NAVY, RULE, W, _table


def _bullets(items, st) -> list:
    return [Paragraph(clean(i), st["bullet"], bulletText="-") for i in items]


def _note_box(text: str, st) -> Table:
    t = Table([[Paragraph(clean(text), st["small"])]], colWidths=[W])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BAND),
                ("LINEBEFORE", (0, 0), (0, -1), 2, colors.HexColor("#6b7280")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return t


def _hbar(title: str, rows: list[tuple[str, Decimal, bool]], label_w: int = 110, fmt: str = "%.1f"):
    h = 14 * len(rows) + 28
    d = Drawing(W, h)
    d.add(String(0, h - 9, title, fontName="Helvetica-Bold", fontSize=8, fillColor=INK))
    ch = HorizontalBarChart()
    ch.x, ch.y, ch.width, ch.height = label_w, 6, W - label_w - 50, h - 26
    ch.data = [[float(v) for _, v, _ in reversed(rows)]]
    ch.categoryAxis.categoryNames = [n[:26] for n, _, _ in reversed(rows)]
    ch.categoryAxis.labels.fontSize = 7
    ch.valueAxis.labels.fontSize = 6.5
    ch.valueAxis.valueMin = min(0.0, min(float(v) for _, v, _ in rows))  # bars start at zero
    ch.valueAxis.visibleGrid = True
    ch.valueAxis.gridStrokeColor = RULE
    ch.bars.strokeColor = None
    ch.barWidth = 8
    for i, (_, _, me) in enumerate(reversed(rows)):
        ch.bars[(0, i)].fillColor = NAVY if me else CHART_B if me is False else CHART_A
    ch.barLabelFormat = fmt
    ch.barLabels.fontSize = 6.5
    ch.barLabels.nudge = 8
    d.add(ch)
    return d


def _pp(x) -> str:
    v = Decimal(x)
    return f"{'+' if v > 0 else ''}{v}"


# ---------------------------------------------------------------- industry overview


def industry_section(report: dict, st) -> list:
    a = _analysis(report)
    ind = a.get("industry") if a else None
    if not ind:
        return []
    out = [
        _note_box(ind["note"], st),
        Paragraph("Industry background and business model", st["h2"]),
        Paragraph(clean(ind["background"]), st["body"]),
        Paragraph("How the industry makes money", st["h2"]),
        *_bullets(ind["how_it_earns"], st),
        Paragraph("What drives revenue", st["h2"]),
        *_bullets(ind["revenue_drivers"], st),
        Paragraph("Industry structure and competition", st["h2"]),
        Paragraph(clean(ind["structure"]), st["body"]),
    ]
    if ind.get("industry_note"):
        out.append(Paragraph(clean(ind["industry_note"]), st["body"]))
    peers = ind["fit"]["peers"]
    if peers:
        out.append(
            Paragraph(
                clean(
                    "Major listed players in this company's stored peer group: "
                    + ", ".join(peers)
                    + "."
                ),
                st["body"],
            )
        )
    out += [
        Paragraph("Demand drivers and growth opportunities", st["h2"]),
        *_bullets([*ind["demand"], *ind["opportunities"]], st),
        Paragraph("Structural challenges", st["h2"]),
        *_bullets(ind["challenges"], st),
        Paragraph("Regulation", st["h2"]),
        Paragraph(clean(ind["regulation"]), st["body"]),
        Paragraph("How the company fits into the industry", st["h2"]),
        Paragraph(clean(ind["fit"]["text"]), st["body"]),
    ]
    return out


# ---------------------------------------------------------------- company and management profile


def _quote_table(items: list[dict], st) -> Table:
    rows = [_hdr(st, "Statement (verbatim from the annual report)", "Page", right_from=1)]
    for i in items:
        rows.append(
            [
                Paragraph(f"&quot;{clean(i['text'])}&quot;", st["cell"]),
                Paragraph(str(i["page"]), st["cellr"]),
            ]
        )
    return _table(rows, [W - 18 * mm, 18 * mm])


def _external_block(ext: dict | None, st) -> list:
    """Verified quotations from the company's own web pages, each with its page and date."""
    if not ext or not ext.get("available"):
        return []
    out: list = [Paragraph("From the company's website", st["h2"])]
    out.append(Paragraph(clean(ext["method"]), st["small"]))

    def source(i: dict) -> Paragraph:
        return Paragraph(clean(f"{i['publisher']}, retrieved {i['retrieved']}"), st["cell"])

    if ext["leadership"]:
        rows = [_hdr(st, "Name", "Title and quotation", "Source")]
        for i in ext["leadership"]:
            rows.append(
                [
                    Paragraph(clean(i["name"] or ""), st["cellb"]),
                    Paragraph(f"{clean(i['role'])}: &quot;{clean(i['quote'])}&quot;", st["cell"]),
                    source(i),
                ]
            )
        out.append(
            KeepTogether(
                [
                    Paragraph("Leadership as stated by the company", st["h2"]),
                    _table(rows, [38 * mm, W - 38 * mm - 40 * mm, 40 * mm]),
                ]
            )
        )
    for key, title in (
        ("history", "History as stated by the company"),
        ("business", "Business description as stated by the company"),
    ):
        if ext[key]:
            rows = [_hdr(st, "Statement (verbatim)", "Source")]
            for i in ext[key]:
                rows.append([Paragraph(f"&quot;{clean(i['quote'])}&quot;", st["cell"]), source(i)])
            out.append(
                KeepTogether([Paragraph(title, st["h2"]), _table(rows, [W - 40 * mm, 40 * mm])])
            )
    return out


def profile_section(report: dict, st) -> list:
    a = _analysis(report)
    pf = a.get("profile") if a else None
    if not pf:
        return []
    out: list = []
    seg = pf["segments"]
    gaps: list[
        str
    ] = []  # blocks with nothing to show are not printed; they are listed once, at the end
    if seg.get("available"):
        out.append(Paragraph("Business segments and revenue contribution", st["h2"]))
        rows = [_hdr(st, "Segment", "Revenue (Rs. crore)", "Share", "Growth", "Result margin")]
        for r in seg["rows"]:
            rows.append(
                [
                    Paragraph(clean(r["segment"]), st["cell"]),
                    Paragraph(compact(r["revenue"], r["unit"]), st["cellr"]),
                    Paragraph(f"{r['share_pct']}%" if r["share_pct"] else "n/a", st["cellr"]),
                    Paragraph(f"{r['growth_pct']}%" if r["growth_pct"] else "n/a", st["cellr"]),
                    Paragraph(
                        f"{r['result_margin_pct']}%" if r["result_margin_pct"] else "n/a",
                        st["cellr"],
                    ),
                ]
            )
        out.append(_table(rows, [64 * mm, 34 * mm, 22 * mm, 22 * mm, W - 142 * mm]))
        period = f"{seg['period_end']} ({seg['period_type'].lower()}, {str(seg['basis']).lower()})"
        out.append(Paragraph(clean(f"Period: {period}. {seg['note']}"), st["small"]))
        pts = [(r["segment"], Decimal(r["share_pct"]), None) for r in seg["rows"] if r["share_pct"]]
        if len(pts) >= 2:
            out += [
                Spacer(1, 4),
                _hbar("Share of reported segment revenue (percent)", pts[:8], label_w=150),
            ]
    else:
        gaps.append("segment revenue (" + seg["reason"] + ")")

    narr = pf["narrative"]
    if narr.get("available") and narr.get("leaders"):
        out.append(Paragraph("Leadership named in the annual report", st["h2"]))
        rows = [_hdr(st, "Name", "Title as printed", "Mentions", "Pages", right_from=2)]
        for p in narr["leaders"]:
            titles = "; ".join(t["title"] for t in p["titles"])
            pages = ", ".join(str(x) for x in p["titles"][0]["pages"])
            rows.append(
                [
                    Paragraph(clean(p["name"]), st["cellb"]),
                    Paragraph(clean(titles), st["cell"]),
                    Paragraph(str(p["mentions"]), st["cellr"]),
                    Paragraph(pages, st["cellr"]),
                ]
            )
        out.append(_table(rows, [48 * mm, W - 48 * mm - 44 * mm, 18 * mm, 26 * mm]))
        out.append(
            Paragraph(
                "Names are those printed beside a leadership title at least twice in the latest annual "
                "report; a title such as Chairman beside a name can be that of a board committee. "
                "Experience, tenure and past roles are not extracted and no biography is added.",
                st["small"],
            )
        )
    else:
        gaps.append(
            "leadership named in an annual report"
            if narr.get("available")
            else "leadership (" + narr["reason"] + ")"
        )

    ext = pf.get("external")
    out += _external_block(ext, st)
    if not (ext and ext.get("available")):
        gaps.append("history and leadership from the company's own pages (none read yet)")

    if narr.get("available"):
        d = narr["document"]
        out.append(
            Paragraph(
                clean(
                    f"Source of the statements below: {d['title']} ({d['pages_read']} pages read). "
                    f"{narr['method']}."
                ),
                st["small"],
            )
        )
        for t in narr["topics"]:
            block = [Paragraph(clean(t["title"]), st["h2"])]
            if t["items"]:
                block.append(_quote_table(t["items"], st))
            else:
                block.append(
                    Paragraph("No matching statement was found in the stored report.", st["body"])
                )
            out.append(KeepTogether(block))
    else:
        gaps.append("history, strategic changes and exposure statements (" + narr["reason"] + ")")

    g = pf["group"]
    if g.get("available"):
        out.append(Paragraph("Group structure", st["h2"]))
        counts = ", ".join(f"{n} {k.lower()}" for k, n in g["by_relationship"].items())
        out.append(
            Paragraph(clean(f"Entities listed in the report: {g['total']} ({counts})."), st["body"])
        )
        rows = [_hdr(st, "Entity", "Relationship", "Page", right_from=2)] + [
            [
                Paragraph(clean(e["name"]), st["cell"]),
                Paragraph(clean(e["relationship"]), st["cell"]),
                Paragraph(str(e["page"]), st["cellr"]),
            ]
            for e in g["entities"][:12]
        ]
        out.append(_table(rows, [W - 62 * mm, 44 * mm, 18 * mm]))
    else:
        gaps.append("group structure (" + g["reason"] + ")")
    note = (("Not available for this company: " + "; ".join(gaps) + ". ") if gaps else "") + pf[
        "not_available"
    ]
    out.append(Paragraph(clean(note), st["small"]))
    return out


# ---------------------------------------------------------------- growth comparison


def growth_section(report: dict, st) -> list:
    a = _analysis(report)
    g = a.get("growth") if a else None
    if not g:
        return []
    out = [
        Paragraph(
            clean(
                "The benchmark is the company's stored peer group (same-industry listed companies "
                "closest in size), because EquityOS holds no industry-wide statistics. It is called the "
                "peer-set CAGR below."
            ),
            st["body"],
        )
    ]
    heads = (
        "Measure",
        "Years",
        "Company CAGR",
        "Peer median",
        "Peer combined",
        "Growth alpha vs median",
        "vs combined",
    )
    rows = [_hdr(st, *heads)]
    notes = []
    for label, blk in ((g["revenue_label"], g["revenue"]), ("Net profit", g["profit"])):
        if not blk.get("available"):
            rows.append(
                [
                    Paragraph(clean(label), st["cellb"]),
                    Paragraph("not available", st["cell"]),
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
            notes.append(f"{label}: {blk.get('reason', '')}.")
            continue
        c = blk["company"]
        span = f"{c['start'][:4]} to {c['end'][:4]} ({c['years']} years, {str(c['basis']).lower()})"
        if blk.get("peer_benchmark"):
            vs_c = blk["difference_vs_aggregate_pp"]
            rows.append(
                [
                    Paragraph(clean(label), st["cellb"]),
                    Paragraph(clean(span), st["cell"]),
                    Paragraph(f"{c['cagr']}%", st["cellr"]),
                    Paragraph(f"{blk['peer_median_cagr']}%", st["cellr"]),
                    Paragraph(
                        f"{blk['peer_aggregate_cagr']}%" if blk["peer_aggregate_cagr"] else "n/a",
                        st["cellr"],
                    ),
                    Paragraph(f"{_pp(blk['difference_vs_median_pp'])} pp", st["cellr"]),
                    Paragraph(f"{_pp(vs_c)} pp" if vs_c else "n/a", st["cellr"]),
                ]
            )
        else:
            rows.append(
                [
                    Paragraph(clean(label), st["cellb"]),
                    Paragraph(clean(span), st["cell"]),
                    Paragraph(f"{c['cagr']}%", st["cellr"]),
                ]
                + [Paragraph("n/a", st["cellr"])] * 4
            )
            notes.append(f"{label}: no peer comparison, because {blk['peer_reason']}.")
    out.append(_table(rows, [24 * mm, 44 * mm, 20 * mm, 20 * mm, 22 * mm, 24 * mm, W - 154 * mm]))
    out += [Paragraph(clean(n), st["small"]) for n in notes]
    for label, blk in ((g["revenue_label"], g["revenue"]), ("Net profit", g["profit"])):
        if blk.get("available") and blk.get("peer_benchmark"):
            c = blk["company"]
            pts = [("This company", Decimal(c["cagr"]), True)] + [
                (p["name"], Decimal(p["cagr"]), False) for p in blk["peers"]
            ]
            pts.sort(key=lambda t: t[1], reverse=True)
            out += [
                Spacer(1, 6),
                _hbar(f"{label} CAGR, {c['start'][:4]} to {c['end'][:4]} (percent)", pts),
            ]
            drows = [_hdr(st, "Company", "CAGR", "Evidence")]
            drows.append(
                [
                    Paragraph("This company", st["cellb"]),
                    Paragraph(f"{c['cagr']}%", st["cellr"]),
                    Paragraph(_ids(c["evidence_ids"]), st["small"]),
                ]
            )
            for p in blk["peers"]:
                drows.append(
                    [
                        Paragraph(clean(p["name"]), st["cell"]),
                        Paragraph(f"{p['cagr']}%", st["cellr"]),
                        Paragraph(_ids(p["evidence_ids"]), st["small"]),
                    ]
                )
            out.append(
                KeepTogether(
                    [
                        Paragraph(clean(f"{label}: peer detail"), st["h2"]),
                        _table(drows, [70 * mm, 30 * mm, W - 100 * mm]),
                    ]
                )
            )
    out.append(Paragraph("Relative price return (defined separately)", st["h2"]))
    if g["price_return"]:
        rows = [_hdr(st, "Period", "Company", "Peer-set median", "Difference", "As of")]
        for r in g["price_return"]:
            rows.append(
                [
                    Paragraph(clean(r["period"]), st["cell"]),
                    Paragraph(f"{Decimal(r['company']).quantize(Decimal('0.1'))}%", st["cellr"]),
                    Paragraph(
                        f"{Decimal(r['peer_median']).quantize(Decimal('0.1'))}%", st["cellr"]
                    ),
                    Paragraph(f"{_pp(r['difference_pp'])} pp", st["cellr"]),
                    Paragraph(clean(r["as_of"] or ""), st["cellr"]),
                ]
            )
        out.append(_table(rows, [30 * mm, 36 * mm, 40 * mm, 34 * mm, W - 140 * mm]))
    else:
        out.append(
            Paragraph(
                "No stored price-return evidence for the company and its peer set.", st["body"]
            )
        )
    out.append(Paragraph("Definitions, formulas and assumptions", st["h2"]))
    out += _bullets(g["definitions"], st)
    out.append(Paragraph("Limitations", st["h2"]))
    out += _bullets(g["limitations"], st)
    return out


# ---------------------------------------------------------------- cyclicality


def cyclicality_section(report: dict, st) -> list:
    a = _analysis(report)
    c = a.get("cyclicality") if a else None
    if not c:
        return []
    sec, obs = c["sector"], c["observed"]
    out = [
        Paragraph("Is this business cyclical, defensive or stable?", st["h2"]),
        Paragraph(
            clean(f"Sector characterisation: {sec['classification']}. {sec['text']} {sec['note']}"),
            st["body"],
        ),
    ]
    if obs["volatility_class"] in ("low", "moderate", "high"):
        extra = (
            f"Profit moved {obs['profit_to_revenue_volatility']} times as much as revenue. "
            if obs.get("profit_to_revenue_volatility")
            else ""
        )
        out.append(
            Paragraph(
                clean(
                    f"Observed in this company's stored history: net-profit growth volatility is "
                    f"{obs['volatility_class']}, {obs['versus_sector'] or 'compared with'} the sector "
                    f"characterisation. {extra}Thresholds: {obs['thresholds']}."
                ),
                st["body"],
            )
        )
    else:
        out.append(
            Paragraph(
                "Observed volatility cannot be measured: too few annual changes are stored.",
                st["body"],
            )
        )
    series = [s for s in (obs["revenue"], obs["profit"]) if s]
    if series:
        out.append(Paragraph("Historical volatility", st["h2"]))
        ys = sorted({y for s in series for y in s["years"]})
        rows = [_hdr(st, "Year-on-year growth", *ys, "Volatility (pp)", "Range (pp)", "Down years")]
        for s in series:
            by = dict(zip(s["years"], s["growth"], strict=False))
            rows.append(
                [Paragraph(clean(s["label"]), st["cellb"])]
                + [Paragraph(f"{by[y]}%" if y in by else "-", st["cellr"]) for y in ys]
                + [
                    Paragraph(f"{s['volatility_pp'] or 'n/a'}", st["cellr"]),
                    Paragraph(s["range_pp"], st["cellr"]),
                    Paragraph(str(s["down_years"]), st["cellr"]),
                ]
            )
        n = len(ys)
        out.append(
            _table(
                rows, [38 * mm] + [(W - 38 * mm - 56 * mm) / n] * n + [20 * mm, 18 * mm, 18 * mm]
            )
        )
        out.append(
            Paragraph(
                "Volatility is the population standard deviation of the year-on-year growth rates shown, in percentage points.",
                st["small"],
            )
        )
        out.append(Paragraph("Downturn performance and recovery", st["h2"]))
        recs = [(s["label"], r) for s in series for r in s["recoveries"]]
        if recs:
            rows = [
                _hdr(
                    st,
                    "Measure",
                    "Fall in",
                    "Change",
                    "Next year",
                    "Change next year",
                    right_from=2,
                )
            ]
            for label, r in recs:
                rows.append(
                    [
                        Paragraph(clean(label), st["cell"]),
                        Paragraph(r["fall_fy"], st["cell"]),
                        Paragraph(f"{r['fall_pct']}%", st["cellr"]),
                        Paragraph(r["next_fy"] or "not yet reported", st["cell"]),
                        Paragraph(f"{r['next_pct']}%" if r["next_pct"] else "n/a", st["cellr"]),
                    ]
                )
            out.append(_table(rows, [38 * mm, 24 * mm, 24 * mm, 36 * mm, W - 122 * mm]))
        else:
            out.append(
                Paragraph(
                    "No annual decline in revenue or profit appears in the stored years, so no downturn or recovery can be shown.",
                    st["body"],
                )
            )
        p = obs["profit"]
        if p and p.get("max_peak_to_trough_pct") is not None:
            out.append(
                Paragraph(
                    clean(
                        f"Largest peak-to-trough fall in annual net profit over the stored years: {p['max_peak_to_trough_pct']}%."
                    ),
                    st["small"],
                )
            )
    if c.get("structural"):
        out.append(Paragraph("Structural versus cyclical growth", st["h2"]))
        out.append(Paragraph(clean(c["structural"]["text"]), st["body"]))
    out.append(Paragraph("Sensitivity to the economy (sector template)", st["h2"]))
    rows = [_hdr(st, "Factor", "Typical relevance", "Mechanism", right_from=9)]
    for s in c["sensitivities"]:
        rows.append(
            [
                Paragraph(clean(s["factor"]), st["cellb"]),
                Paragraph(clean(s["relevance"]), st["cell"]),
                Paragraph(clean(s["mechanism"]), st["cell"]),
            ]
        )
    out.append(_table(rows, [38 * mm, 26 * mm, W - 64 * mm]))
    out.append(Paragraph(clean(c["sensitivity_note"]), st["small"]))
    out.append(Paragraph("Indicators that could signal a downturn or recovery", st["h2"]))
    out += _bullets(c["indicators"], st)
    if c.get("monitored"):
        rows = [_hdr(st, "Stored indicator", "Latest", "As of")] + [
            [
                Paragraph(clean(m["label"]), st["cell"]),
                Paragraph(fv(m["value"], m["unit"]), st["cellr"]),
                Paragraph(clean(m["period_end"]), st["cellr"]),
            ]
            for m in c["monitored"]
        ]
        out.append(_table(rows, [70 * mm, 40 * mm, W - 110 * mm]))
    out.append(Paragraph("Limitations of this analysis", st["h2"]))
    out += _bullets(c["limits"], st)
    return out


# ---------------------------------------------------------------- appendix


def data_quality_section(report: dict, st) -> list:
    """How statements are classified (this replaces the old inline labels)."""
    counts: dict[str, int] = {}
    seen = set()
    for sec in report["sections"]:
        for it in sec.get("items", []):
            src = it.get("source") or {}
            key = (it["requirement"], src.get("evidence_id"))
            if key in seen:
                continue
            seen.add(key)
            kind = (
                "Calculated by EquityOS"
                if src.get("provider") == "EVIDENCE_ENGINE"
                else "Reported (company document or market feed)"
            )
            counts[kind] = counts.get(kind, 0) + 1
    out = [
        Paragraph("Appendix E - Data quality and how statements are classified", st["h1"]),
        Paragraph(
            "Evidence labels are not repeated beside each figure. A figure read from a company document "
            "or the market feed cites its page and evidence number. A figure calculated by EquityOS says "
            "&quot;calculated&quot; in its source note and is built from stored figures by the stated "
            "formula. Rule-based readings (observations, thesis points and the verdict) follow the "
            "rules printed in the methodology. Hypotheses are written as hypotheses in the sentence. "
            "Management statements are in quotation marks with their page. Industry sections and sector "
            "risks are sector templates, not evidence about the company, and say so in their own headings.",
            st["body"],
        ),
    ]
    if counts:
        rows = [_hdr(st, "Figure type", "Count")] + [
            [Paragraph(clean(k), st["cell"]), Paragraph(str(v), st["cellr"])]
            for k, v in counts.items()
        ]
        out.append(_table(rows, [100 * mm, 30 * mm]))
    return out


def growth_has_content(growth: dict | None) -> bool:
    """True if the growth section can show at least one real company figure or price comparison."""
    if not growth:
        return False
    for key in ("revenue", "profit"):
        block = growth.get(key) or {}
        if block.get("available") and (block.get("company") or {}).get("cagr") is not None:
            return True
    return bool(growth.get("price_return"))


def cyclicality_has_content(cyc: dict | None) -> bool:
    """True if there is a sector characterisation or any observed history to report."""
    if not cyc:
        return False
    obs = cyc.get("observed") or {}
    return bool(
        (cyc.get("sector") or {}).get("classification")
        or obs.get("revenue")
        or obs.get("profit")
        or cyc.get("sensitivities")
    )
