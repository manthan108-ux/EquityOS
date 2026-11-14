"""Growth and profitability: how fast the company grows, how profitable it is, and how both compare
with its peer group. Built only from validated evidence; nothing is estimated.

What it produces (`analysis["profitability"]`):

* growth: for revenue, gross profit, EBITDA, EBIT and net profit (a bank: net interest income,
  operating profit before provisions and net profit), the annual series with year-on-year growth and
  the CAGR over the longest span the evidence holds and over exactly three years where it holds four
  year ends. A figure that cannot be calculated says why (fewer than two years, a loss in the base
  year, a line the company does not print).
* margins: gross, EBITDA, EBIT and net margin by year, the change in percentage points, whether the
  margin expanded, contracted or held, and in how many years it moved each way.
* drivers: what moved the net margin between two years, line by line (materials, employee costs,
  other expenses, depreciation, finance costs, tax, other income), with the part no line explains
  shown as "not itemised" rather than hidden.
* the peer-group comparison: the company's CAGR and margins against the same peer group over the
  same fiscal years on the same accounting basis.

Labelling. EquityOS holds no verified industry statistics, so the benchmark here is the PEER GROUP
(the same-industry listed companies closest in size that have stored filings), and it is called that
everywhere. It is never called the industry. The excess of the company's growth over the peer
group is a "difference vs the peer group in percentage points"; "growth alpha" (company CAGR minus
industry CAGR) is withheld until a verified industry benchmark exists, and the output says so.

One accounting basis runs through all of it: the basis resolved for the report. Company and peers
are compared only on that basis and over the same fiscal years.
"""

from datetime import date
from decimal import Decimal
from statistics import median

from app.research import analysis as an
from app.research import industry
from app.research import readiness as rd
from app.research.sectors import AMOUNT, PCT, Metric

VERSION = "1.0"
STABLE_PP = Decimal("0.5")  # a margin within +/- 0.5 percentage points over the window "held"
MIN_PEERS = 3
MAX_YEARS = 6
YEAR_DAYS = (350, 380)  # two annual periods this far apart are consecutive fiscal years

REVENUE = Metric(
    "revenue", "Revenue from operations", ("INCOME.revenue_from_operations", "INCOME.total_income"),
    AMOUNT, ptype="ANNUAL",
)  # fmt: skip
TOTAL_INCOME = Metric(
    "revenue", "Total income", ("INCOME.total_income", "INCOME.revenue_from_operations"),
    AMOUNT, ptype="ANNUAL",
)  # fmt: skip
GROSS = Metric("gross_profit", "Gross profit", ("INCOME.gross_profit",), AMOUNT, ptype="ANNUAL")
EBITDA = Metric("ebitda", "EBITDA", ("INCOME.ebitda",), AMOUNT, ptype="ANNUAL")
EBIT = Metric("ebit", "EBIT", ("INCOME.ebit",), AMOUNT, ptype="ANNUAL")
PAT = industry.PAT
NII = Metric(
    "net_interest_income", "Net interest income", ("INCOME.net_interest_income",), AMOUNT,
    ptype="ANNUAL",
)  # fmt: skip
PPOP = Metric(
    "ppop", "Operating profit before provisions", ("INCOME.operating_profit_before_provisions",),
    AMOUNT, ptype="ANNUAL",
)  # fmt: skip

GROWTH_NONBANK = (REVENUE, GROSS, EBITDA, EBIT, PAT)
GROWTH_BANK = (TOTAL_INCOME, NII, PPOP, PAT)

M_GROSS = Metric("gross_margin", "Gross margin", ("MARGIN.gross_margin",), PCT, ptype="ANNUAL")
M_EBITDA = Metric("ebitda_margin", "EBITDA margin", ("MARGIN.ebitda_margin",), PCT, ptype="ANNUAL")
M_EBIT = Metric("ebit_margin", "EBIT margin", ("MARGIN.ebit_margin",), PCT, ptype="ANNUAL")
M_NET = Metric("net_margin", "Net profit margin", ("MARGIN.net_margin",), PCT, ptype="ANNUAL")
M_PPOP = Metric(
    "ppop_margin", "Operating profit before provisions margin", ("MARGIN.ppop_margin",), PCT,
    ptype="ANNUAL",
)  # fmt: skip
M_NET_INCOME = Metric(
    "net_profit_to_income", "Net profit to total income", ("MARGIN.net_profit_to_income",), PCT,
    ptype="ANNUAL",
)  # fmt: skip
MARGINS_NONBANK = (M_GROSS, M_EBITDA, M_EBIT, M_NET)
MARGINS_BANK = (M_PPOP, M_NET_INCOME)

# cost lines for the margin bridge: (label, evidence names, sign in the bridge)
DRIVERS = (
    ("Cost of goods (materials, purchases, inventories)", (
        "INCOME.cost_of_materials_consumed", "INCOME.purchases_of_stock_in_trade",
        "INCOME.changes_in_inventories"), -1),
    ("Employee benefits", ("INCOME.employee_benefits_expense",), -1),
    ("Other expenses", ("INCOME.other_expenses",), -1),
    ("Depreciation and amortisation", ("INCOME.depreciation_and_amortisation",), -1),
    ("Finance costs", ("INCOME.finance_costs",), -1),
    ("Tax", ("INCOME.tax_expense",), -1),
    ("Other income", ("INCOME.other_income",), +1),
)  # fmt: skip

NAMES = frozenset(
    {n for m in (*GROWTH_NONBANK, *GROWTH_BANK, *MARGINS_NONBANK, *MARGINS_BANK) for n in m.names}
    | {n for _, names, _ in DRIVERS for n in names}
    | {"INCOME.net_profit", "INCOME.revenue_from_operations"}
)


def _dec(value) -> Decimal | None:
    return an.dec(value)


def _on_basis(store, names, basis) -> dict:
    """{period_end: record} for annual records on exactly `basis` (never the other one)."""
    return {d: r for d, r in an._records(store, names, "ANNUAL", basis).items() if r.basis == basis}


def _consecutive(a: date, b: date) -> bool:
    return YEAR_DAYS[0] <= (b - a).days <= YEAR_DAYS[1]


def _cagr_block(recs: dict, start: date, end: date) -> dict | None:
    c = industry._cagr_for(recs, start, end)
    if c is None:
        return None
    return {
        "pct": an._s(c["cagr"].quantize(Decimal("0.1"))),
        "years": c["years"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_fy": rd.fiscal_year(start),
        "end_fy": rd.fiscal_year(end),
        "start_value": an._s(c["first"].value),
        "end_value": an._s(c["last"].value),
        "unit": c["first"].unit,
        "evidence_ids": [c["first"].id, c["last"].id],
    }


def growth_metric(store, m: Metric, basis: str) -> dict:
    recs = _on_basis(store, m.names, basis)
    ends = sorted(recs)[-MAX_YEARS:]
    if not ends:
        return {
            "label": m.label,
            "available": False,
            "reason": f"no annual {m.label.lower()} is stored on the {basis.lower()} basis "
            "(the company does not print the lines it is built from, or they are not yet read)",
        }
    series = []
    for i, end in enumerate(ends):
        rec = recs[end]
        row = {
            "period_end": end.isoformat(),
            "fy": rd.fiscal_year(end),
            "value": an._s(rec.value),
            "unit": rec.unit,
            "evidence_id": rec.id,
            "yoy_pct": None,
        }
        if i and _consecutive(ends[i - 1], end):
            prev = recs[ends[i - 1]].value
            if prev is not None and prev > 0 and rec.value is not None:
                row["yoy_pct"] = an._s(((rec.value / prev - 1) * 100).quantize(Decimal("0.1")))
                row["prior_evidence_id"] = recs[ends[i - 1]].id
                if rec.value <= 0:
                    row["note"] = "the figure is zero or negative"
        series.append(row)
    out: dict = {"label": m.label, "available": True, "basis": basis, "series": series}
    if len(ends) < 2:
        out["reason"] = "only one annual value is stored, so no growth can be measured"
        return out
    span = _cagr_block(recs, ends[0], ends[-1])
    if span is None:
        out["reason"] = (
            "the CAGR is undefined: the first or last value is zero or negative "
            "(a CAGR needs a positive start and end)"
        )
    out["cagr"] = span
    if len(ends) >= 4:
        three = _cagr_block(recs, ends[-4], ends[-1])
        if three and _years_between(ends[-4], ends[-1]) == 3:
            out["cagr_3y"] = three
    return out


def _years_between(a: date, b: date) -> int:
    return round((b - a).days / 365.25)


def _direction(delta: Decimal | None) -> str | None:
    if delta is None:
        return None
    if delta >= STABLE_PP:
        return "EXPANDING"
    if delta <= -STABLE_PP:
        return "CONTRACTING"
    return "STABLE"


def margin_metric(store, m: Metric, basis: str, *, lower_is_better: bool = False) -> dict:
    recs = _on_basis(store, m.names, basis)
    ends = sorted(recs)[-MAX_YEARS:]
    if not ends:
        return {
            "label": m.label,
            "available": False,
            "reason": f"no annual {m.label.lower()} is stored on the {basis.lower()} basis "
            "(it needs lines this company does not print or that are not yet read)",
        }
    series = []
    up = down = pairs = 0
    for i, end in enumerate(ends):
        rec = recs[end]
        row = {
            "period_end": end.isoformat(),
            "fy": rd.fiscal_year(end),
            "value": an._s(rec.value.quantize(Decimal("0.01"))),
            "evidence_id": rec.id,
            "change_bps": None,
        }
        if i and _consecutive(ends[i - 1], end):
            delta = rec.value - recs[ends[i - 1]].value
            row["change_bps"] = int((delta * 100).quantize(Decimal("1")))
            pairs += 1
            up += delta > 0
            down += delta < 0
        series.append(row)
    out: dict = {"label": m.label, "available": True, "basis": basis, "series": series}
    if len(ends) >= 2:
        first, last = recs[ends[0]], recs[ends[-1]]
        delta = (last.value - first.value).quantize(Decimal("0.01"))
        out.update(
            {
                "start": {
                    "fy": rd.fiscal_year(ends[0]),
                    "value": an._s(first.value.quantize(Decimal("0.01"))),
                    "evidence_id": first.id,
                },  # fmt: skip
                "end": {
                    "fy": rd.fiscal_year(ends[-1]),
                    "value": an._s(last.value.quantize(Decimal("0.01"))),
                    "evidence_id": last.id,
                },  # fmt: skip
                "change_pp": an._s(delta),
                "direction": _direction(delta),
                "years_up": int(up),
                "years_down": int(down),
                "years_compared": pairs,
                "peak": _extreme(recs, ends, max),
                "trough": _extreme(recs, ends, min),
            }
        )
        out["lower_is_better"] = lower_is_better
    else:
        out["reason"] = "only one annual value is stored, so no change can be measured"
    return out


def _extreme(recs, ends, fn) -> dict:
    end = fn(ends, key=lambda d: recs[d].value)
    value = an._s(recs[end].value.quantize(Decimal("0.01")))
    return {"fy": rd.fiscal_year(end), "value": value, "evidence_id": recs[end].id}


def observations(margins: dict[str, dict]) -> list[dict]:
    """Rule-based readings of the margin series. Each carries the evidence it rests on."""
    out = []
    for key, m in margins.items():
        if not m.get("available") or "change_pp" not in m:
            continue
        s, e = m["start"], m["end"]
        verb = {
            "EXPANDING": "expanded",
            "CONTRACTING": "contracted",
            "STABLE": "was broadly unchanged",
        }[m["direction"]]
        pp = abs(Decimal(m["change_pp"])).quantize(Decimal("0.1"))
        first_v = Decimal(s["value"]).quantize(Decimal("0.1"))
        last_v = Decimal(e["value"]).quantize(Decimal("0.1"))
        text = f"{m['label']} {verb}"
        if m["direction"] != "STABLE":
            text += f" by {pp} percentage points"
        text += f" between {s['fy']} and {e['fy']} ({first_v}% to {last_v}%)"
        if m["years_compared"]:
            text += (
                f"; it rose in {m['years_up']} and fell in {m['years_down']} of "
                f"{m['years_compared']} years"
            )
        out.append(
            {
                "key": key,
                "text": text + ".",
                "kind": "INTERPRETATION",
                "rule": f"a margin within {STABLE_PP} percentage points over the window is 'held'",
                "evidence_ids": [s["evidence_id"], e["evidence_id"]],
            }
        )
    return out


def _sum_lines(store, names, basis):
    """{period_end: (total, [evidence ids])} summing the lines printed in EVERY year listed."""
    per = [_on_basis(store, (n,), basis) for n in names]
    per = [p for p in per if p]
    if not per:
        return {}
    shape = tuple(sorted(i for i, p in enumerate(per)))
    out = {}
    for d in set.intersection(*(set(p) for p in per)):
        out[d] = (sum(p[d].value for p in per), [p[d].id for p in per], shape)
    return out


def drivers(store, basis: str) -> dict:
    """What moved the net margin between the first and last year where every line is available."""
    rev = _on_basis(store, ("INCOME.revenue_from_operations",), basis)
    net = _on_basis(store, ("INCOME.net_profit",), basis)
    lines = []
    for label, names, sign in DRIVERS:
        series = _sum_lines(store, names, basis)
        if series:
            lines.append((label, series, sign))
    years = sorted(set(rev) & set(net))
    if len(lines) < 3 or len(years) < 2:
        return {
            "available": False,
            "reason": "fewer than three cost lines are stored for two or more years on this basis, "
            "so the margin change cannot be itemised",
        }
    usable = [d for d in years if sum(1 for _, s, _ in lines if d in s) >= 3]
    if len(usable) < 2:
        return {"available": False, "reason": "the cost lines are not stored for two common years"}
    start, end = usable[0], usable[-1]
    r0, r1 = rev[start].value, rev[end].value
    if r0 <= 0 or r1 <= 0:
        return {"available": False, "reason": "revenue is not positive in the span"}
    chosen = [(label, s, sign) for label, s, sign in lines if start in s and end in s]
    if len(chosen) < 3:
        return {
            "available": False,
            "reason": "the same three cost lines are not stored in both years",
        }
    items, itemised = [], Decimal(0)
    for label, s, sign in chosen:
        t0, ids0, _ = s[start]
        t1, ids1, _ = s[end]
        ratio0, ratio1 = t0 / r0 * 100, t1 / r1 * 100
        contribution = sign * (ratio1 - ratio0)
        itemised += contribution
        items.append(
            {
                "line": label,
                "pct_of_revenue_start": an._s(ratio0.quantize(Decimal("0.01"))),
                "pct_of_revenue_end": an._s(ratio1.quantize(Decimal("0.01"))),
                "contribution_pp": an._s(contribution.quantize(Decimal("0.01"))),
                "evidence_ids": [*ids0, *ids1],
            }
        )
    items.sort(key=lambda x: -abs(Decimal(x["contribution_pp"])))
    m0, m1 = net[start].value / r0 * 100, net[end].value / r1 * 100
    change = m1 - m0
    return {
        "available": True,
        "basis": basis,
        "start_fy": rd.fiscal_year(start),
        "end_fy": rd.fiscal_year(end),
        "net_margin_start": an._s(m0.quantize(Decimal("0.01"))),
        "net_margin_end": an._s(m1.quantize(Decimal("0.01"))),
        "change_pp": an._s(change.quantize(Decimal("0.01"))),
        "items": items,
        "not_itemised_pp": an._s((change - itemised).quantize(Decimal("0.01"))),
        "evidence_ids": [rev[start].id, rev[end].id, net[start].id, net[end].id],
        "note": "a contribution is the change in that line as a share of revenue, signed by its "
        "effect "
        "on the margin; 'not itemised' is the part of the change no listed line explains "
        "(exceptional items, associates, minority interests, lines not printed)",
    }


# ---------------------------------------------------------------- peers


def peer_growth(own_store, peer_stores, m: Metric, basis: str) -> dict:
    block = industry._metric_block(own_store, peer_stores, m, [basis])
    if not block.get("available"):
        return {"available": False, "reason": block.get("reason")}
    if not block.get("peer_benchmark"):
        return {
            "available": True,
            "peer_benchmark": False,
            "reason": block.get("peer_reason"),
            "company": block.get("company"),
        }
    return {
        "available": True,
        "peer_benchmark": True,
        "benchmark": "PEER_GROUP",
        "company": block["company"],
        "peer_group_median_cagr": block["peer_median_cagr"],
        "peer_group_combined_cagr": block["peer_aggregate_cagr"],
        "difference_vs_peer_median_pp": block["difference_vs_median_pp"],
        "difference_vs_peer_combined_pp": block["difference_vs_aggregate_pp"],
        "peer_count": block["peer_count"],
        "peers": block["peers"],
    }


def peer_margin(own_store, peer_stores, m: Metric, basis: str, own: dict) -> dict:
    """The company's latest margin and its change against the peer group, same years and basis."""
    if not own.get("available") or not own.get("series"):
        return {"available": False, "reason": own.get("reason", "the company has no stored margin")}
    own_recs = _on_basis(own_store, m.names, basis)
    latest = max(own_recs)
    rows = []
    for company, store in peer_stores:
        recs = _on_basis(store, m.names, basis)
        if latest in recs:
            rows.append((company.common_name, recs))
    if len(rows) < MIN_PEERS:
        return {
            "available": False,
            "reason": f"fewer than {MIN_PEERS} peers have a {m.label.lower()} for "
            f"{rd.fiscal_year(latest)} on the {basis.lower()} basis",
            "peers_with_value": len(rows),
        }
    values = [recs[latest].value for _, recs in rows]
    med = Decimal(str(median(values)))
    mine = own_recs[latest].value
    out = {
        "available": True,
        "benchmark": "PEER_GROUP",
        "fy": rd.fiscal_year(latest),
        "company": an._s(mine.quantize(Decimal("0.01"))),
        "peer_group_median": an._s(med.quantize(Decimal("0.01"))),
        "difference_pp": an._s((mine - med).quantize(Decimal("0.01"))),
        "peers_above": sum(1 for v in values if v > mine),
        "peers_below": sum(1 for v in values if v < mine),
        "peer_count": len(values),
        "evidence_ids": [own_recs[latest].id],
    }
    # the change over the same window, for the peers that have both end points
    first = min(own_recs)
    if first != latest:
        changes = [recs[latest].value - recs[first].value for _, recs in rows if first in recs]
        if len(changes) >= MIN_PEERS:
            mine_change = mine - own_recs[first].value
            med_change = Decimal(str(median(changes)))
            out["window"] = {
                "from_fy": rd.fiscal_year(first),
                "to_fy": rd.fiscal_year(latest),
                "company_change_pp": an._s(mine_change.quantize(Decimal("0.01"))),
                "peer_group_median_change_pp": an._s(med_change.quantize(Decimal("0.01"))),
                "difference_pp": an._s((mine_change - med_change).quantize(Decimal("0.01"))),
                "peer_count": len(changes),
            }
    return out


# ---------------------------------------------------------------- the whole


def build(own_store, peer_stores, sector_code: str, basis: str) -> dict:
    """The growth and profitability analysis on one accounting basis."""
    bank = sector_code == "BANK"
    growth_defs = GROWTH_BANK if bank else GROWTH_NONBANK
    margin_defs = MARGINS_BANK if bank else MARGINS_NONBANK
    growth = {m.key: growth_metric(own_store, m, basis) for m in growth_defs}
    margins = {m.key: margin_metric(own_store, m, basis) for m in margin_defs}
    peers_growth = {m.key: peer_growth(own_store, peer_stores, m, basis) for m in growth_defs}
    peers_margin = {
        m.key: peer_margin(own_store, peer_stores, m, basis, margins[m.key]) for m in margin_defs
    }
    has_content = any(g.get("available") for g in (*growth.values(), *margins.values()))
    return {
        "version": VERSION,
        "available": has_content,
        "basis": basis,
        "kind": "BANK" if bank else "NON_FINANCIAL",
        "growth": growth,
        "margins": margins,
        "observations": observations(margins),
        "drivers": None if bank else drivers(own_store, basis),
        "peer_growth": peers_growth,
        "peer_margins": peers_margin,
        "benchmark": {
            "kind": "PEER_GROUP",
            "label": "peer group",
            "description": "the same-industry listed companies closest in size that have stored "
            "filings on the same accounting basis and fiscal years",
            "industry": None,
            "industry_reason": "no verified industry statistics are held, so no industry CAGR is "
            "shown and no growth alpha is calculated; the comparison is with the peer group only",
        },
        "definitions": [
            "CAGR = (ending value / starting value) ^ (1 / years) - 1 on annual figures of one "
            "accounting basis; it needs a positive start and end value.",
            "EBITDA = profit before tax + finance costs + depreciation and amortisation, and "
            "EBIT = "
            "profit before tax + finance costs: both include other income and exceptional items.",
            "Gross profit = revenue from operations less materials consumed, purchases of stock in "
            "trade and the change in inventories, as printed; a services company has none.",
            f"A margin change of less than {STABLE_PP} percentage points over the window is "
            "reported "
            "as 'held'.",
            "Difference vs the peer group = the company's figure minus the peer-group median, in "
            "percentage points. It measures relative performance only; it is not alpha.",
        ],
    }
