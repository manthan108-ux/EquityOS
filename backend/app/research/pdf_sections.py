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
from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph, Spacer, Table, TableStyle

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
                "The first benchmark is the company's stored peer group (same-industry listed "
                "companies closest in size), called the peer-group CAGR; it is not an industry CAGR. A second, broader comparison "
                "uses the companies the data provider files under the same sector label, shown "
                "below when enough of them have stored filings. EquityOS has no official industry "
                "statistics."
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
        "Difference vs peer median",
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
    ig = g.get("industry_group") or {}
    out.append(CondPageBreak(95 * mm))  # heading, intro and table stay together
    out.append(Paragraph("Industry-group comparison (provider sector label)", st["h2"]))
    if ig.get("available"):
        out.append(
            Paragraph(
                clean(
                    f"Group: {ig['label']} ({ig['group_companies']} listed companies carry this "
                    f"label; classification read {ig['classification_source']}). The figures below "
                    "use only the other companies in the group whose stored annual filings cover "
                    "the same two fiscal years on the same accounting basis as this company, and "
                    "the combined value of exactly those companies at both ends. They describe "
                    "those companies, not the whole industry."
                    + (
                        f" The provider reports the group's combined market capitalisation as "
                        f"{Decimal(ig['group_market_cap_crore']):,.0f} crore (not used in any "
                        "calculation)."
                        if ig.get("group_market_cap_crore")
                        else ""
                    )
                ),
                st["small"],
            )
        )
        rows = [
            _hdr(
                st,
                "Measure",
                "Years",
                "Company CAGR",
                "Group median",
                "Group combined",
                "Alpha vs median",
                "vs combined",
                "Companies",
            )
        ]
        gaps = []
        for key, label in (
            ("revenue", ig["metrics"].get("revenue", {}).get("label") or "Revenue"),
            ("net_profit", "Net profit"),
        ):
            blk = ig["metrics"].get(key) or {}
            if not blk.get("available"):
                gaps.append(f"{label}: {blk.get('reason', 'not available')}.")
                continue
            c = blk["company"]
            span = f"{c['start'][:4]} to {c['end'][:4]} ({c['years']} years, {str(c['basis']).lower()})"
            vs = blk["alpha_vs_aggregate_pp"]
            rows.append(
                [
                    Paragraph(clean(label), st["cellb"]),
                    Paragraph(clean(span), st["cell"]),
                    Paragraph(f"{c['cagr']}%", st["cellr"]),
                    Paragraph(f"{blk['median_cagr']}%", st["cellr"]),
                    Paragraph(
                        f"{blk['aggregate_cagr']}%" if blk["aggregate_cagr"] else "n/a", st["cellr"]
                    ),
                    Paragraph(f"{_pp(blk['alpha_vs_median_pp'])} pp", st["cellr"]),
                    Paragraph(f"{_pp(vs)} pp" if vs else "n/a", st["cellr"]),
                    Paragraph(f"{blk['members_used']} of {blk['group_size'] - 1}", st["cellr"]),
                ]
            )
        if len(rows) > 1:
            out.append(
                _table(
                    rows,
                    [20 * mm, 40 * mm, 18 * mm, 18 * mm, 20 * mm, 20 * mm, 18 * mm, W - 154 * mm],
                )
            )
        out += [Paragraph(clean(n), st["small"]) for n in gaps]
        for key in ("revenue", "net_profit"):
            blk = ig["metrics"].get(key) or {}
            if blk.get("available"):
                mrows = [_hdr(st, "Company in the group", "CAGR", "Evidence")]
                for m in blk["members"]:
                    mrows.append(
                        [
                            Paragraph(clean(m["name"]), st["cell"]),
                            Paragraph(f"{m['cagr']}%", st["cellr"]),
                            Paragraph(_ids(m["evidence_ids"]), st["small"]),
                        ]
                    )
                out.append(
                    KeepTogether(
                        [
                            Paragraph(clean(f"{blk['label']}: group members used"), st["h2"]),
                            _table(mrows, [70 * mm, 30 * mm, W - 100 * mm]),
                        ]
                    )
                )
    else:
        out.append(
            Paragraph(
                clean(
                    "Not available: "
                    + str(ig.get("reason") or "no industry label has been collected")
                    + "."
                    + (f" Provider label: {ig['label']}." if ig.get("label") else "")
                ),
                st["small"],
            )
        )
    out.append(Paragraph("Relative price return (defined separately)", st["h2"]))
    if g["price_return"]:
        rows = [_hdr(st, "Period", "Company", "Peer-group median", "Difference", "As of")]
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


# ---------------------------------------------------------------- growth and profitability


def profitability_has_content(p: dict | None) -> bool:
    """True if at least one growth or margin figure is available."""
    return bool(p and p.get("available"))


def _g(x, nd: str = "0.1") -> str:
    return str(Decimal(x).quantize(Decimal(nd)))


def profitability_section(report: dict, st) -> list:
    a = _analysis(report)
    p = a.get("profitability") if a else None
    if not profitability_has_content(p):
        return []
    bench = p["benchmark"]
    out = [
        Paragraph(
            clean(
                f"All figures are on the {p['basis'].lower()} basis. Comparisons are with the "
                f"{bench['label']}: {bench['description']}. {bench['industry_reason'].capitalize()}."
            ),
            st["body"],
        )
    ]
    rows = [
        _hdr(st, "Measure", "Years", "Latest year-on-year", "CAGR", "3-year CAGR", right_from=1)
    ]
    gaps = []
    for g in p["growth"].values():
        if not g.get("available") or not g.get("series"):
            gaps.append(f"{g['label']}: {g['reason']}.")
            continue
        last = g["series"][-1]
        cg, c3 = g.get("cagr"), g.get("cagr_3y")
        yoy = last.get("yoy_pct")
        rows.append(
            [
                Paragraph(clean(g["label"]), st["cellb"]),
                Paragraph(f"{g['series'][0]['fy']} to {last['fy']}", st["cellr"]),
                Paragraph(f"{_g(yoy)}%" if yoy is not None else "n/a", st["cellr"]),
                Paragraph(f"{_g(cg['pct'])}%" if cg else "n/a", st["cellr"]),
                Paragraph(f"{_g(c3['pct'])}%" if c3 else "n/a", st["cellr"]),
            ]
        )
        if not cg and g.get("reason"):
            gaps.append(f"{g['label']}: {g['reason']}.")
    if len(rows) > 1:
        out += [
            Paragraph("Growth", st["h2"]),
            _table(rows, [W - 120 * mm, 34 * mm, 30 * mm, 28 * mm, 28 * mm]),
        ]
    mrows = [_hdr(st, "Margin", "Latest", "Change", "Direction", "Years up / down", right_from=1)]
    for m in p["margins"].values():
        if not m.get("available") or not m.get("series"):
            gaps.append(f"{m['label']}: {m['reason']}.")
            continue
        latest = m["series"][-1]
        mrows.append(
            [
                Paragraph(clean(m["label"]), st["cellb"]),
                Paragraph(f"{_g(latest['value'])}% ({latest['fy']})", st["cellr"]),
                Paragraph(f"{_pp(m['change_pp'])} pp" if "change_pp" in m else "n/a", st["cellr"]),
                Paragraph((m.get("direction") or "n/a").lower(), st["cellr"]),
                Paragraph(
                    f"{m['years_up']} / {m['years_down']}" if "years_up" in m else "n/a",
                    st["cellr"],
                ),
            ]
        )
    if len(mrows) > 1:
        out += [
            Spacer(1, 6),
            Paragraph("Margins", st["h2"]),
            _table(mrows, [W - 120 * mm, 40 * mm, 24 * mm, 28 * mm, 28 * mm]),
        ]
    out += [Paragraph(clean(o["text"]), st["body"]) for o in p["observations"]]
    d = p.get("drivers")
    if d and d.get("available"):
        drows = [
            _hdr(
                st,
                "Line (share of revenue)",
                d["start_fy"],
                d["end_fy"],
                "Effect on margin",
                right_from=1,
            )
        ]
        for i in d["items"]:
            drows.append(
                [
                    Paragraph(clean(i["line"]), st["cell"]),
                    Paragraph(f"{_g(i['pct_of_revenue_start'])}%", st["cellr"]),
                    Paragraph(f"{_g(i['pct_of_revenue_end'])}%", st["cellr"]),
                    Paragraph(f"{_pp(i['contribution_pp'])} pp", st["cellr"]),
                ]
            )
        drows.append(
            [
                Paragraph("Not itemised", st["cell"]),
                "",
                "",
                Paragraph(f"{_pp(d['not_itemised_pp'])} pp", st["cellr"]),
            ]
        )
        out += [
            Spacer(1, 6),
            Paragraph(
                f"What moved the net margin ({_g(d['net_margin_start'])}% to {_g(d['net_margin_end'])}%)",
                st["h2"],
            ),
            _table(drows, [W - 90 * mm, 30 * mm, 30 * mm, 30 * mm]),
            Paragraph(clean(d["note"]), st["small"]),
        ]
    prows = [
        _hdr(
            st,
            "Versus the peer group",
            "Company",
            "Peer-group median",
            "Difference",
            "Peers",
            right_from=1,
        )
    ]
    for key, b in p["peer_growth"].items():
        if b.get("peer_benchmark"):
            c = b["company"]
            prows.append(
                [
                    Paragraph(clean(p["growth"][key]["label"] + " CAGR"), st["cell"]),
                    Paragraph(f"{_g(c['cagr'])}%", st["cellr"]),
                    Paragraph(f"{_g(b['peer_group_median_cagr'])}%", st["cellr"]),
                    Paragraph(f"{_pp(b['difference_vs_peer_median_pp'])} pp", st["cellr"]),
                    Paragraph(str(b["peer_count"]), st["cellr"]),
                ]
            )
        elif b.get("reason"):
            gaps.append(
                f"Peer comparison, {p['growth'][key]['label'].lower()} CAGR: {b['reason']}."
            )
    for key, b in p["peer_margins"].items():
        if b.get("available"):
            prows.append(
                [
                    Paragraph(clean(p["margins"][key]["label"] + f" ({b['fy']})"), st["cell"]),
                    Paragraph(f"{_g(b['company'])}%", st["cellr"]),
                    Paragraph(f"{_g(b['peer_group_median'])}%", st["cellr"]),
                    Paragraph(f"{_pp(b['difference_pp'])} pp", st["cellr"]),
                    Paragraph(str(b["peer_count"]), st["cellr"]),
                ]
            )
        elif b.get("reason"):
            gaps.append(f"Peer comparison, {p['margins'][key]['label'].lower()}: {b['reason']}.")
    if len(prows) > 1:
        out += [Spacer(1, 6), _table(prows, [W - 120 * mm, 26 * mm, 36 * mm, 30 * mm, 28 * mm])]
    if gaps:
        out += [Spacer(1, 4), Paragraph("Not available", st["h2"])]
        out += _bullets(gaps, st)
    out += [Spacer(1, 4)] + _bullets(p["definitions"], st)
    return out


# ---------------------------------------------------------------- cash flow and earnings quality


def cashflow_has_content(c: dict | None) -> bool:
    return bool(c and c.get("available"))


def _crore(x) -> str:
    return f"{(Decimal(x) / Decimal(10_000_000)):,.0f}"


def _ratio_cell(m: dict, last: bool = True) -> tuple[str, str]:
    row = m["series"][-1]
    v = Decimal(row["value"])
    return (f"{v}x" if m["kind"] == "x" else f"{v}%"), row["fy"]


def cashflow_section(report: dict, st) -> list:
    a = _analysis(report)
    c = a.get("cashflow") if a else None
    if not cashflow_has_content(c):
        return []
    gaps: list[str] = []
    out = [
        Paragraph(
            clean(
                f"All figures are on the {c['basis'].lower()} basis, in INR crore. Comparisons are with "
                f"the {c['benchmark']['label']}: {c['benchmark']['description']}. Readings are rules "
                "of thumb with stated thresholds; they describe the evidence and are not conclusions "
                "about the company's accounting."
            ),
            st["body"],
        )
    ]
    # cash and profit by year
    conv = c["conversion"]
    fcf = c["free_cash_flow"]
    capex = c["capital_expenditure"]
    fcf_by = {r["fy"]: r for r in fcf.get("series", [])} if fcf["available"] else {}
    capex_by = {r["fy"]: r for r in capex.get("series", [])} if capex["available"] else {}
    cfo_by = {r["fy"]: r for r in c["operating_cash_flow"].get("series", [])}
    pat_by = {r["fy"]: r for r in conv.get("years", [])}
    years = sorted(set(cfo_by) | set(fcf_by) | set(capex_by) | set(pat_by))
    if years:
        rows = [
            _hdr(
                st,
                "INR crore",
                "Net profit",
                "Operating cash flow",
                "CFO / PAT",
                "Capital expenditure",
                "Free cash flow",
                right_from=1,
            )
        ]
        for fy in years:
            p, o = pat_by.get(fy), cfo_by.get(fy)
            rows.append(
                [
                    Paragraph(fy, st["cellb"]),
                    Paragraph(_crore(p["pat"]) if p else "n/a", st["cellr"]),
                    Paragraph(_crore(o["value"]) if o else "n/a", st["cellr"]),
                    Paragraph(f"{p['ratio']}x" if p and p.get("ratio") else "n/a", st["cellr"]),
                    Paragraph(
                        _crore(capex_by[fy]["value"]) if fy in capex_by else "n/a", st["cellr"]
                    ),
                    Paragraph(_crore(fcf_by[fy]["value"]) if fy in fcf_by else "n/a", st["cellr"]),
                ]
            )
        out += [
            Paragraph("Profit and cash by year", st["h2"]),
            _table(rows, [W - 130 * mm, 26 * mm, 28 * mm, 24 * mm, 26 * mm, 26 * mm]),
        ]
    cfo = c["operating_cash_flow"]
    if cfo.get("cagr"):
        out.append(
            Paragraph(
                clean(
                    f"Operating cash flow grew at {_g(cfo['cagr']['pct'])}% a year ({cfo['cagr']['start_fy']} to {cfo['cagr']['end_fy']})."
                ),
                st["body"],
            )
        )
    elif not cfo["available"]:
        gaps.append(f"Operating cash flow: {cfo['reason']}.")
    if not fcf["available"]:
        gaps.append(f"Free cash flow: {fcf['reason']}.")
    if not capex["available"]:
        gaps.append(f"Capital expenditure: {capex['reason']}.")
    # ratios
    rrows = [_hdr(st, "Ratio", "Latest", "Year", right_from=1)]
    for key in (
        "cfo_to_net_profit",
        "cfo_to_ebitda",
        "cfo_margin",
        "fcf_margin",
        "fcf_to_net_profit",
        "capex_to_revenue",
        "capex_to_cfo",
        "capex_to_depreciation",
        "accrual_ratio",
    ):
        m = c["ratios"][key]
        if not m.get("available"):
            gaps.append(f"{m['label']}: {m['reason']}.")
            continue
        value, fy = _ratio_cell(m)
        rrows.append(
            [
                Paragraph(clean(m["label"]), st["cell"]),
                Paragraph(value, st["cellr"]),
                Paragraph(fy, st["cellr"]),
            ]
        )
    if len(rrows) > 1:
        out += [
            Spacer(1, 6),
            Paragraph("Cash conversion, capital expenditure and accruals", st["h2"]),
            _table(rrows, [W - 60 * mm, 30 * mm, 30 * mm]),
        ]
    out += [Paragraph(clean(o["text"]), st["body"]) for o in c["observations"]]
    if c.get("other_basis"):
        out.append(_note_box(c["other_basis"]["note"], st))
    if c["divergences"]:
        out += [Spacer(1, 4), Paragraph("Where profit and cash moved apart", st["h2"])]
        out += _bullets([d["text"] for d in c["divergences"]], st)
    elif conv.get("available"):
        out.append(
            Paragraph(
                "No year shows profit and cash moving apart under the rules used.", st["body"]
            )
        )
    wc = c.get("working_capital")
    if wc:
        wrows = [_hdr(st, f"Cash flow statement lines, {wc['fy']}", "INR crore", right_from=1)]
        wrows += [
            [Paragraph(clean(i["line"]), st["cell"]), Paragraph(_crore(i["value"]), st["cellr"])]
            for i in wc["items"]
        ]
        out += [
            Spacer(1, 4),
            _table(wrows, [W - 40 * mm, 40 * mm]),
            Paragraph(clean(wc["note"]), st["small"]),
        ]
    prows = [
        _hdr(
            st,
            "Versus the peer group",
            "Company",
            "Peer-group median",
            "Difference",
            "Peers",
            right_from=1,
        )
    ]
    for key, b in c["peers"].items():
        label = c["ratios"][key]["label"]
        if b.get("available"):
            unit = "x" if b["kind"] == "x" else "%"
            diff = f"{_pp(b['difference_pp'])} pp" if unit == "%" else f"{_pp(b['difference_pp'])}x"
            prows.append(
                [
                    Paragraph(clean(f"{label} ({b['fy']})"), st["cell"]),
                    Paragraph(f"{_g(b['company'], '0.01')}{unit}", st["cellr"]),
                    Paragraph(f"{_g(b['peer_group_median'], '0.01')}{unit}", st["cellr"]),
                    Paragraph(diff, st["cellr"]),
                    Paragraph(str(b["peer_count"]), st["cellr"]),
                ]
            )
        elif b.get("reason"):
            gaps.append(f"Peer comparison, {label.lower()}: {b['reason']}.")
    if len(prows) > 1:
        out += [Spacer(1, 6), _table(prows, [W - 120 * mm, 26 * mm, 36 * mm, 30 * mm, 28 * mm])]
    if gaps:
        out += [Spacer(1, 4), Paragraph("Not available", st["h2"])]
        out += _bullets(gaps, st)
    out += [Spacer(1, 4)] + _bullets(c["definitions"], st)
    return out


# ---------------------------------------------------------------- working capital and financial health


def health_has_content(h: dict | None) -> bool:
    return bool(h and h.get("available"))


def _val(m: dict, row: dict) -> str:
    v = Decimal(row["value"])
    if m["kind"] == "days":
        return f"{v.quantize(Decimal('1'))}"
    return f"{v}x" if m["kind"] == "x" else f"{v}%"


def _series_table(st, metrics: dict, keys, first_col: str, gaps: list) -> list:
    """A year-by-year table of the available ratio series (one row per metric)."""
    avail = [(k, metrics[k]) for k in keys if metrics[k].get("available")]
    for k in keys:
        if not metrics[k].get("available"):
            gaps.append(f"{metrics[k]['label']}: {metrics[k]['reason']}.")
    if not avail:
        return []
    fys = sorted({r["fy"] for _, m in avail for r in m["series"]})[-5:]
    rows = [_hdr(st, first_col, *fys, right_from=1)]
    for _, m in avail:
        by = {r["fy"]: r for r in m["series"]}
        rows.append(
            [Paragraph(clean(m["label"]), st["cell"])]
            + [Paragraph(_val(m, by[f]) if f in by else "n/a", st["cellr"]) for f in fys]
        )
    n = len(fys)
    return [_table(rows, [W - 22 * mm * n] + [22 * mm] * n)]


def health_section(report: dict, st) -> list:
    a = _analysis(report)
    h = a.get("health") if a else None
    if not health_has_content(h):
        return []
    gaps: list[str] = []
    out = [
        Paragraph(
            clean(
                f"All figures are on the {h['basis'].lower()} basis. Comparisons are with the "
                f"{h['benchmark']['label']}: {h['benchmark']['description']}. Readings are rules of "
                "thumb with stated thresholds; they describe the evidence and are not conclusions "
                "about the company."
            ),
            st["body"],
        )
    ]
    if h["kind"] == "NON_FINANCIAL":
        wc = h["working_capital"]
        if wc["available"]:
            out += [Paragraph("Working capital (days)", st["h2"])]
            out += _series_table(
                st,
                wc["days"],
                list(wc["days"]),
                "Days",
                gaps,
            )
            out += [Paragraph(clean(o["text"]), st["body"]) for o in wc["observations"]]
            out.append(Paragraph(clean(wc["convention"]), st["small"]))
        else:
            gaps.append(f"Working capital: {wc['reason']}.")
        lev = h["leverage_liquidity"]
        if lev["available"]:
            out += [Spacer(1, 6), Paragraph("Leverage, liquidity and coverage", st["h2"])]
            keys = [
                "debt_to_equity", "net_debt_to_equity", "net_debt_to_ebitda", "interest_cover",
                "ebitda_interest_cover", "current_ratio", "quick_ratio",
            ]  # fmt: skip
            out += _series_table(st, lev["ratios"], keys, "Ratio", gaps)
            nd = lev["net_debt"]
            if nd.get("available"):
                last = nd["series"][-1]
                word = "net cash" if last["position"] == "NET_CASH" else "net debt"
                out.append(
                    Paragraph(
                        clean(
                            f"{last['fy']} {word}: INR {_crore(abs(Decimal(last['value'])))} crore."
                        ),
                        st["body"],
                    )
                )
            out += [Paragraph(clean(o["text"]), st["body"]) for o in lev["observations"]]
        else:
            gaps.append(f"Leverage and liquidity: {lev['reason']}.")
        rep = h["repayment"]
        if rep["available"]:
            out += [Spacer(1, 6), Paragraph("Repayment capacity", st["h2"])]
            out += [Paragraph(clean(o["text"]), st["body"]) for o in rep["observations"]]
            out.append(Paragraph(clean(rep["rules"]), st["small"]))
        else:
            gaps.append(f"Repayment capacity: {rep['reason']}.")
    else:
        out.append(_note_box(h["note"], st))
    ret = h["returns"]
    if ret["available"]:
        out += [Spacer(1, 6), Paragraph("Returns", st["h2"])]
        keys = ["roe", "roe_closing", "roce", "roa", "roa_closing"]
        out += _series_table(st, ret["metrics"], [k for k in keys if h["kind"] == "NON_FINANCIAL" or k != "roce"], "Return", gaps)  # fmt: skip
    dp = h["dupont"]
    if dp["available"]:
        out += [Spacer(1, 6), Paragraph("DuPont analysis of return on equity", st["h2"])]
        rows = [
            _hdr(
                st, "Year", "Net margin", "Asset turnover", "Equity multiplier", "ROE", right_from=1
            )
        ]
        for r in dp["years"]:
            rows.append(
                [
                    Paragraph(r["fy"], st["cellb"]),
                    Paragraph(f"{_g(r['net_margin'], '0.01')}%", st["cellr"]),
                    Paragraph(f"{_g(r['asset_turnover'], '0.01')}x", st["cellr"]),
                    Paragraph(f"{_g(r['equity_multiplier'], '0.01')}x", st["cellr"]),
                    Paragraph(f"{_g(r['roe'], '0.1')}%", st["cellr"]),
                ]
            )
        out.append(_table(rows, [W - 120 * mm, 28 * mm, 30 * mm, 32 * mm, 30 * mm]))
        d = dp["driver"]
        if d:
            shares = ", ".join(f"{k.lower()} {v}%" for k, v in d["shares_pct"].items())
            out.append(
                Paragraph(
                    clean(
                        f"ROE changed by {_pp(d['roe_change_pp'])} percentage points between "
                        f"{d['from_fy']} and {d['to_fy']}; {d['leading'].lower()} was the largest "
                        f"driver (shares of the change: {shares})."
                    ),
                    st["body"],
                )
            )
        five = [r for r in dp["years"] if r.get("five_step")]
        if five:
            r = five[-1]
            f = r["five_step"]
            out.append(
                Paragraph(
                    clean(
                        f"Five-step, {r['fy']}: tax burden {_g(f['tax_burden'], '0.01')} x interest "
                        f"burden {_g(f['interest_burden'], '0.01')} x EBIT margin "
                        f"{_g(f['ebit_margin'], '0.1')}% x asset turnover "
                        f"{_g(f['asset_turnover'], '0.01')}x x equity multiplier "
                        f"{_g(f['equity_multiplier'], '0.01')}x."
                    ),
                    st["body"],
                )
            )
        out.append(Paragraph(clean(dp["convention"]), st["small"]))
    else:
        gaps.append(f"DuPont analysis: {dp['reason']}.")
    prows = [
        _hdr(
            st,
            "Versus the peer group",
            "Company",
            "Peer-group median",
            "Difference",
            "Peers",
            right_from=1,
        )
    ]
    labels = {}
    for grp in (h.get("working_capital", {}).get("days", {}), h.get("leverage_liquidity", {}).get("ratios", {}), h["returns"]["metrics"]):  # fmt: skip
        labels.update({k: v["label"] for k, v in grp.items()})
    for key, b in h["peers"].items():
        label = labels.get(key, key)
        if b.get("available"):
            unit = {"days": " days", "x": "x", "pct": "%"}.get(b["kind"], "")
            prows.append(
                [
                    Paragraph(clean(f"{label} ({b['fy']})"), st["cell"]),
                    Paragraph(f"{_g(b['company'], '0.1')}{unit}", st["cellr"]),
                    Paragraph(f"{_g(b['peer_group_median'], '0.1')}{unit}", st["cellr"]),
                    Paragraph(
                        f"{_pp(b['difference_pp'])}{' pp' if b['kind'] == 'pct' else unit}",
                        st["cellr"],
                    ),
                    Paragraph(str(b["peer_count"]), st["cellr"]),
                ]
            )
        elif b.get("reason"):
            gaps.append(f"Peer comparison, {label.lower()}: {b['reason']}.")
    if len(prows) > 1:
        out += [Spacer(1, 6), _table(prows, [W - 120 * mm, 26 * mm, 36 * mm, 30 * mm, 28 * mm])]
    if gaps:
        out += [Spacer(1, 4), Paragraph("Not available", st["h2"])]
        out += _bullets(gaps, st)
    out += [Spacer(1, 4)] + _bullets(h["definitions"], st)
    return out
