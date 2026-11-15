# ruff: noqa: E501
"""Discounted cash flow valuation (FCFF) with bull, base and bear scenarios, for non-financial companies.

What is evidence and what is assumed is never mixed:

* ACTUALS (validated evidence): the last annual revenue, the latest net debt and borrowings, the shares
  outstanding, non-controlling interests and the share price, each with its evidence id.
* ASSUMPTIONS (the assumption ledger, research/assumptions.py): per scenario the first-year revenue
  growth, the long-run growth, the operating margin, the tax rate, capital expenditure, depreciation and
  operating working capital as percentages of revenue, and the discount rate: either entered directly (WACC)
  or built from the cost of equity (CAPM from sourced inputs, or entered), the pre-tax cost of debt and the
  market weights of equity and debt. Each carries its source or author, date and rationale. NOTHING here is a
  built-in default: a scenario missing any input is NOT COMPLETED and says exactly which.
* METHOD (stated, fixed, shown in the report): a five-year explicit forecast; revenue growth fades linearly
  from the entered first-year growth to the long-run growth in year five; the other drivers are held at the
  entered percentages; FCFF = EBIT x (1 - tax) + depreciation - capital expenditure - change in operating
  working capital (working capital / revenue x change in revenue); cash flows arrive at year ends; terminal
  value = FCFF of year six / (WACC - long-run growth) with year six built from the same ratios; enterprise
  value = present value of the five cash flows + present value of the terminal value; equity value =
  enterprise value - net debt - non-controlling interests; value per share = equity value / shares.

A bank is not valued this way. For a bank, debt is raw material and interest is an operating cost, so "net
debt" and free cash flow to the firm have no meaning; the residual-income (justified P/B) method applies.

Results are scenario-implied values under the entered assumptions, valued as at the last fiscal year end of
the revenue used (no roll-forward to today), not target prices. Sensitivity tables are grids of hypotheticals
around the entered base case; their axes are offsets from the entered values, not inputs.
"""

from datetime import date
from decimal import Decimal

from app.research import assumptions as asm
from app.research import profitability as pf
from app.research import readiness as rd
from app.research.analysis import _s
from app.research.sectors import AMOUNT, MULT, Metric

VERSION = "1.0"
HORIZON = 5
HUNDRED = Decimal(100)
TV_SHARE_FLAG = Decimal(75)  # a terminal value above this share of enterprise value is flagged
WACC_OFFSETS = (Decimal(-2), Decimal(-1), Decimal(0), Decimal(1), Decimal(2))
G_OFFSETS = (Decimal(-1), Decimal("-0.5"), Decimal(0), Decimal("0.5"), Decimal(1))
GROWTH_OFFSETS = (Decimal(-4), Decimal(-2), Decimal(0), Decimal(2), Decimal(4))
MARGIN_OFFSETS = (Decimal(-4), Decimal(-2), Decimal(0), Decimal(2), Decimal(4))

REVENUE = pf.REVENUE
NET_DEBT = Metric("net_debt", "Net debt", ("LEVERAGE.net_debt",), AMOUNT, ptype="ANNUAL")
BORROWINGS = Metric(
    "borrowings", "Borrowings", ("LEVERAGE.total_borrowings",), AMOUNT, ptype="ANNUAL"
)
NCI = Metric("nci", "Non-controlling interests", ("BALANCE_SHEET.non_controlling_interests", "BALANCE_SHEET.minority_interest"), AMOUNT, ptype="ANNUAL")  # fmt: skip
SHARES = Metric("shares", "Shares outstanding", ("BALANCE.shares_outstanding",), MULT, ptype="ANY")
PRICE = Metric("price", "Share price", ("PRICE.close",), AMOUNT, ptype="ANY")
NAMES = frozenset({n for m in (REVENUE, NET_DEBT, BORROWINGS, NCI, SHARES, PRICE) for n in m.names})
SCENARIO_INPUTS = ("revenue_growth", "terminal_growth", "operating_margin", "tax_rate", "capex_to_revenue", "depreciation_to_revenue", "working_capital_to_revenue")  # fmt: skip


def _latest(store, m: Metric, basis: str | None, ptypes=("ANNUAL",)):
    best = None
    for name in m.names:
        for r in store.by_metric.get(name, []):
            if r.value is None or r.period_end is None or r.period_type not in ptypes:
                continue
            if basis is not None and r.basis not in (basis, None):
                continue
            if best is None or r.period_end > best.period_end:
                best = r
    return best


def _ref(rec) -> dict | None:
    if rec is None:
        return None
    return {"value": _s(rec.value), "unit": rec.unit, "period_end": rec.period_end.isoformat() if rec.period_end else None, "basis": rec.basis, "evidence_id": rec.id}  # fmt: skip


def actuals(store, basis: str) -> dict:
    return {
        "revenue": _ref(_latest(store, REVENUE, basis)),
        "net_debt": _ref(_latest(store, NET_DEBT, basis)),
        "borrowings": _ref(_latest(store, BORROWINGS, basis)),
        "non_controlling_interests": _ref(_latest(store, NCI, basis)),
        "shares": _ref(_latest(store, SHARES, basis, ("ANNUAL", "QUARTERLY", None))),
        "price": _ref(_latest(store, PRICE, None, ("QUARTERLY", "ANNUAL", None))),
    }


def _d(x) -> Decimal:
    return Decimal(str(x))


# ---------------------------------------------------------------- the model


def project(rev0: Decimal, a: dict, wacc: Decimal) -> dict:
    """The explicit forecast, terminal value and enterprise value for one set of assumptions (percent points)."""
    g1, gT = a["revenue_growth"], a["terminal_growth"]
    margin, tax = a["operating_margin"] / HUNDRED, a["tax_rate"] / HUNDRED
    capex, dep, nwc = a["capex_to_revenue"] / HUNDRED, a["depreciation_to_revenue"] / HUNDRED, a["working_capital_to_revenue"] / HUNDRED  # fmt: skip
    if wacc <= gT:
        raise ValueError("the discount rate must exceed the long-run growth rate")
    rows, prev, pv_sum = [], rev0, Decimal(0)
    for t in range(1, HORIZON + 2):  # year HORIZON + 1 is the terminal-year cash flow
        growth = gT if t == HORIZON + 1 else g1 + (gT - g1) * Decimal(t - 1) / Decimal(HORIZON - 1)
        rev = prev * (1 + growth / HUNDRED)
        ebit = rev * margin
        taxes = max(ebit, Decimal(0)) * tax
        nopat = ebit - taxes
        da, cx, dn = rev * dep, rev * capex, nwc * (rev - prev)
        fcff = nopat + da - cx - dn
        row = {"year": t, "growth_pct": growth, "revenue": rev, "ebit": ebit, "tax": taxes, "nopat": nopat, "depreciation": da, "capex": cx, "change_in_working_capital": dn, "fcff": fcff}  # fmt: skip
        if t <= HORIZON:
            df = (1 + wacc / HUNDRED) ** (-t)
            row["discount_factor"] = df
            row["pv"] = fcff * df
            pv_sum += row["pv"]
        rows.append(row)
        prev = rev
    terminal = rows[HORIZON]
    tv = terminal["fcff"] / ((wacc - gT) / HUNDRED)
    pv_tv = tv * (1 + wacc / HUNDRED) ** (-HORIZON)
    ev = pv_sum + pv_tv
    return {"rows": rows[:HORIZON], "terminal_row": terminal, "terminal_value": tv, "pv_terminal": pv_tv, "pv_explicit": pv_sum, "enterprise_value": ev, "tv_share_pct": (pv_tv / ev * HUNDRED) if ev > 0 else None}  # fmt: skip


def _fmt_rows(rows: list[dict], base_fy: int) -> list[dict]:
    out = []
    for r in rows:
        out.append({"year": r["year"], "fy": f"FY{base_fy + r['year']}", **{k: (_s(v.quantize(Decimal("0.01"))) if k == "growth_pct" else _s(v.quantize(Decimal("1")))) for k, v in r.items() if k not in ("year", "discount_factor")}, **({"discount_factor": _s(r["discount_factor"].quantize(Decimal("0.0001")))} if "discount_factor" in r else {})})  # fmt: skip
    return out


def discount_rate(session, company_id, s: str, inputs: dict, capm: dict, act: dict) -> dict:
    """The discount rate for one scenario: entered directly, or built from entered cost of equity and debt."""
    entered = inputs.get(f"{asm.PREFIX}.wacc_{s}")
    if entered is not None:
        return {"completed": True, "value": _d(entered["value"]), "source": "ENTERED", "label": "Discount rate entered directly", "input_ids": [entered["evidence_id"]], "inputs": [entered]}  # fmt: skip
    missing, parts = [], {}
    ke = inputs.get(f"{asm.PREFIX}.cost_of_equity_{s}")
    if ke is not None:
        parts["ke"] = (_d(ke["value"]), [ke["evidence_id"]], ke)
    elif s == "base" and capm.get("completed"):
        parts["ke"] = (_d(capm["value"]), [capm["evidence_id"], *capm["input_ids"]], {"label": "Cost of equity (CAPM)", "class": "DERIVED_OUTPUT", "stale": bool(capm["stale_inputs"])})  # fmt: skip
    else:
        missing.append(f"cost of equity for the {s} scenario (or an entered discount rate)")
    kd = inputs.get(f"{asm.PREFIX}.cost_of_debt")
    if kd is None:
        missing.append("pre-tax cost of debt (or an entered discount rate)")
    tax = inputs.get(f"{asm.PREFIX}.tax_rate_{s}")
    price, shares, borrowings = act.get("price"), act.get("shares"), act.get("borrowings")
    for label, v in (
        ("share price", price),
        ("shares outstanding", shares),
        ("borrowings", borrowings),
    ):
        if v is None:
            missing.append(f"stored {label}, needed for the market weights of equity and debt")
    if missing or tax is None:
        if tax is None:
            missing.append(f"tax rate for the {s} scenario")
        return {"completed": False, "missing": sorted(set(missing))}
    e = _d(price["value"]) * _d(shares["value"])
    d = _d(borrowings["value"])
    if e + d <= 0:
        return {"completed": False, "missing": ["a positive market value of equity plus debt"]}
    t_ = _d(tax["value"]) / HUNDRED
    wacc = (e * parts["ke"][0] + d * _d(kd["value"]) * (1 - t_)) / (e + d)
    ids = [
        *parts["ke"][1],
        kd["evidence_id"],
        tax["evidence_id"],
        price["evidence_id"],
        shares["evidence_id"],
        borrowings["evidence_id"],
    ]
    return {
        "completed": True,
        "value": wacc,
        "source": "DERIVED",
        "label": "WACC = E/(E+D) x cost of equity + D/(E+D) x pre-tax cost of debt x (1 - tax)",
        "weights": {
            "equity_pct": _s((e / (e + d) * HUNDRED).quantize(Decimal("0.1"))),
            "debt_pct": _s((d / (e + d) * HUNDRED).quantize(Decimal("0.1"))),
        },  # fmt: skip
        "input_ids": sorted(set(ids)),
        "inputs": [parts["ke"][2], kd, tax],
    }


def scenario(
    session,
    company_id,
    s: str,
    inputs: dict,
    capm: dict,
    act: dict,
    base_fy: int,
    price_v: Decimal | None,
) -> dict:
    missing = [
        f"{asm.SPECS[n].label.lower()} for the {s} scenario"
        for n in SCENARIO_INPUTS
        if f"{asm.PREFIX}.{n}_{s}" not in inputs
    ]
    for key, label in (
        ("revenue", "the last annual revenue"),
        ("shares", "shares outstanding"),
        ("net_debt", "net debt"),
    ):
        if act.get(key) is None:
            missing.append(f"stored {label}")
    wacc = discount_rate(session, company_id, s, inputs, capm, act)
    if not wacc["completed"]:
        missing += wacc["missing"]
    if missing:
        return {"completed": False, "missing": sorted(set(missing))}
    a = {n: _d(inputs[f"{asm.PREFIX}.{n}_{s}"]["value"]) for n in SCENARIO_INPUTS}
    if wacc["value"] <= a["terminal_growth"]:
        return {"completed": False, "missing": [], "reason": f"the discount rate {_s(wacc['value'].quantize(Decimal('0.01')))}% does not exceed the long-run growth {_s(a['terminal_growth'])}%; the terminal value is undefined"}  # fmt: skip
    rev0 = _d(act["revenue"]["value"])
    res = project(rev0, a, wacc["value"])
    nci = (
        _d(act["non_controlling_interests"]["value"])
        if act.get("non_controlling_interests")
        else Decimal(0)
    )
    net_debt = _d(act["net_debt"]["value"])
    equity = res["enterprise_value"] - net_debt - nci
    shares = _d(act["shares"]["value"])
    per_share = equity / shares if shares > 0 else None
    entered = [inputs[f"{asm.PREFIX}.{n}_{s}"] for n in SCENARIO_INPUTS]
    ids = sorted({i["evidence_id"] for i in entered} | set(wacc["input_ids"]) | {act[k]["evidence_id"] for k in ("revenue", "net_debt", "shares") if act.get(k)} | ({act["non_controlling_interests"]["evidence_id"]} if act.get("non_controlling_interests") else set()))  # fmt: skip
    flags = sorted({f"{i['label']} is stale" for i in [*entered, *wacc["inputs"]] if i.get("stale")} | {f"{i['label']} is a market input without a source" for i in [*entered, *wacc["inputs"]] if i.get("market_input_without_source")})  # fmt: skip
    if res["tv_share_pct"] is not None and res["tv_share_pct"] > TV_SHARE_FLAG:
        flags.append(
            f"the terminal value is {res['tv_share_pct'].quantize(Decimal('1'))}% of enterprise value: most of the value rests on the long-run assumptions"
        )
    if a["capex_to_revenue"] < a["depreciation_to_revenue"]:
        flags.append("capital expenditure is below depreciation, including in the terminal year: the asset base shrinks while revenue grows")  # fmt: skip
    if not act.get("non_controlling_interests"):
        flags.append("non-controlling interests are not stored and are not deducted")
    if act["net_debt"]["period_end"] != act["revenue"]["period_end"]:
        flags.append(f"net debt is as at {act['net_debt']['period_end']} but revenue is for the year ended {act['revenue']['period_end']}")  # fmt: skip
    out = {
        "completed": True,
        "assumptions": {
            n: {
                "value": _s(a[n]),
                "evidence_id": inputs[f"{asm.PREFIX}.{n}_{s}"]["evidence_id"],
                "class": inputs[f"{asm.PREFIX}.{n}_{s}"]["class"],
                "source": inputs[f"{asm.PREFIX}.{n}_{s}"]["source"],
                "author": inputs[f"{asm.PREFIX}.{n}_{s}"]["author"],
                "as_of": inputs[f"{asm.PREFIX}.{n}_{s}"]["as_of"],
                "rationale": inputs[f"{asm.PREFIX}.{n}_{s}"]["rationale"],
            }
            for n in SCENARIO_INPUTS
        },  # fmt: skip
        "discount_rate": {
            "value": _s(wacc["value"].quantize(Decimal("0.01"))),
            "source": wacc["source"],
            "label": wacc["label"],
            "weights": wacc.get("weights"),
            "input_ids": wacc["input_ids"],
        },  # fmt: skip
        "projection": _fmt_rows(res["rows"], base_fy),
        "terminal": {
            "year": f"FY{base_fy + HORIZON + 1}",
            "fcff": _s(res["terminal_row"]["fcff"].quantize(Decimal("1"))),
            "terminal_value": _s(res["terminal_value"].quantize(Decimal("1"))),
            "pv_terminal_value": _s(res["pv_terminal"].quantize(Decimal("1"))),
            "share_of_enterprise_value_pct": _s(res["tv_share_pct"].quantize(Decimal("0.1")))
            if res["tv_share_pct"] is not None
            else None,
        },  # fmt: skip
        "enterprise_value": _s(res["enterprise_value"].quantize(Decimal("1"))),
        "pv_explicit": _s(res["pv_explicit"].quantize(Decimal("1"))),
        "bridge": {
            "enterprise_value": _s(res["enterprise_value"].quantize(Decimal("1"))),
            "net_debt": _s(net_debt.quantize(Decimal("1"))),
            "non_controlling_interests": _s(nci.quantize(Decimal("1"))),
            "equity_value": _s(equity.quantize(Decimal("1"))),
            "shares": _s(shares.quantize(Decimal("1"))),
        },  # fmt: skip
        "evidence_ids": ids,
        "flags": flags,
    }
    if per_share is not None:
        out["value_per_share"] = _s(per_share.quantize(Decimal("1")))
        if price_v:
            out["vs_price_pct"] = _s(((per_share / price_v - 1) * HUNDRED).quantize(Decimal("0.1")))
    return out


def sensitivity(session, company_id, inputs, capm, act, base_fy) -> dict | None:
    """Value per share on grids of hypotheticals around the entered base case."""
    b = scenario(session, company_id, "base", inputs, capm, act, base_fy, None)
    if not b.get("completed") or b.get("value_per_share") is None:
        return None
    a = {n: _d(b["assumptions"][n]["value"]) for n in SCENARIO_INPUTS}
    wacc = _d(b["discount_rate"]["value"])
    rev0, net_debt, shares = (
        _d(act["revenue"]["value"]),
        _d(b["bridge"]["net_debt"]),
        _d(act["shares"]["value"]),
    )
    nci = _d(b["bridge"]["non_controlling_interests"])

    def value(aa, w):
        if w <= aa["terminal_growth"]:
            return None
        ev = project(rev0, aa, w)["enterprise_value"]
        return _s(((ev - net_debt - nci) / shares).quantize(Decimal("1")))

    grid1 = [{"wacc": _s((wacc + dw).quantize(Decimal("0.01"))), "cells": [{"terminal_growth": _s(a["terminal_growth"] + dg), "value_per_share": value({**a, "terminal_growth": a["terminal_growth"] + dg}, wacc + dw)} for dg in G_OFFSETS]} for dw in WACC_OFFSETS]  # fmt: skip
    grid2 = [{"operating_margin": _s(a["operating_margin"] + dm), "cells": [{"revenue_growth": _s(a["revenue_growth"] + dg), "value_per_share": value({**a, "operating_margin": a["operating_margin"] + dm, "revenue_growth": a["revenue_growth"] + dg}, wacc)} for dg in GROWTH_OFFSETS]} for dm in MARGIN_OFFSETS]  # fmt: skip
    return {
        "base_value_per_share": b["value_per_share"],
        "wacc_vs_growth": grid1,
        "margin_vs_growth": grid2,
        "note": "value per share (INR) under the entered base case with one or two inputs offset: rows and columns are offsets from the entered values (WACC and long-run growth in percentage points; operating margin and first-year revenue growth in percentage points), a grid of hypotheticals, not inputs",  # fmt: skip
    }


def build(session, company_id, store, sector, prefer: str, today=None) -> dict:
    """The DCF block for the company, or the reason there is none."""
    if sector.valuation == "BANK":
        return {
            "version": VERSION,
            "applicable": False,
            "kind": "BANK",
            "reason": "a free-cash-flow-to-the-firm DCF is not applied to a bank: its debt (deposits and borrowings) is raw material and its interest is an operating cost, so net debt, enterprise value and free cash flow to the firm have no meaning here; the residual-income (justified P/B) valuation applies",  # fmt: skip
        }
    from app.research import clock

    today = today or clock.today()
    inputs = asm.current_inputs(session, company_id, today)
    capm = asm.capm_cost_of_equity(session, company_id, inputs)
    act = actuals(store, prefer)
    base_fy = (
        int(rd.fiscal_year(date.fromisoformat(act["revenue"]["period_end"]))[2:])
        if act["revenue"]
        else 0
    )
    price_v = _d(act["price"]["value"]) if act["price"] else None
    scenarios = {
        s: scenario(session, company_id, s, inputs, capm, act, base_fy, price_v)
        for s in asm.SCENARIOS
    }
    done = [s for s, v in scenarios.items() if v["completed"]]
    flags = sorted({f for v in scenarios.values() if v["completed"] for f in v["flags"]})
    return {
        "version": VERSION,
        "applicable": True,
        "kind": "FCFF",
        "basis": prefer,
        "completed": "base" in done,
        "scenarios_complete": done,
        "horizon_years": HORIZON,
        "valuation_date_note": "values are as at the last fiscal year end of the revenue used; no roll-forward to today",
        "actuals": act,
        "scenarios": scenarios,
        "sensitivity": sensitivity(session, company_id, inputs, capm, act, base_fy),
        "flags": flags,
        "price": act["price"],
        "method": [
            f"Explicit forecast of {HORIZON} years; revenue growth fades linearly from the entered first-year growth to the entered long-run growth in year {HORIZON}.",
            "Operating margin, tax rate, capital expenditure, depreciation and operating working capital are held at the entered percentages.",
            "FCFF = EBIT x (1 - tax) + depreciation - capital expenditure - change in operating working capital (working capital / revenue x change in revenue). Tax is not charged on a loss.",
            "Discount rate: entered directly, or WACC = E/(E+D) x cost of equity + D/(E+D) x pre-tax cost of debt x (1 - tax), with market weights from the stored share price, shares and borrowings; the cost of equity is entered or, for the base scenario, derived by CAPM from sourced inputs.",
            "Terminal value = FCFF of the year after the forecast / (WACC - long-run growth), built from the same ratios; it must be positive-spread (WACC above growth).",
            "Enterprise value = present value of the explicit cash flows + present value of the terminal value; equity value = enterprise value - net debt - non-controlling interests; value per share = equity value / shares outstanding.",
        ],
        "needs": [f"{asm.SPECS[n].label} (per scenario)" for n in SCENARIO_INPUTS]
        + ["a discount rate (WACC) or a cost of equity and a pre-tax cost of debt"],
        "how_to_enter": "python -m app.research.assumption_cli set --symbol SYMBOL --input NAME --scenario bear|base|bull --value V --as-of YYYY-MM-DD --author NAME --rationale TEXT [--source NAME]; or PUT /api/companies/{id}/valuation-inputs; an edit supersedes the old value and keeps the history",  # fmt: skip
        "assumption_note": "every figure in a scenario is an entered input with its source or author, date and rationale; none is built in. Results are scenario-implied values, not target prices.",
    }
