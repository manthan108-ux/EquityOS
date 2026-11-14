# ruff: noqa: E501
"""Sector-specific operating and financial metrics, connected to the financial analysis.

`analysis["sector_kpis"]` shows, for the company's sector, the indicators that sector is judged on:

* Banking: net interest margin, gross and net NPA, CASA, credit and deposit growth, capital adequacy,
  credit-deposit ratio, cost-to-income, provisions to operating profit.
* IT services: revenue growth, operating (EBIT/EBITDA) margin, employee cost to revenue, attrition,
  utilisation, receivable days.
* FMCG: revenue and volume growth, realisation, gross and EBITDA margin, distribution indicators
  (receivable and inventory days).
* Automobiles: sales volume and its growth, realisation per vehicle, segment (product) mix, margins.
* Energy: revenue growth, EBITDA margin, inventory days, net debt / EBITDA, segment mix, and production,
  throughput and refining margin where the company states them.

Each indicator is a series from validated evidence with its source (REPORTED = stated by the company or a
filing; CALCULATED = built by the calculation engine; DERIVED = computed here from two stored figures, with
the formula shown), a latest value, the change, and the evidence ids. An indicator nothing stored supports
says why ("not stated in the stored documents"); nothing is estimated, and no figure is borrowed from a
different period, basis or definition.

Regulatory and operating disclosures (NIM, GNPA, CASA, capital adequacy, volumes, attrition) are shown as
the company stated them, with the period they relate to; they do not carry a consolidated/standalone basis.

Comparison with the peer group (>= 3 peers, same period) is shown for the indicators peers also have.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.research import analysis as an
from app.research import profitability as pf
from app.research import readiness as rd

VERSION = "1.0"
PCT, MULT, AMOUNT, UNITS, DAYS = "percent", "x", "amount", "units", "days"
MIN_PEERS = pf.MIN_PEERS
LOWER_BETTER = {"gnpa_ratio", "nnpa_ratio", "attrition", "cost_to_income", "provisions_to_ppop", "employee_cost_ratio", "receivable_days", "inventory_days"}  # fmt: skip


@dataclass(frozen=True)
class Kpi:
    key: str
    label: str
    names: tuple[str, ...]
    kind: str
    source: str  # REPORTED | CALCULATED
    group: str
    note: str = ""
    absent: str = "not stated in the stored documents"


R, C = "REPORTED", "CALCULATED"

BANKING = (
    Kpi("nim", "Net interest margin", ("RATIOS.net_interest_margin",), PCT, R, "Profitability"),
    Kpi("gnpa_ratio", "Gross NPA ratio", ("RATIOS.gross_npa_ratio",), PCT, R, "Asset quality"),
    Kpi("nnpa_ratio", "Net NPA ratio", ("RATIOS.net_npa_ratio",), PCT, R, "Asset quality"),
    Kpi("gnpa_amount", "Gross NPA amount", ("RATIOS.gross_npa_amount",), AMOUNT, R, "Asset quality"),
    Kpi("nnpa_amount", "Net NPA amount", ("RATIOS.net_npa_amount",), AMOUNT, R, "Asset quality"),
    Kpi("provisions_to_ppop", "Provisions / operating profit before provisions", ("RATIO.provisions_to_ppop",), PCT, C, "Asset quality"),
    Kpi("casa", "CASA ratio", ("RATIOS.casa_ratio",), PCT, R, "Funding"),
    Kpi("credit_growth", "Credit (advances) growth, year on year", ("GROWTH.advances_yoy",), PCT, C, "Growth"),
    Kpi("deposit_growth", "Deposit growth, year on year", ("GROWTH.deposits_yoy",), PCT, C, "Growth"),
    Kpi("cd_ratio", "Credit-deposit ratio", ("RATIO.advances_to_deposits",), PCT, C, "Funding"),
    Kpi("capital_adequacy", "Capital adequacy ratio", ("RATIOS.capital_adequacy_ratio",), PCT, R, "Capital"),
    Kpi("cost_to_income", "Cost-to-income ratio", ("RATIO.cost_to_income",), PCT, C, "Efficiency"),
)  # fmt: skip
IT = (
    Kpi("revenue_growth", "Revenue growth, year on year", ("GROWTH.revenue_yoy",), PCT, C, "Growth"),
    Kpi("ebit_margin", "Operating margin (EBIT)", ("MARGIN.ebit_margin",), PCT, C, "Margins"),
    Kpi("ebitda_margin", "EBITDA margin", ("MARGIN.ebitda_margin",), PCT, C, "Margins"),
    Kpi("employee_cost_ratio", "Employee cost / revenue", ("MARGIN.employee_cost_ratio",), PCT, C, "People"),
    Kpi("attrition", "Employee attrition", ("OPERATING.attrition",), PCT, R, "People", "voluntary and total attrition are kept apart where both are stated"),
    Kpi("utilisation", "Employee utilisation", ("OPERATING.utilization", "OPERATING.utilisation"), PCT, R, "People", "including and excluding trainees are kept apart where both are stated"),
    Kpi("receivable_days", "Receivable days", ("WORKING_CAPITAL.receivable_days",), DAYS, C, "Collections"),
)  # fmt: skip
FMCG = (
    Kpi("revenue_growth", "Revenue growth, year on year", ("GROWTH.revenue_yoy",), PCT, C, "Growth"),
    Kpi("volume_growth", "Volume growth", ("OPERATING.volume_growth",), PCT, R, "Growth", absent="the company's volume growth is not stated in the stored documents"),  # fmt: skip
    Kpi("gross_margin", "Gross margin", ("MARGIN.gross_margin",), PCT, C, "Margins"),
    Kpi("ebitda_margin", "EBITDA margin", ("MARGIN.ebitda_margin",), PCT, C, "Margins"),
    Kpi("receivable_days", "Receivable days (distribution)", ("WORKING_CAPITAL.receivable_days",), DAYS, C, "Distribution"),
    Kpi("inventory_days", "Inventory days (distribution)", ("WORKING_CAPITAL.inventory_days",), DAYS, C, "Distribution"),
)  # fmt: skip
AUTO = (
    Kpi("revenue_growth", "Revenue growth, year on year", ("GROWTH.revenue_yoy",), PCT, C, "Growth"),
    Kpi("vehicle_volumes", "Sales volume (vehicles)", ("OPERATING.vehicle_volumes",), UNITS, R, "Volumes", "as stated by the company; domestic and export are kept apart where both are stated"),  # fmt: skip
    Kpi("ebitda_margin", "EBITDA margin", ("MARGIN.ebitda_margin",), PCT, C, "Margins"),
    Kpi("ebit_margin", "Operating margin (EBIT)", ("MARGIN.ebit_margin",), PCT, C, "Margins"),
    Kpi("gross_margin", "Gross margin", ("MARGIN.gross_margin",), PCT, C, "Margins"),
    Kpi("inventory_days", "Inventory days", ("WORKING_CAPITAL.inventory_days",), DAYS, C, "Distribution"),
)  # fmt: skip
ENERGY = (
    Kpi("revenue_growth", "Revenue growth, year on year", ("GROWTH.revenue_yoy",), PCT, C, "Growth"),
    Kpi("production", "Production", ("OPERATING.production",), AMOUNT, R, "Operations", absent="production is not stated in the stored documents"),  # fmt: skip
    Kpi("realisation", "Realisation (price received)", ("OPERATING.realisation",), AMOUNT, R, "Operations", absent="realisation is not stated in the stored documents"),  # fmt: skip
    Kpi("refinery_throughput", "Refinery throughput", ("OPERATING.refinery_throughput",), AMOUNT, R, "Operations", absent="refinery throughput is not stated in the stored documents"),  # fmt: skip
    Kpi("grm", "Gross refining margin", ("OPERATING.grm",), AMOUNT, R, "Operations", absent="the gross refining margin is not stated in the stored documents"),  # fmt: skip
    Kpi("ebitda_margin", "EBITDA margin", ("MARGIN.ebitda_margin",), PCT, C, "Margins"),
    Kpi("inventory_days", "Inventory days", ("WORKING_CAPITAL.inventory_days",), DAYS, C, "Operations"),
    Kpi("net_debt_to_ebitda", "Net debt / EBITDA", ("LEVERAGE.net_debt_to_ebitda",), MULT, C, "Balance sheet"),
)  # fmt: skip
SPECS = {"BANK": BANKING, "IT_SERVICES": IT, "FMCG": FMCG, "AUTOMOBILES": AUTO, "ENERGY": ENERGY}
PEER_COMPARED = {"BANK": ("nim", "gnpa_ratio", "nnpa_ratio", "casa", "credit_growth", "capital_adequacy", "cost_to_income"), "IT_SERVICES": ("revenue_growth", "ebit_margin", "employee_cost_ratio"), "FMCG": ("revenue_growth", "gross_margin", "ebitda_margin"), "AUTOMOBILES": ("revenue_growth", "ebitda_margin", "ebit_margin"), "ENERGY": ("revenue_growth", "ebitda_margin", "net_debt_to_ebitda")}  # fmt: skip
REV_NAMES = ("INCOME.revenue_from_operations",)
VOLUME = "OPERATING.vehicle_volumes"

NAMES = frozenset(
    {n for specs in SPECS.values() for k in specs for n in k.names}
    | set(REV_NAMES)
    | {VOLUME, "OPERATING.volume_growth", "OPERATING.production", "OPERATING.realisation"}
)

QUARTER_DAYS = (80, 100)
YEAR_DAYS = (350, 380)


def _decs(kind: str) -> str:
    return "0.1" if kind in (PCT, DAYS, MULT) else "1"


def _by_dimension(store, names, basis: str | None) -> dict:
    """{(dimension, period_type): {period_end: record}} for every stored point of the names."""
    out: dict = {}
    for name in names:
        for r in store.by_metric.get(name, []):
            if r.value is None or r.period_end is None:
                continue
            if basis is not None and r.basis not in (basis, None):
                continue
            slot = out.setdefault((r.dimension or "", r.period_type or ""), {})
            cur = slot.get(r.period_end)
            if cur is None or an._tier(r) < an._tier(cur):
                slot[r.period_end] = r
    return out


def _row(rec, kind: str) -> dict:
    return {
        "period_end": rec.period_end.isoformat(),
        "fy": rd.fiscal_year(rec.period_end),
        "period_type": rec.period_type,
        "value": an._s(rec.value.quantize(Decimal(_decs(kind)))),
        "evidence_id": rec.id,
        "document_id": str(rec.document_id) if rec.document_id else None,
        "page": rec.source_page,
    }


def indicator(store, k: Kpi, basis: str) -> dict:
    groups = _by_dimension(store, k.names, basis)
    if not groups:
        return {"key": k.key, "label": k.label, "group": k.group, "source": k.source, "kind": k.kind, "available": False, "reason": k.absent}  # fmt: skip
    variants = []
    for (dimension, ptype), recs in sorted(
        groups.items(), key=lambda kv: (kv[0][1] != "ANNUAL", kv[0][0], kv[0][1])
    ):
        ends = sorted(recs)[-pf.MAX_YEARS :]
        series = [_row(recs[e], k.kind) for e in ends]
        v = {
            "dimension": dimension or None,
            "period_type": ptype,
            "series": series,
            "latest": series[-1],
        }
        if len(series) >= 2 and k.kind != AMOUNT and k.kind != UNITS:
            first, last = recs[ends[0]], recs[ends[-1]]
            v["change"] = an._s((last.value - first.value).quantize(Decimal(_decs(k.kind))))
            v["change_from"], v["change_to"] = series[0]["fy"] if ptype == "ANNUAL" else series[0]["period_end"], series[-1]["fy"] if ptype == "ANNUAL" else series[-1]["period_end"]  # fmt: skip
            v["change_evidence_ids"] = [first.id, last.id]
        variants.append(v)
    return {"key": k.key, "label": k.label, "group": k.group, "source": k.source, "kind": k.kind, "note": k.note or None, "available": True, "variants": variants}  # fmt: skip


# ---------------------------------------------------------------- derived volume measures


def _prior_year(recs: dict, end):
    for d in recs:
        if 350 <= (end - d).days <= 380:
            return d
    return None


def volume_measures(store, basis: str) -> dict | None:
    """Volume growth and realisation per vehicle from the stated sales volume and reported revenue."""
    groups = {
        k: v for k, v in _by_dimension(store, (VOLUME,), basis).items() if not k[0]
    }  # undimensioned total only
    if not groups:
        return None
    rev = {pt: recs for (_, pt), recs in _by_dimension(store, REV_NAMES, basis).items()}
    growth, realisation = [], []
    for (_, ptype), vols in groups.items():
        for end in sorted(vols):
            prev = _prior_year(vols, end)
            if prev is not None and vols[prev].value > 0:
                growth.append(
                    {
                        "period_end": end.isoformat(),
                        "fy": rd.fiscal_year(end),
                        "period_type": ptype,
                        "value": an._s(
                            ((vols[end].value / vols[prev].value - 1) * 100).quantize(
                                Decimal("0.1")
                            )
                        ),
                        "evidence_ids": [vols[prev].id, vols[end].id],
                    }
                )
            r = rev.get(ptype, {}).get(end)
            if r is not None and vols[end].value > 0:
                realisation.append(
                    {
                        "period_end": end.isoformat(),
                        "fy": rd.fiscal_year(end),
                        "period_type": ptype,
                        "value": an._s((r.value / vols[end].value).quantize(Decimal("1"))),
                        "unit": "inr_per_vehicle",
                        "evidence_ids": [r.id, vols[end].id],
                    }
                )
    if not growth and not realisation:
        return {"available": False, "reason": "sales volume is stored for one period only or without a matching revenue period, so neither growth nor realisation can be calculated"}  # fmt: skip
    return {
        "available": True,
        "volume_growth": sorted(growth, key=lambda r: r["period_end"]),
        "realisation": sorted(realisation, key=lambda r: r["period_end"]),
        "formula": "growth = volume / volume one year earlier - 1; realisation = revenue from operations / total sales volume of the same period (includes non-vehicle revenue, so it is an average revenue per vehicle, not a price)",  # fmt: skip
        "kind": "DERIVED",
    }


# ---------------------------------------------------------------- peers


def peer_compare(own_store, peer_stores, k: Kpi, basis: str, own: dict) -> dict:
    """The company's value against the peer-group median for ONE shared period (>= 3 peers).

    The latest period the company states is tried first; if fewer than three peers state it, up to
    three earlier periods are tried. The period used is always named; nothing is carried across periods.
    """
    from statistics import median

    if not own.get("available"):
        return {"available": False, "reason": "the company has no stored value"}
    variant = next((v for v in own["variants"] if not v["dimension"]), own["variants"][0])
    ptype = variant["period_type"]
    mine_by_end = _by_dimension(own_store, k.names, basis).get(
        (variant["dimension"] or "", ptype), {}
    )
    peer_by_end = [
        _by_dimension(store, k.names, basis).get(("", ptype), {}) for _c, store in peer_stores
    ]
    tried = 0
    for end in sorted(mine_by_end, reverse=True)[:4]:
        values = [recs[end].value for recs in peer_by_end if end in recs]
        tried = max(tried, len(values))
        if len(values) < MIN_PEERS:
            continue
        mine = mine_by_end[end].value
        med = Decimal(str(median(values)))
        q = Decimal(_decs(k.kind))
        return {
            "available": True,
            "benchmark": "PEER_GROUP",
            "period_end": end.isoformat(),
            "company": an._s(mine.quantize(q)),
            "company_evidence_id": mine_by_end[end].id,
            "peer_group_median": an._s(med.quantize(q)),
            "difference": an._s((mine - med).quantize(q)),
            "peer_count": len(values),
            "lower_is_better": k.key in LOWER_BETTER,
            "kind": k.kind,
        }
    return {
        "available": False,
        "reason": f"fewer than {MIN_PEERS} peers state this for any of the company's latest periods",
        "peers_with_value": tried,
    }


def build(
    own_store, peer_stores, sector_code: str, basis: str, segments: dict | None = None
) -> dict:
    specs = SPECS.get(sector_code)
    if not specs:
        return {"available": False, "reason": "no sector-specific indicator set is defined for this sector; the general financial sections apply"}  # fmt: skip
    items = {k.key: indicator(own_store, k, basis) for k in specs}
    volume = volume_measures(own_store, basis) if sector_code == "AUTOMOBILES" else None
    peers = {}
    for key in PEER_COMPARED.get(sector_code, ()):
        k = next(s for s in specs if s.key == key)
        peers[key] = peer_compare(own_store, peer_stores, k, basis, items[key])
    mix = segments if sector_code in ("AUTOMOBILES", "ENERGY", "FMCG", "IT_SERVICES") else None
    available = any(i["available"] for i in items.values()) or bool(
        volume and volume.get("available")
    )
    return {
        "version": VERSION,
        "available": available,
        "sector": sector_code,
        "basis": basis,
        "indicators": items,
        "volume_measures": volume,
        "mix": mix if mix and mix.get("available") else None,
        "peers": peers,
        "benchmark": {"kind": "PEER_GROUP", "label": "peer group", "industry": None},
        "definitions": [
            "REPORTED indicators are shown as the company or its filing stated them, with the period they relate to; they carry no consolidated/standalone basis and are never converted, averaged or carried over from another period.",
            "CALCULATED indicators come from the calculation engine on the report's accounting basis; DERIVED ones are computed in this section from two stored figures, with the formula shown.",
            "An indicator that nothing stored supports is listed with the reason; nothing is estimated.",
            "Comparison with the peer group needs at least three peers stating the same indicator for the same period; for NPA, attrition, cost and days a lower figure is the better one.",
        ],
    }
