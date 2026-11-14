# ruff: noqa: E501
"""Industry and peer analysis: what the industry looks like, what each benchmark is, and how far the peer
comparison can be trusted.

`analysis["industry_view"]` keeps three benchmarks apart and names each exactly:

* PEER_GROUP: the selected peers (same statement profile, sector and industry, ordered by business
  similarity and size). Its CAGR is the "peer-group CAGR".
* SECTOR_LABEL_GROUP: every stored company the data provider files under the same sector label, limited
  to those with the same fiscal years and accounting basis. Its CAGR is the "sector-label group CAGR"; it
  covers only the stored companies and says how many.
* INDUSTRY: verified official statistics (industry size and growth). EquityOS holds none, so there is no
  industry CAGR and no industry size; nothing stands in for it. If verified statistics are stored later
  they appear here, with their source, as the only thing called the industry.

Structure and competitive intensity are measured among the STORED companies of the sector-label group
(concentration of revenue: share of the largest and the top three, and HHI). They describe the stored
universe, never the market, and the reading is withheld unless the stored companies cover most of the
group.

Opportunities, threats, regulation and the macroeconomic environment are sector context written for each
sector profile (no statistics). Each opportunity or threat is tied to the company's own stored figures
where one exists, so the context is checked against evidence rather than asserted.

The peer-quality block says how many selected peers are comparable for each metric (same period, accounting
basis and unit), the reliability of each comparison, and which comparisons are withheld because too few
peers support them.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, CompanyClassification, EvidenceRecord, EvidenceStatus
from app.research import industry as ind
from app.research import sector_kpis as sk
from app.research.analysis import MIN_RANK_PEERS, MIN_RANK_SHARE

VERSION = "1.0"
MIN_COVERAGE = Decimal(
    "0.6"
)  # stored companies must cover this share of the label group for a structure reading
HHI_FRAGMENTED, HHI_CONCENTRATED = 1500, 2500
SCHEME = "UPSTOX_SECTOR"
REV_NAMES = ("INCOME.revenue_from_operations", "INCOME.total_income", "INCOME.summary.revenue")

# sector -> (kind, theme, indicator key in sector_kpis, what it tests)
WATCH = {
    "BANK": (
        (
            "threat",
            "Asset quality: slippages and provisions decide how much income is kept",
            "gnpa_ratio",
        ),
        (
            "threat",
            "Funding cost: a shrinking share of low-cost deposits compresses margins",
            "casa",
        ),
        ("opportunity", "Credit growth from rising formal borrowing", "credit_growth"),
        ("threat", "Margin pressure from changing rates and deposit competition", "nim"),
    ),
    "IT_SERVICES": (
        ("threat", "Wage inflation and attrition raise costs", "employee_cost_ratio"),
        ("threat", "Pricing pressure and automation compress operating margins", "ebit_margin"),
        ("opportunity", "Client demand for technology spending", "revenue_growth"),
        ("threat", "Attrition and utilisation determine delivery capacity", "attrition"),
    ),
    "FMCG": (
        ("opportunity", "Rising consumption and premiumisation", "revenue_growth"),
        ("threat", "Input-cost inflation squeezes gross margin", "gross_margin"),
        ("threat", "Distribution reach and trade credit tie up cash", "receivable_days"),
    ),
    "AUTOMOBILES": (
        ("opportunity", "Demand growth in vehicle sales", "revenue_growth"),
        (
            "threat",
            "Operating leverage: fixed costs make profit sensitive to volumes",
            "ebitda_margin",
        ),
        ("threat", "Inventory build-up when demand slows", "inventory_days"),
    ),
    "ENERGY": (
        ("threat", "Commodity price cycles swing revenue", "revenue_growth"),
        ("threat", "Margins depend on spreads the company does not control", "ebitda_margin"),
        (
            "threat",
            "Capital-heavy balance sheets carry debt through the cycle",
            "net_debt_to_ebitda",
        ),
    ),
}


def _latest_revenue_by_company(session: Session, ids: list, prefer: str) -> dict:
    """{company_id: (period_end, value, basis, evidence_id)}: the latest annual revenue of each company."""
    out: dict = {}
    rows = session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id.in_(ids),
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.metric.in_(REV_NAMES),
            EvidenceRecord.period_type == "ANNUAL",
            EvidenceRecord.value.is_not(None),
        )
    )
    for r in rows:
        key = (r.period_end, REV_NAMES.index(r.metric) * -1, r.basis == prefer)
        cur = out.get(r.company_id)
        if cur is None or key > cur[0]:
            out[r.company_id] = (key, r)
    return {cid: (rec.period_end, rec.value, rec.basis, rec.id) for cid, (_k, rec) in out.items()}


def structure(session: Session, company: Company, prefer: str) -> dict:
    """Concentration of revenue among the stored companies that share the company's sector label."""
    row = session.get(CompanyClassification, (company.id, SCHEME))
    if row is None or row.label.strip().lower() in ind.NOT_AN_INDUSTRY:
        return {"available": False, "reason": "no usable industry label is stored for this company, so no group of stored companies can be formed"}  # fmt: skip
    members = list(
        session.scalars(
            select(Company)
            .join(CompanyClassification, CompanyClassification.company_id == Company.id)
            .where(CompanyClassification.scheme == SCHEME, CompanyClassification.label == row.label)
        )
    )
    group_size = len(members)
    revenue = _latest_revenue_by_company(session, [m.id for m in members], prefer)
    if len(revenue) < 3:
        return {"available": False, "label": row.label, "group_companies": group_size, "with_revenue": len(revenue), "reason": f"only {len(revenue)} of the {group_size} stored companies under '{row.label}' have an annual revenue stored, too few to describe a structure"}  # fmt: skip
    # one fiscal year: the most common latest period, so no company is compared on another year
    years: dict = {}
    for end, *_rest in revenue.values():
        years[end] = years.get(end, 0) + 1
    year = max(years, key=lambda e: (years[e], e))
    used = {cid: v for cid, v in revenue.items() if v[0] == year and v[1] > 0}
    total = sum((v[1] for v in used.values()), Decimal(0))
    if len(used) < 3 or total <= 0:
        return {"available": False, "label": row.label, "group_companies": group_size, "with_revenue": len(used), "reason": "fewer than three stored companies share a fiscal year with a positive revenue"}  # fmt: skip
    shares = sorted(((v[1] / total * 100, cid) for cid, v in used.items()), reverse=True)
    hhi = sum((s * s for s, _ in shares), Decimal(0))
    coverage = Decimal(len(used)) / Decimal(group_size)
    names = {m.id: m.common_name for m in members}
    own = next((i for i, (_s, cid) in enumerate(shares, 1) if cid == company.id), None)
    out = {
        "available": True,
        "label": row.label,
        "fiscal_year_end": year.isoformat(),
        "group_companies": group_size,
        "with_revenue": len(used),
        "coverage_pct": str((coverage * 100).quantize(Decimal("1"))),
        "combined_revenue": str(total),
        "top1": {
            "name": names[shares[0][1]],
            "share_pct": str(shares[0][0].quantize(Decimal("0.1"))),
        },
        "top3_share_pct": str(sum((s for s, _ in shares[:3]), Decimal(0)).quantize(Decimal("0.1"))),
        "hhi": int(hhi.quantize(Decimal("1"))),
        "company_rank": own,
        "company_share_pct": str(shares[own - 1][0].quantize(Decimal("0.1"))) if own else None,
        "evidence_ids": [v[3] for v in used.values()][:30],
        "scope": f"among the {len(used)} stored companies under the sector label '{row.label}' with revenue for the year ended {year.isoformat()}; this is the stored universe, not the market",  # fmt: skip
    }
    if coverage >= MIN_COVERAGE:
        out["reading"] = "FRAGMENTED" if out["hhi"] < HHI_FRAGMENTED else "CONCENTRATED" if out["hhi"] >= HHI_CONCENTRATED else "MODERATELY_CONCENTRATED"  # fmt: skip
        out["rule"] = f"HHI (sum of squared revenue shares, 0 to 10,000) below {HHI_FRAGMENTED}: fragmented; {HHI_FRAGMENTED} to {HHI_CONCENTRATED}: moderately concentrated; above: concentrated. Read only when the stored companies cover at least {int(MIN_COVERAGE * 100)}% of the group"  # fmt: skip
    else:
        out["reading"] = None
        out["rule"] = f"no reading: the stored companies are {out['coverage_pct']}% of the group, below the {int(MIN_COVERAGE * 100)}% needed to describe its structure; the figures are shown as partial"  # fmt: skip
    return out


def environment(sector_code: str) -> dict:
    t = ind.template(sector_code)
    return {
        "kind": "CONTEXT",
        "regulation": t["regulation"],
        "macro_sensitivity": [
            {"factor": f, "sensitivity": lvl, "why": why}
            for f, lvl, why in (t.get("sensitivity") or [])
        ],
        "leading_indicators": list(t.get("leading_indicators") or []),
        "demand": t.get("demand"),
        "note": ind.CONTEXT_NOTE,
    }


def _reading(k: dict) -> dict | None:
    if not k or not k.get("available"):
        return None
    v = next((x for x in k["variants"] if not x["dimension"]), k["variants"][0])
    latest = v["latest"]
    out = {"label": k["label"], "latest": latest["value"], "period": latest["fy"] if v["period_type"] == "ANNUAL" else latest["period_end"], "kind": k["kind"], "source": k["source"], "evidence_ids": [latest["evidence_id"]]}  # fmt: skip
    if "change" in v:
        out.update({"change": v["change"], "change_from": v["change_from"], "change_to": v["change_to"], "evidence_ids": v["change_evidence_ids"]})  # fmt: skip
        change = Decimal(v["change"])
        lower = k["key"] in sk.LOWER_BETTER
        if change == 0:
            out["direction"] = "UNCHANGED"
        else:
            out["direction"] = "FAVOURABLE" if (change < 0) == lower else "UNFAVOURABLE"
    return out


def opportunities_threats(sector_code: str, sector_kpis: dict | None) -> dict:
    t = ind.template(sector_code)
    watch = []
    indicators = (sector_kpis or {}).get("indicators") or {}
    for kind, theme, key in WATCH.get(sector_code, ()):
        r = _reading(indicators.get(key))
        watch.append({"kind": kind.upper(), "theme": theme, "indicator": key, "company_evidence": r, "reason": None if r else "the company's figure for this is not stored"})  # fmt: skip
    return {
        "opportunities": list(t["opportunities"]),
        "threats": list(t["challenges"]),
        "checked_against_company": watch,
        "note": "the lists are sector context, not statistics; each theme below is checked against the company's own stored figure where one exists, and the direction shown is what that figure did, favourable or unfavourable for the theme",  # fmt: skip
    }


def peer_quality(analysis: dict) -> dict:
    peers = analysis.get("peers") or {}
    rows = peers.get("rows") or []
    selected = len(peers.get("peers") or [])
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "NONE": 0}
    withheld = []
    for r in rows:
        counts[r.get("reliability", "NONE")] = counts.get(r.get("reliability", "NONE"), 0) + 1
        if r.get("position") == "not ranked" or r.get("reliability") in ("LOW", "NONE"):
            withheld.append({"label": r["label"], "comparable": r["coverage"]["comparable"] if r.get("coverage") else 0, "selected": selected, "why": r.get("note") or "fewer than three comparable peers"})  # fmt: skip
    warnings = []
    if selected < 4:
        warnings.append(f"only {selected} peers are selected; a rank among so few is withheld")
    if rows and counts["HIGH"] + counts["MEDIUM"] == 0:
        warnings.append(
            "no metric has enough comparable peers for a rank; only medians (where three or more peers compare) are shown"
        )
    return {
        "available": bool(rows) or selected > 0,
        "selected_peers": selected,
        "peers": [{"name": p["name"], "reason": p.get("reason")} for p in peers.get("peers", [])],
        "metrics": len(rows),
        "reliability_counts": counts,
        "withheld": withheld,
        "warnings": warnings,
        "policy": f"a median needs 3 comparable peers; a rank or better/worse verdict needs {MIN_RANK_PEERS} comparable peers and {int(MIN_RANK_SHARE * 100)}% of the selected peers. A peer is compared only on the same period end, accounting basis and unit; a peer on another period or basis is listed but never compared",  # fmt: skip
    }


def benchmarks(analysis: dict) -> dict:
    prof = analysis.get("profitability") or {}
    pg = (prof.get("peer_growth") or {}).get("revenue") or {}
    ig = ((analysis.get("growth") or {}).get("industry_group")) or {}
    group = None
    if ig.get("available"):
        metrics = ig.get("metrics") or {}
        m = metrics.get("revenue") or next((x for x in metrics.values() if x.get("available")), {})
        if m.get("available"):
            group = {"cagr": m["median_cagr"], "aggregate_cagr": m.get("aggregate_cagr"), "members_used": m["members_used"], "group_size": m["group_size"], "label": ig.get("label")}  # fmt: skip
    return {
        "peer_group": {
            "name": "Peer-group CAGR",
            "what": "the selected peers (same statement profile, sector and industry, ordered by business similarity and size), on the same fiscal years and accounting basis",
            "revenue_cagr_median": pg.get("peer_group_median_cagr")
            if pg.get("peer_benchmark")
            else None,
            "peers": pg.get("peer_count") if pg.get("peer_benchmark") else None,
            "available": bool(pg.get("peer_benchmark")),
            "reason": None
            if pg.get("peer_benchmark")
            else (
                pg.get("reason")
                or "fewer than three peers have the same fiscal years on the same basis"
            ),
        },
        "sector_label_group": {
            "name": "Sector-label group CAGR",
            "what": "the stored companies the data provider files under the same sector label, on the same fiscal years and basis; it covers only those stored companies",
            "available": group is not None,
            "revenue": group,
            "reason": None
            if group
            else (
                ig.get("reason")
                or "too few stored companies under the label share the same fiscal years on the same basis"
            ),
        },
        "industry": {
            "name": "Industry CAGR",
            "what": "official, verified industry statistics",
            "available": False,
            "reason": "no verified industry statistics are held, so there is no industry size, no industry CAGR and no growth alpha; the peer-group and sector-label group figures are never called the industry",
        },
    }


def build(session: Session, company: Company, analysis: dict, prefer: str) -> dict:
    sector_code = (analysis.get("sector") or {}).get("code") or "GENERIC_NONFIN"
    struct = structure(session, company, prefer)
    out = {
        "version": VERSION,
        "available": True,
        "basis": prefer,
        "benchmarks": benchmarks(analysis),
        "industry_size": {
            "available": False,
            "reason": "industry size and growth need verified official statistics, which EquityOS does not hold; the stored universe below is a coverage figure for the sector label, not the size of the industry",
        },
        "structure": struct,
        "environment": environment(sector_code),
        "opportunities_threats": opportunities_threats(sector_code, analysis.get("sector_kpis")),
        "peer_quality": peer_quality(analysis),
        "definitions": [
            "Peer-group CAGR, sector-label group CAGR and industry CAGR are three different things and are never interchanged; only verified official statistics could be called the industry.",
            "Structure is measured among stored companies only and is withheld unless they cover most of the sector-label group.",
            "Sector context (regulation, macroeconomic sensitivity, opportunities, threats) contains no statistics; it is checked against the company's own stored figures where possible.",
        ],
    }
    return out
