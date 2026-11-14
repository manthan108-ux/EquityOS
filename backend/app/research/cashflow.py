# ruff: noqa: E501
"""Cash flow and earnings quality: does reported profit turn into cash?

Built only from validated evidence on one accounting basis; nothing is estimated. Output
(`analysis["cashflow"]`):

* operating cash flow: annual series, year-on-year growth and CAGR;
* CFO / PAT and cash conversion (CFO / EBITDA): by year, the multi-year total, and a reading;
* free cash flow = operating cash flow + capital expenditure, as stored, with FCF margin and FCF / PAT;
* capital expenditure: spend, spend as a share of revenue, of operating cash flow and of depreciation;
* accruals = PAT - CFO and the accrual ratio (accruals / average total assets);
* divergences: years where profit and cash moved apart, with the evidence behind each flag, and the
  working-capital lines the cash flow statement itemises beside the gap;
* the peer-group comparison (>= 3 peers, same fiscal year and basis).

Readings are rules, stated with their thresholds, and marked INTERPRETATION. A figure that cannot be
calculated says why (no capital-expenditure line printed, a loss, a bank, too few years).

Banks are excluded: a bank's operating cash flow is dominated by deposits and advances, so CFO/PAT and
accruals say nothing about earnings quality there.
"""

from decimal import Decimal

from app.research import analysis as an
from app.research import profitability as pf
from app.research import readiness as rd
from app.research.sectors import AMOUNT, MULT, PCT, Metric

VERSION = "1.0"
STRONG = Decimal("1.0")  # CFO at least equal to profit
ADEQUATE = Decimal("0.8")  # CFO within a fifth of profit
WEAK = Decimal("0.5")  # CFO under half of profit
ACCRUAL_ELEVATED = Decimal("5")  # accruals above 5% of average assets
ACCRUAL_HIGH = Decimal("10")
MIN_PEERS = pf.MIN_PEERS

CFO = Metric(
    "operating_cash_flow", "Operating cash flow", ("CASH_FLOW.net_cash_from_operating_activities",),
    AMOUNT, ptype="ANNUAL",
)  # fmt: skip
FCF = Metric("free_cash_flow", "Free cash flow", ("CASH.free_cash_flow",), AMOUNT, ptype="ANNUAL")
CAPEX = Metric(
    "capital_expenditure", "Capital expenditure", ("CASH_FLOW.capital_expenditure",), AMOUNT,
    ptype="ANNUAL",
)  # fmt: skip

RATIOS = (
    Metric(
        "cfo_to_net_profit",
        "Operating cash flow / net profit",
        ("CASH.cfo_to_net_profit",),
        MULT,
        ptype="ANNUAL",
    ),  # fmt: skip
    Metric(
        "cfo_to_ebitda",
        "Operating cash flow / EBITDA",
        ("CASH.cfo_to_ebitda",),
        MULT,
        ptype="ANNUAL",
    ),  # fmt: skip
    Metric(
        "cfo_margin", "Operating cash flow / revenue", ("CASH.cfo_margin",), PCT, ptype="ANNUAL"
    ),
    Metric("fcf_margin", "Free cash flow / revenue", ("CASH.fcf_margin",), PCT, ptype="ANNUAL"),
    Metric(
        "fcf_to_net_profit",
        "Free cash flow / net profit",
        ("CASH.fcf_to_net_profit",),
        MULT,
        ptype="ANNUAL",
    ),  # fmt: skip
    Metric(
        "capex_to_revenue",
        "Capital expenditure / revenue",
        ("CASH.capex_to_revenue",),
        PCT,
        ptype="ANNUAL",
    ),  # fmt: skip
    Metric(
        "capex_to_cfo",
        "Capital expenditure / operating cash flow",
        ("CASH.capex_to_cfo",),
        PCT,
        ptype="ANNUAL",
    ),  # fmt: skip
    Metric(
        "capex_to_depreciation",
        "Capital expenditure / depreciation",
        ("CASH.capex_to_depreciation",),
        MULT,
        ptype="ANNUAL",
    ),  # fmt: skip
    Metric(
        "accrual_ratio",
        "Accrual ratio (of average assets)",
        ("ACCRUAL.accrual_ratio",),
        PCT,
        ptype="ANNUAL",
    ),  # fmt: skip
)
BY_KEY = {m.key: m for m in RATIOS}
PEER_COMPARED = ("cfo_to_net_profit", "cfo_margin", "fcf_margin", "capex_to_revenue")
ACCRUALS = Metric("accruals", "Accruals (net profit - operating cash flow)", ("ACCRUAL.accruals",), AMOUNT, ptype="ANNUAL")  # fmt: skip
PAT = pf.PAT
REVENUE = pf.REVENUE

WORKING_CAPITAL_LINES = (
    ("Trade receivables", "CASH_FLOW.change_in_trade_receivables"),
    ("Inventories", "CASH_FLOW.change_in_inventories"),
    ("Trade payables", "CASH_FLOW.change_in_trade_payables"),
    ("Income taxes paid", "CASH_FLOW.income_taxes_paid"),
    ("Depreciation and amortisation (non-cash)", "CASH_FLOW.depreciation_and_amortisation"),
)

NAMES = frozenset(
    {n for m in (CFO, FCF, CAPEX, ACCRUALS, PAT, REVENUE, *RATIOS) for n in m.names}
    | {n for _, n in WORKING_CAPITAL_LINES}
    | {"INCOME.attributable_profit"}
)


def _recs(store, m: Metric, basis: str) -> dict:
    return pf._on_basis(store, m.names, basis)


def _row(rec, **extra) -> dict:
    return {
        "period_end": rec.period_end.isoformat(),
        "fy": rd.fiscal_year(rec.period_end),
        "value": an._s(rec.value),
        "unit": rec.unit,
        "evidence_id": rec.id,
        **extra,
    }


def _flow(store, m: Metric, basis: str, *, spend: bool = False) -> dict:
    """An annual amount series; `spend` shows a stored negative outflow as a positive spend."""
    recs = _recs(store, m, basis)
    ends = sorted(recs)[-pf.MAX_YEARS :]
    if not ends:
        return {
            "label": m.label,
            "available": False,
            "reason": f"no annual {m.label.lower()} is stored on the {basis.lower()} basis "
            "(the company does not print the line or it is not yet read)",
        }
    series = []
    for i, end in enumerate(ends):
        rec = recs[end]
        v = -rec.value if spend else rec.value
        row = _row(rec, value=an._s(v), yoy_pct=None)
        if i and pf._consecutive(ends[i - 1], end):
            prev = -recs[ends[i - 1]].value if spend else recs[ends[i - 1]].value
            if prev > 0:
                row["yoy_pct"] = an._s(((v / prev - 1) * 100).quantize(Decimal("0.1")))
        series.append(row)
    out: dict = {"label": m.label, "available": True, "basis": basis, "series": series}
    if len(ends) >= 2 and not spend:
        out["cagr"] = pf._cagr_block(recs, ends[0], ends[-1])
        if out["cagr"] is None:
            out["cagr_reason"] = (
                "the CAGR is undefined: the first or last value is zero or negative"
            )
    return out


def _ratio(store, m: Metric, basis: str) -> dict:
    recs = _recs(store, m, basis)
    ends = sorted(recs)[-pf.MAX_YEARS :]
    if not ends:
        return {
            "label": m.label,
            "available": False,
            "kind": m.kind,
            "reason": f"{m.label.lower()} is not calculated on the {basis.lower()} basis "
            "(an input line is missing, or the denominator is zero or negative)",
        }
    q = Decimal("0.01")
    return {
        "label": m.label,
        "available": True,
        "kind": m.kind,
        "series": [_row(recs[e], value=an._s(recs[e].value.quantize(q))) for e in ends],
    }


def reading_for_conversion(ratio: Decimal) -> str:
    if ratio >= STRONG:
        return "STRONG"
    if ratio >= ADEQUATE:
        return "ADEQUATE"
    if ratio >= WEAK:
        return "WEAK"
    return "POOR"


def reading_for_accruals(ratio: Decimal) -> str:
    if ratio <= 0:
        return "CASH_BACKED"
    if ratio < ACCRUAL_ELEVATED:
        return "NORMAL"
    if ratio < ACCRUAL_HIGH:
        return "ELEVATED"
    return "HIGH"


def _latest_run(years: list) -> list:
    """The most recent unbroken run of consecutive fiscal years (a total over a gap is not a total)."""
    run: list = []
    for end in reversed(years):
        if run and not pf._consecutive(end, run[0]):
            break
        run.insert(0, end)
    return run


def other_basis_check(store, basis: str, own: dict) -> dict | None:
    """The conversion on the other accounting basis, shown for reference when the readings differ.

    A consolidated operating cash flow includes subsidiaries; a finance arm's loan disbursements are an
    operating outflow there, which can make a profitable manufacturer look cash-poor. The other basis is
    never substituted: it is shown beside the report's basis, labelled, only when the readings differ.
    """
    other = "STANDALONE" if basis == "CONSOLIDATED" else "CONSOLIDATED"
    alt = conversion(store, other)
    mine = (own or {}).get("cumulative")
    theirs = (alt or {}).get("cumulative") if alt.get("available") else None
    if not mine or not theirs or mine["reading"] == theirs["reading"]:
        return None
    return {
        "basis": other,
        "from_fy": theirs["from_fy"],
        "to_fy": theirs["to_fy"],
        "ratio": theirs["ratio"],
        "reading": theirs["reading"],
        "evidence_ids": theirs["evidence_ids"],
        "note": f"On the {other.lower()} basis the same measure reads {theirs['reading'].lower()} "
        f"({theirs['ratio']}x) against {mine['reading'].lower()} ({mine['ratio']}x) on the "
        f"{basis.lower()} basis. A consolidated operating cash flow includes subsidiaries (a lending "
        "arm's loan disbursements, for example, are an operating outflow), so the two can differ "
        "widely. The report's basis is unchanged; this is shown for reference.",
    }


def conversion(store, basis: str) -> dict:
    """CFO against PAT, year by year and over the years where both exist."""
    cfo, pat = _recs(store, CFO, basis), _recs(store, PAT, basis)
    years = sorted(set(cfo) & set(pat))[-pf.MAX_YEARS :]
    if not years:
        return {
            "available": False,
            "reason": "operating cash flow and net profit are not both stored for any year on this basis",
        }
    rows = []
    for end in years:
        p, c = pat[end], cfo[end]
        row = {
            "fy": rd.fiscal_year(end),
            "period_end": end.isoformat(),
            "pat": an._s(p.value),
            "cfo": an._s(c.value),
            "gap": an._s(p.value - c.value),
            "ratio": None,
            "reading": None,
            "evidence_ids": [p.id, c.id],
        }
        if p.value > 0:
            ratio = (c.value / p.value).quantize(Decimal("0.01"))
            row["ratio"] = an._s(ratio)
            row["reading"] = reading_for_conversion(ratio)
        else:
            row["note"] = "net profit is zero or a loss, so no ratio is meaningful"
        rows.append(row)
    run = _latest_run(years)
    total_p = sum(pat[e].value for e in run)
    total_c = sum(cfo[e].value for e in run)
    out = {
        "available": True,
        "basis": basis,
        "years": rows,
        "rules": f"CFO/PAT >= {STRONG}: strong; >= {ADEQUATE}: adequate; >= {WEAK}: weak; below: poor",
    }
    if len(run) >= 2 and total_p > 0:
        r = (total_c / total_p).quantize(Decimal("0.01"))
        out["cumulative"] = {
            "from_fy": rd.fiscal_year(run[0]),
            "to_fy": rd.fiscal_year(run[-1]),
            "years": len(run),
            "pat": an._s(total_p),
            "cfo": an._s(total_c),
            "ratio": an._s(r),
            "reading": reading_for_conversion(r),
            "evidence_ids": [pat[e].id for e in run] + [cfo[e].id for e in run],
        }
    return out


def accruals(store, basis: str) -> dict:
    amount = _flow(store, ACCRUALS, basis)
    ratio = _ratio(store, BY_KEY["accrual_ratio"], basis)
    if ratio["available"]:
        for row in ratio["series"]:
            row["reading"] = reading_for_accruals(Decimal(row["value"]))
        ratio["rules"] = (
            f"accrual ratio <= 0: cash-backed; below {ACCRUAL_ELEVATED}%: normal; below "
            f"{ACCRUAL_HIGH}%: elevated; above: high. Profit that exceeds cash by a growing share of "
            "assets is lower quality"
        )
    return {"amount": amount, "ratio": ratio}


def divergences(store, basis: str) -> list[dict]:
    """Years where reported profit and cash generation moved apart, each with its evidence."""
    cfo, pat = _recs(store, CFO, basis), _recs(store, PAT, basis)
    years = sorted(set(cfo) & set(pat))
    out = []
    for i, end in enumerate(years):
        p, c = pat[end], cfo[end]
        fy = rd.fiscal_year(end)
        ids = [p.id, c.id]
        if p.value > 0 and c.value <= 0:
            out.append(
                {
                    "fy": fy,
                    "kind": "PROFIT_WITHOUT_CASH",
                    "text": f"{fy}: net profit was positive but operating cash flow was zero or negative.",
                    "evidence_ids": ids,
                }
            )
        elif p.value > 0 and c.value / p.value < WEAK:
            out.append(
                {
                    "fy": fy,
                    "kind": "CASH_LAGS_PROFIT",
                    "text": f"{fy}: operating cash flow was {(c.value / p.value * 100):.0f}% of net profit "
                    f"(below the {WEAK * 100:.0f}% mark).",
                    "evidence_ids": ids,
                }
            )
        if i and pf._consecutive(years[i - 1], end):
            pp, pc = pat[years[i - 1]], cfo[years[i - 1]]
            if p.value > pp.value > 0 and c.value < pc.value:
                out.append(
                    {
                        "fy": fy,
                        "kind": "DIVERGING_TRENDS",
                        "text": f"{fy}: net profit rose while operating cash flow fell.",
                        "evidence_ids": [*ids, pp.id, pc.id],
                    }
                )
            elif p.value < pp.value and pp.value > 0 and c.value > pc.value and pc.value > 0:
                out.append(
                    {
                        "fy": fy,
                        "kind": "CASH_OUTPACES_PROFIT",
                        "text": f"{fy}: operating cash flow rose while net profit fell.",
                        "evidence_ids": [*ids, pp.id, pc.id],
                    }
                )
    return out


def working_capital(store, basis: str) -> dict | None:
    """The cash flow statement's own working-capital and non-cash lines for the latest year."""
    cfo = _recs(store, CFO, basis)
    if not cfo:
        return None
    end = max(cfo)
    items = []
    for label, name in WORKING_CAPITAL_LINES:
        rec = _recs(store, Metric("x", label, (name,), AMOUNT, ptype="ANNUAL"), basis).get(end)
        if rec is not None:
            items.append({"line": label, **_row(rec)})
    if not items:
        return None
    return {
        "fy": rd.fiscal_year(end),
        "items": items,
        "note": "signed as printed in the cash flow statement (a negative figure absorbed cash); "
        "the lines are those the statement itemises and need not add up to the gap",
        "evidence_ids": [i["evidence_id"] for i in items],
    }


def observations(conv: dict, acc: dict, fcf: dict, capex_dep: dict) -> list[dict]:
    out = []
    cum = conv.get("cumulative") if conv.get("available") else None
    if cum:
        pct = (Decimal(cum["ratio"]) * 100).quantize(Decimal("1"))
        out.append(
            {
                "key": "cumulative_conversion",
                "text": f"Over {cum['from_fy']} to {cum['to_fy']} operating cash flow was {pct}% of net "
                f"profit ({cum['reading'].lower()} cash conversion).",
                "kind": "INTERPRETATION",
                "rule": conv["rules"],
                "evidence_ids": cum["evidence_ids"],
            }
        )
    ratio = acc["ratio"]
    if ratio.get("available") and ratio["series"]:
        last = ratio["series"][-1]
        out.append(
            {
                "key": "accruals",
                "text": f"The {last['fy']} accrual ratio was {Decimal(last['value'])}% of average "
                f"assets ({last['reading'].lower().replace('_', '-')}).",
                "kind": "INTERPRETATION",
                "rule": ratio["rules"],
                "evidence_ids": [last["evidence_id"]],
            }
        )
    if fcf.get("available") and fcf["series"]:
        positive = sum(1 for r in fcf["series"] if Decimal(r["value"]) > 0)
        out.append(
            {
                "key": "free_cash_flow",
                "text": f"Free cash flow was positive in {positive} of {len(fcf['series'])} years with "
                "a stored capital-expenditure line.",
                "kind": "INTERPRETATION",
                "rule": "free cash flow = operating cash flow + capital expenditure",
                "evidence_ids": [r["evidence_id"] for r in fcf["series"]],
            }
        )
    if capex_dep.get("available") and capex_dep["series"]:
        last = capex_dep["series"][-1]
        v = Decimal(last["value"])
        word = "above" if v > 1 else "below"
        out.append(
            {
                "key": "capex_vs_depreciation",
                "text": f"{last['fy']} capital expenditure was {v}x depreciation, {word} the level that "
                "only replaces the asset base.",
                "kind": "INTERPRETATION",
                "rule": "capital expenditure above depreciation grows the asset base; below it shrinks "
                "it unless assets are leased",
                "evidence_ids": [last["evidence_id"]],
            }
        )
    return out


# ---------------------------------------------------------------- peers


def peer_ratio(own_store, peer_stores, key: str, basis: str, own: dict) -> dict:
    block = pf.peer_margin(own_store, peer_stores, BY_KEY[key], basis, own)
    if block.get("available"):
        block["kind"] = BY_KEY[key].kind
    return block


def build(own_store, peer_stores, sector_code: str, basis: str) -> dict:
    if sector_code == "BANK":
        return {
            "version": VERSION,
            "available": False,
            "kind": "BANK",
            "reason": "a bank's operating cash flow is dominated by deposits, advances and investments, "
            "so cash conversion, free cash flow and accruals do not measure its earnings quality; the "
            "bank's asset quality and capital sections are the relevant tests",
        }
    cfo = _flow(own_store, CFO, basis)
    fcf = _flow(own_store, FCF, basis)
    capex = _flow(own_store, CAPEX, basis, spend=True)
    ratios = {m.key: _ratio(own_store, m, basis) for m in RATIOS}
    conv = conversion(own_store, basis)
    acc = accruals(own_store, basis)
    peers = {k: peer_ratio(own_store, peer_stores, k, basis, ratios[k]) for k in PEER_COMPARED}
    has_content = cfo["available"] or fcf["available"] or capex["available"]
    return {
        "version": VERSION,
        "available": bool(has_content),
        "kind": "NON_FINANCIAL",
        "basis": basis,
        "operating_cash_flow": cfo,
        "free_cash_flow": fcf,
        "capital_expenditure": capex,
        "ratios": ratios,
        "conversion": conv,
        "other_basis": other_basis_check(own_store, basis, conv),
        "accruals": acc,
        "divergences": divergences(own_store, basis),
        "working_capital": working_capital(own_store, basis),
        "observations": observations(conv, acc, fcf, ratios["capex_to_depreciation"]),
        "peers": peers,
        "benchmark": {
            "kind": "PEER_GROUP",
            "label": "peer group",
            "industry": None,
            "description": "the same-industry listed companies closest in size that have stored "
            "filings on the same accounting basis and fiscal years",
        },
        "definitions": [
            "Free cash flow = operating cash flow + capital expenditure (stored as an outflow). It is "
            "calculated only where the company's cash flow statement prints a capital-expenditure line.",
            "Capital expenditure = purchase of property, plant and equipment, fixed assets and "
            "intangibles as printed; investments, acquisitions and leases are not included.",
            "CFO / PAT uses net profit attributable to owners; it is not calculated when profit is "
            "zero or a loss.",
            "Cash conversion = operating cash flow / EBITDA, where EBITDA = profit before tax + finance "
            "costs + depreciation and amortisation.",
            "Accruals = net profit - operating cash flow; the accrual ratio divides it by average total "
            "assets (closing assets where no opening balance sheet is stored).",
            "Readings are rules of thumb, stated with their thresholds; they describe the evidence and "
            "are not conclusions about the company's accounting.",
        ],
    }
