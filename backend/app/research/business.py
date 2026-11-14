# ruff: noqa: E501
"""Company and business analysis: how the business earns, where it is positioned, what it is good at and
where it is structurally exposed.

`analysis["business"]` joins two kinds of support and never mixes them up:

* DOCUMENT_QUOTE: a sentence quoted verbatim from the company's own latest annual report with its page
  (business model, revenue sources, product exposure, advantages, positioning, dependencies). It is the
  company's claim, not EquityOS's finding, and is labelled that way.
* EVIDENCE: a statement built by a stated rule from validated figures already in the analysis (growth
  against the peer group, margin direction, cash conversion, returns, leverage, segment concentration).
  Each cites the evidence ids it rests on and names the rule.

Nothing is written about the company that neither a quote nor a rule supports; a topic with no support
says so. Strengths and weaknesses are reported side by side so a reader sees both. Readings are rules of
thumb with their thresholds, not conclusions about the company's quality.

Revenue sources are shown from figures: for non-financials, revenue from operations against other income;
for banks, net interest income against other (non-interest) income; plus the segment mix and profit
contribution where segments are reported.
"""

from decimal import Decimal

from app.research import analysis as an
from app.research import readiness as rd
from app.research.sectors import AMOUNT, Metric

VERSION = "1.0"
ROE_STRONG = Decimal("15")
OTHER_INCOME_HIGH = Decimal("25")  # other income above a quarter of total income
CONCENTRATION = Decimal("70")  # one segment above 70% of reported segment revenue
MAX_STATED = 3

TOTAL = Metric("total_income", "Total income", ("INCOME.total_income",), AMOUNT, ptype="ANNUAL")
REV = Metric("revenue", "Revenue from operations", ("INCOME.revenue_from_operations",), AMOUNT, ptype="ANNUAL")  # fmt: skip
OTHER = Metric("other_income", "Other income", ("INCOME.other_income",), AMOUNT, ptype="ANNUAL")
NII = Metric("nii", "Net interest income", ("INCOME.net_interest_income",), AMOUNT, ptype="ANNUAL")  # fmt: skip
NAMES = frozenset({n for m in (TOTAL, REV, OTHER, NII) for n in m.names})


def _pt(kind, text, rule, ids, side, **extra) -> dict:
    return {"source": kind, "text": text, "side": side, "rule": rule, "evidence_ids": [i for i in ids if i], **extra}  # fmt: skip


def revenue_mix(store, basis: str, bank: bool) -> dict:
    """Where income comes from, by year: operating revenue (or net interest income) against other income."""
    other = {
        d: r
        for d, r in an._records(store, OTHER.names, "ANNUAL", basis).items()
        if r.basis == basis
    }
    main = an._records(store, (NII if bank else REV).names, "ANNUAL", basis)
    main = {d: r for d, r in main.items() if r.basis == basis}
    years = sorted(set(main) & set(other))[-4:]
    if not years:
        return {"available": False, "reason": "operating revenue and other income are not both stored for any year on this basis"}  # fmt: skip
    rows = []
    for end in years:
        m, o = main[end], other[end]
        denom = m.value + o.value
        if denom <= 0:
            continue
        rows.append(
            {
                "fy": rd.fiscal_year(end),
                "main": an._s(m.value),
                "other": an._s(o.value),
                "main_share_pct": an._s((m.value / denom * 100).quantize(Decimal("0.1"))),
                "other_share_pct": an._s((o.value / denom * 100).quantize(Decimal("0.1"))),
                "evidence_ids": [m.id, o.id],
            }
        )
    if not rows:
        return {"available": False, "reason": "income is not positive in the stored years"}
    return {
        "available": True,
        "basis": basis,
        "main_label": "Net interest income" if bank else "Revenue from operations",
        "other_label": "Other (non-interest) income" if bank else "Other income",
        "rows": rows,
        "note": "shares are of the two lines added together; a bank's net interest income is after interest expense, so its shares are not shares of total income",  # fmt: skip
    }


def _last(series):
    return series[-1] if series else None


def financial_points(analysis: dict) -> tuple[list[dict], list[dict]]:
    """Strength and weakness statements from rules over the analysis blocks already built."""
    strengths, weaknesses = [], []

    def add(side, text, rule, ids, **kw):
        (strengths if side == "strength" else weaknesses).append(
            _pt("EVIDENCE", text, rule, ids, side, **kw)
        )

    prof = analysis.get("profitability") or {}
    for key, label in (("revenue", "revenue"), ("net_profit", "net profit")):
        b = (prof.get("peer_growth") or {}).get(key) or {}
        if b.get("peer_benchmark"):
            d = Decimal(b["difference_vs_peer_median_pp"])
            c = b["company"]
            ids = c.get("evidence_ids", [])
            if d >= 2:
                add("strength", f"{label.capitalize()} grew {d} percentage points a year faster than the peer-group median ({c['cagr']}% against {b['peer_group_median_cagr']}%).", "company CAGR at least 2 points above the peer-group median", ids)  # fmt: skip
            elif d <= -2:
                add("weakness", f"{label.capitalize()} grew {abs(d)} percentage points a year slower than the peer-group median ({c['cagr']}% against {b['peer_group_median_cagr']}%).", "company CAGR at least 2 points below the peer-group median", ids)  # fmt: skip
    for key in ("ebitda_margin", "ebit_margin", "net_margin", "ppop_margin"):
        m = (prof.get("margins") or {}).get(key) or {}
        if m.get("available") and m.get("direction") in ("EXPANDING", "CONTRACTING"):
            word = "expanded" if m["direction"] == "EXPANDING" else "contracted"
            add("strength" if m["direction"] == "EXPANDING" else "weakness", f"{m['label']} {word} by {abs(Decimal(m['change_pp'])).quantize(Decimal('0.1'))} percentage points between {m['start']['fy']} and {m['end']['fy']}.", "a margin change of at least 0.5 percentage points over the window", [m["start"]["evidence_id"], m["end"]["evidence_id"]])  # fmt: skip
            break
    cf = analysis.get("cashflow") or {}
    cum = ((cf.get("conversion") or {}).get("cumulative")) if cf.get("available") else None
    if cum:
        if cum["reading"] == "STRONG":
            add("strength", f"Operating cash flow was {(Decimal(cum['ratio']) * 100).quantize(Decimal('1'))}% of net profit over {cum['from_fy']} to {cum['to_fy']}: profit turned into cash.", "CFO / PAT at least 1.0 over the unbroken years", cum["evidence_ids"])  # fmt: skip
        elif cum["reading"] in ("WEAK", "POOR"):
            add("weakness", f"Operating cash flow was only {(Decimal(cum['ratio']) * 100).quantize(Decimal('1'))}% of net profit over {cum['from_fy']} to {cum['to_fy']}.", "CFO / PAT below 0.8 over the unbroken years", cum["evidence_ids"])  # fmt: skip
    acc = (((cf.get("accruals") or {}).get("ratio")) or {}) if cf.get("available") else {}
    if acc.get("available"):
        last = acc["series"][-1]
        if last.get("reading") in ("ELEVATED", "HIGH"):
            add("weakness", f"The {last['fy']} accrual ratio of {Decimal(last['value'])}% of assets shows profit running ahead of cash.", "accrual ratio at or above 5% of average assets", [last["evidence_id"]])  # fmt: skip
    h = analysis.get("health") or {}
    roe = ((h.get("returns") or {}).get("metrics") or {}).get("roe") or {}
    if roe.get("available"):
        last = roe["series"][-1]
        if Decimal(last["value"]) >= ROE_STRONG:
            add("strength", f"Return on equity was {Decimal(last['value'])}% in {last['fy']}.", f"ROE (average equity) at least {ROE_STRONG}%", [last["evidence_id"]])  # fmt: skip
    lev = (h.get("leverage_liquidity") or {}).get("ratios") or {}
    nd = lev.get("net_debt_to_ebitda") or {}
    if nd.get("available"):
        last = nd["series"][-1]
        if last["reading"] in ("NET_CASH", "LOW"):
            add("strength", f"Net debt / EBITDA was {Decimal(last['value'])}x in {last['fy']} ({last['reading'].lower().replace('_', ' ')}): the balance sheet is lightly geared.", "net debt / EBITDA below 1x", [last["evidence_id"]])  # fmt: skip
        elif last["reading"] == "HIGH":
            add("weakness", f"Net debt / EBITDA was {Decimal(last['value'])}x in {last['fy']}: the balance sheet carries heavy debt.", "net debt / EBITDA at or above 3x", [last["evidence_id"]])  # fmt: skip
    ic = lev.get("interest_cover") or {}
    if ic.get("available"):
        last = ic["series"][-1]
        if last["reading"] == "STRETCHED":
            add("weakness", f"Interest cover was {Decimal(last['value'])}x in {last['fy']}.", "EBIT / finance costs below 1.5x", [last["evidence_id"]])  # fmt: skip
        elif last["reading"] == "COMFORTABLE" and Decimal(last["value"]) >= 10:
            add("strength", f"Interest cover was {Decimal(last['value'])}x in {last['fy']}: debt service is not a constraint.", "EBIT / finance costs at least 10x", [last["evidence_id"]])  # fmt: skip
    cr = lev.get("current_ratio") or {}
    if cr.get("available") and cr["series"][-1].get("reading") == "TIGHT":
        last = cr["series"][-1]
        add("weakness", f"The current ratio was {Decimal(last['value'])}x in {last['fy']}: current liabilities exceed current assets.", "current ratio below 1x", [last["evidence_id"]])  # fmt: skip
    seg = (analysis.get("profile") or {}).get("segments") or {}
    if (
        seg.get("available")
        and seg.get("largest_share_pct")
        and Decimal(seg["largest_share_pct"]) >= CONCENTRATION
    ):
        top = max(seg["rows"], key=lambda r: Decimal(r["share_pct"] or 0))
        add("weakness", f"{top['segment']} is {Decimal(seg['largest_share_pct'])}% of reported segment revenue: the business depends on one segment.", f"one segment at least {CONCENTRATION}% of segment revenue", [top["evidence_id"]])  # fmt: skip
    return strengths, weaknesses


def _stated(narrative: dict, key: str) -> list[dict]:
    for t in (narrative or {}).get("topics", []):
        if t["key"] == key:
            return t["items"][:MAX_STATED]
    return []


def build(analysis: dict, store, basis: str) -> dict:
    prof = analysis.get("profile") or {}
    narrative = prof.get("narrative") or {}
    bank = (analysis.get("sector") or {}).get("code") == "BANK"
    strengths, weaknesses = financial_points(analysis)
    doc = narrative.get("document") if narrative.get("available") else None

    def quotes(key, side=None):
        return [{"source": "DOCUMENT_QUOTE", "text": i["text"], "page": i["page"], "side": side, "document_id": (doc or {}).get("id")} for i in _stated(narrative, key)]  # fmt: skip

    stated_strengths = quotes("advantages", "strength")
    stated_weaknesses = quotes("weaknesses", "weakness")
    peers = analysis.get("peers") or {}
    summary = peers.get("summary") or {}
    rows_by_label = {r["label"]: r for r in peers.get("rows", [])}
    positioning = {
        "peer_comparison": {
            "available": bool(summary.get("ranked_metrics")),
            "peer_count": len(peers.get("peers", [])),
            "better_on": [
                {
                    "label": lab,
                    "evidence_id": (rows_by_label.get(lab, {}).get("company") or {}).get(
                        "evidence_id"
                    ),
                }
                for lab in summary.get("better", [])
            ],  # fmt: skip
            "worse_on": [
                {
                    "label": lab,
                    "evidence_id": (rows_by_label.get(lab, {}).get("company") or {}).get(
                        "evidence_id"
                    ),
                }
                for lab in summary.get("worse", [])
            ],  # fmt: skip
            "in_line_on": list(summary.get("in_line", [])),
            "rule": peers.get("method")
            or "a peer is compared only on the same period, accounting basis and unit; a median needs at least three comparable peers",  # fmt: skip
        },
        "stated": quotes("positioning"),
    }
    if not positioning["peer_comparison"]["available"]:
        positioning["peer_comparison"]["reason"] = "fewer than three comparable peers hold the stored figures, so no peer rank is shown"  # fmt: skip
    model = {
        "stated": quotes("business_model"),
        "revenue_sources_stated": quotes("revenue_sources"),
        "product_exposure_stated": quotes("product_exposure"),
        "revenue_mix": revenue_mix(store, basis, bank),
    }
    seg = prof.get("segments") or {}
    available = bool(
        model["stated"] or model["revenue_sources_stated"] or model["product_exposure_stated"]
        or model["revenue_mix"]["available"] or seg.get("available") or stated_strengths or stated_weaknesses
        or strengths or weaknesses or positioning["stated"] or positioning["peer_comparison"]["available"]
    )  # fmt: skip
    return {
        "version": VERSION,
        "available": available,
        "basis": basis,
        "document": doc,
        "business_model": model,
        "segments_available": bool(seg.get("available")),
        "positioning": positioning,
        "strengths": {"evidence": strengths, "stated": stated_strengths},
        "weaknesses": {"evidence": weaknesses, "stated": stated_weaknesses},
        "balance": {"strengths": len(strengths), "weaknesses": len(weaknesses)},
        "definitions": [
            "DOCUMENT_QUOTE entries are sentences quoted verbatim from the company's latest stored annual report, filed by keyword cues, with their pages; they are the company's own claims and are not verified or paraphrased by EquityOS.",
            "EVIDENCE entries are produced by the stated rule from validated figures and cite the evidence behind them; thresholds are rules of thumb, not conclusions about quality.",
            "Structural dependencies a company does not state in its report cannot be found here; an empty list means none was stated or detected, not that none exists.",
        ],
    }
