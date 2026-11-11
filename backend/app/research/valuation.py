"""Valuation analysis (report phase 5): historical, peer, intrinsic and scenario views.

Honesty rules:
* Every input is stored evidence. A model whose inputs are missing is listed as NOT COMPLETED with the
  reason; nothing is filled with a default.
* The only things that are not evidence are the SCENARIO ASSUMPTIONS (cost of equity, long-run growth,
  ROE shifts). They are EquityOS's illustrative defaults, printed next to every result and labelled
  ASSUMPTION. No risk-free rate or beta is stored, so no figure is presented as the "correct" discount
  rate.
* Results are scenario-implied values under stated assumptions, not target prices.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from statistics import median, quantiles

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EodPrice, PriceAdjustment, Security
from app.research import readiness as rd
from app.research.analysis import _records, _s, dec, series_for
from app.research.sectors import Metric, Sector

VERSION = "1.0"

# Illustrative scenario assumptions (EquityOS defaults, not market data). Percent points.
SCENARIOS = {
    "bear": {"roe_shift": Decimal("-2"), "cost_of_equity": Decimal("13"), "growth": Decimal("5")},
    "base": {"roe_shift": Decimal("0"), "cost_of_equity": Decimal("12"), "growth": Decimal("6")},
    "bull": {"roe_shift": Decimal("2"), "cost_of_equity": Decimal("11"), "growth": Decimal("7")},
}
SENS_ROE_SHIFTS = (Decimal("-2"), Decimal("0"), Decimal("2"), Decimal("4"))
SENS_COE = (Decimal("11"), Decimal("12"), Decimal("13"), Decimal("14"))
ASSUMPTION_NOTE = (
    "Cost of equity, long-run growth and the ROE shifts are illustrative EquityOS assumptions, not "
    "market data or forecasts; no risk-free rate or beta is stored. Results are scenario-implied "
    "values under those assumptions, not target prices."
)

PE = Metric("pe", "P/E", ("VALUATION.pe_annual", "VALUATION.pe_ttm"), "x", ptype="ANY")
PB = Metric("pb", "P/B", ("VALUATION.price_to_book",), "x", ptype="ANY")
PRICE = Metric("price", "Share price", ("PRICE.close",), "inr_per_share", ptype="ANY")


def _latest(store: rd._Store, names: tuple[str, ...], ptypes=("QUARTERLY", "ANNUAL", None)):
    best = None
    for n in names:
        for r in store.by_metric.get(n, []):
            if r.value is None or r.period_end is None or r.period_type not in ptypes:
                continue
            if best is None or r.period_end > best.period_end:
                best = r
    return best


def _ref(rec) -> dict | None:
    if rec is None:
        return None
    return {
        "value": _s(rec.value),
        "unit": rec.unit,
        "period_end": rec.period_end.isoformat() if rec.period_end else None,
        "basis": rec.basis,
        "evidence_id": rec.id,
    }


def _justified_pb(roe: Decimal, coe: Decimal, g: Decimal) -> Decimal | None:
    if coe <= g:
        return None
    return (roe - g) / (coe - g)


def _implied_roe(pb: Decimal, coe: Decimal, g: Decimal) -> Decimal:
    return g + pb * (coe - g)


# ---------------------------------------------------------------- historical


def _material(a) -> bool:
    """An adjustment that changes per-share comparability: applied, a split/bonus, or a flagged gap."""
    return (
        a.status.value == "APPLIED"
        or a.kind in ("BONUS", "SPLIT", "SPLIT_OR_BONUS")
        or a.confidence == "PRICE_GAP_ONLY"
    )


def historical_pe(session: Session, company_id: uuid.UUID, store: rd._Store, prefer: str) -> dict:
    eps_metric = Metric("eps", "EPS", ("INCOME.eps_basic",), "per_share")
    pts, basis, note = series_for(store, eps_metric, prefer, "ANNUAL", 5)
    if not pts:
        return {"available": False, "reason": "no annual EPS is stored"}
    secs = list(session.scalars(select(Security).where(Security.company_id == company_id)))
    sec_ids = [s.id for s in secs]
    adj = list(
        session.scalars(select(PriceAdjustment).where(PriceAdjustment.security_id.in_(sec_ids)))
    )
    nse_first = sorted(secs, key=lambda s: (s.security_code is not None, s.id))
    rows, skipped = [], []
    for p in pts:
        fy_end = date.fromisoformat(p["period_end"])
        eps = dec(p["value"])
        if eps is None or eps <= 0:
            skipped.append({"fy": p["fy"], "reason": "EPS is not positive"})
            continue
        if any(a.ex_date > fy_end and _material(a) for a in adj):
            skipped.append(
                {
                    "fy": p["fy"],
                    "reason": "a corporate-action adjustment or flagged price gap falls after this date; per-share figures would not be comparable",
                }
            )
            continue
        px = None
        for s in nse_first:
            px = session.scalar(
                select(EodPrice)
                .where(
                    EodPrice.security_id == s.id,
                    EodPrice.trade_date <= fy_end,
                    EodPrice.trade_date >= fy_end - timedelta(days=7),
                    EodPrice.close.is_not(None),
                )
                .order_by(EodPrice.trade_date.desc())
                .limit(1)
            )
            if px is not None:
                break
        if px is None:
            skipped.append(
                {"fy": p["fy"], "reason": "no stored price within 7 days of the year end"}
            )
            continue
        rows.append(
            {
                "fy": p["fy"],
                "price_date": px.trade_date.isoformat(),
                "price": _s(px.close),
                "eps": p["value"],
                "pe": _s(px.close / eps),
                "eps_evidence_id": p["evidence_id"],
            }
        )
    out = {
        "available": bool(rows),
        "basis": basis,
        "basis_note": note,
        "rows": rows,
        "skipped": skipped,
        "formula": "P/E at year end = closing price on or just before the year end / that year's basic EPS",
    }
    if not rows:
        out["reason"] = "no year could be computed"
    return out


# ---------------------------------------------------------------- peer valuation


def _find(rows: list[dict], key: str) -> dict | None:
    return next((r for r in rows if r["key"] == key), None)


def peer_valuation(peer_rows: list[dict]) -> dict:
    out = []
    for key in ("pe", "pb"):
        r = _find(peer_rows, key)
        if not r:
            continue
        own = dec(r["company"]["value"])
        item = {
            "key": key,
            "label": r["label"],
            "company": r["company"]["value"],
            "as_of": r["company"]["period_end"],
            "n_comparable": r["n_comparable"],
            "peers": [p for p in r["peers"] if p.get("comparable")],
        }
        if r.get("median"):
            med = dec(r["median"])
            item["median"] = r["median"]
            if med and med != 0 and own is not None:
                item["premium_pct"] = _s((own / med - 1) * 100)
        else:
            item["note"] = r.get("note")
        out.append(item)
    return {
        "rows": out,
        "note": (
            "A premium or discount is only a measured gap to the peer median. Why it exists is not "
            "established from the evidence held; see the ROE comparison for banks."
        ),
    }


def bank_pb_roe(peer_rows: list[dict]) -> list[dict]:
    """P/B beside ROE for the company and its same-period peers: the pairing the justified-P/B model uses."""
    pb, roe = _find(peer_rows, "pb"), _find(peer_rows, "roe")
    if not pb or not roe:
        return []
    out = [{"name": "This company", "pb": pb["company"]["value"], "roe": roe["company"]["value"]}]
    by_name = {p["name"]: p for p in roe["peers"] if p.get("comparable")}
    for p in pb["peers"]:
        q = by_name.get(p["name"])
        if p.get("comparable") and q:
            out.append({"name": p["name"], "pb": p["value"], "roe": q["value"]})
    return out if len(out) > 1 else []


# ---------------------------------------------------------------- intrinsic and scenarios


def _roe_on_basis(store, sector: Sector, basis):
    m = next(x for x in sector.trend if x.key == "roe")
    recs = _records(store, m.names, "ANNUAL", basis)
    recs = {d: r for d, r in recs.items() if r.basis == basis}
    return recs[max(recs)] if recs else None


def bank_intrinsic(own_store, sector: Sector, prefer: str, price, pb, roe_row) -> dict:
    if pb is None or price is None:
        return {"completed": False, "reason": "price or price-to-book is not stored"}
    if pb.basis is None:
        return {
            "completed": False,
            "reason": "the P/B states no accounting basis, so no ROE can be matched to it",
        }
    roe_rec = _roe_on_basis(own_store, sector, pb.basis)
    if roe_rec is None:
        return {
            "completed": False,
            "reason": f"no annual return on equity is stored on the {pb.basis} basis of the P/B",
        }
    roe_pt = {
        "value": _s(roe_rec.value),
        "period_end": roe_rec.period_end.isoformat(),
        "evidence_id": roe_rec.id,
        "basis": roe_rec.basis,
    }
    roe = dec(roe_pt["value"])
    pbv = dec(pb.value)
    bvps = dec(price.value) / pbv if pbv else None
    if roe is None or not bvps:
        return {"completed": False, "reason": "book value per share cannot be derived"}
    scen = {}
    for name, a in SCENARIOS.items():
        r = roe + a["roe_shift"]
        jpb = _justified_pb(r, a["cost_of_equity"], a["growth"])
        if jpb is None or jpb <= 0:
            scen[name] = {
                "assumptions": {k: _s(v) for k, v in a.items()},
                "roe": _s(r),
                "note": "no positive justified P/B under these assumptions",
            }
            continue
        value = jpb * bvps
        scen[name] = {
            "assumptions": {k: _s(v) for k, v in a.items()},
            "roe": _s(r),
            "justified_pb": _s(jpb.quantize(Decimal("0.01"))),
            "value_per_share": _s(value.quantize(Decimal("1"))),
            "vs_price_pct": _s(((value / dec(price.value) - 1) * 100).quantize(Decimal("0.1"))),
        }
    sens = [
        {
            "cost_of_equity": _s(coe),
            "cells": [
                {
                    "roe": _s(roe + shift),
                    "justified_pb": (
                        _s(j.quantize(Decimal("0.01")))
                        if (j := _justified_pb(roe + shift, coe, SCENARIOS["base"]["growth"]))
                        is not None
                        and j > 0
                        else None
                    ),
                }
                for shift in SENS_ROE_SHIFTS
            ],
        }
        for coe in SENS_COE
    ]
    implied = [
        {
            "cost_of_equity": _s(coe),
            "implied_roe": _s(
                _implied_roe(pbv, coe, SCENARIOS["base"]["growth"]).quantize(Decimal("0.1"))
            ),
        }
        for coe in SENS_COE
    ]
    return {
        "completed": True,
        "method": "justified price-to-book (residual income): P/B = (ROE - g) / (cost of equity - g); value per share = P/B x book value per share",
        "inputs": {
            "roe": {
                "value": _s(roe),
                "period_end": roe_pt["period_end"],
                "evidence_id": roe_pt["evidence_id"],
                "basis": roe_pt["basis"],
            },
            "price": _ref(price),
            "price_to_book": _ref(pb),
            "book_value_per_share": _s(bvps.quantize(Decimal("0.01"))),
            "book_value_note": "price / P/B, so it is on the same basis as the multiple",
        },
        "scenarios": scen,
        "sensitivity": sens,
        "sensitivity_note": f"justified P/B at long-run growth {SCENARIOS['base']['growth']}%; columns are ROE shifts from the latest annual ROE",
        "market_implied": {
            "rows": implied,
            "note": "the ROE the current P/B would need to be justified at each cost of equity (growth at the base value)",
        },
    }


def relative_scenarios(price, own_pe, peer_pe_row: dict | None) -> dict:
    if price is None or own_pe is None:
        return {"completed": False, "reason": "price or P/E is not stored"}
    if not peer_pe_row:
        return {"completed": False, "reason": "no comparable peer P/E"}
    vals = [dec(p["value"]) for p in peer_pe_row["peers"] if p.get("comparable")]
    vals = [v for v in vals if v is not None and v > 0]
    if len(vals) < 3:
        return {
            "completed": False,
            "reason": f"{len(vals)} comparable peer P/E value(s); at least 3 are needed",
        }
    if len(vals) >= 4:
        q1, med, q3 = quantiles(vals, n=4)
        low, mid, high = Decimal(str(q1)), Decimal(str(med)), Decimal(str(q3))
        spread = "lower quartile / median / upper quartile of peer P/E"
    else:
        low, mid, high = min(vals), Decimal(str(median(vals))), max(vals)
        spread = "lowest / median / highest peer P/E (fewer than 4 peers)"
    own = dec(own_pe.value)
    px = dec(price.value)
    out = {}
    for name, pe in (("bear", low), ("base", mid), ("bull", high)):
        value = px * pe / own
        out[name] = {
            "peer_pe": _s(pe.quantize(Decimal("0.01"))),
            "value_per_share": _s(value.quantize(Decimal("1"))),
            "vs_price_pct": _s(((value / px - 1) * 100).quantize(Decimal("0.1"))),
        }
    return {
        "completed": True,
        "method": "relative: value per share = price x (peer P/E / own P/E), i.e. peer P/E applied to the company's own earnings",
        "spread": spread,
        "scenarios": out,
        "inputs": {"price": _ref(price), "pe": _ref(own_pe), "peer_count": len(vals)},
        "assumption": "that peers' P/E is a fair guide to this company; earnings quality and growth differences are not adjusted for",
    }


def build(
    session: Session,
    company_id: uuid.UUID,
    own_store: rd._Store,
    sector: Sector,
    prefer: str,
    peer_rows: list[dict],
    trend_by_key: dict[str, dict],
) -> dict:
    price = _latest(own_store, PRICE.names, ("QUARTERLY", "ANNUAL", None))
    pe = _latest(own_store, PE.names)
    pb = _latest(own_store, PB.names)
    snapshot = {"price": _ref(price), "pe": _ref(pe), "pb": _ref(pb)}
    out = {
        "version": VERSION,
        "snapshot": snapshot,
        "historical": historical_pe(session, company_id, own_store, prefer),
        "peer": peer_valuation(peer_rows),
        "relative": relative_scenarios(price, pe, _find(peer_rows, "pe")),
        "assumption_note": ASSUMPTION_NOTE,
        "not_completed": [],
    }
    if sector.valuation == "BANK":
        out["pb_vs_roe"] = bank_pb_roe(peer_rows)
        out["intrinsic"] = bank_intrinsic(
            own_store, sector, prefer, price, pb, trend_by_key.get("roe")
        )
        out["not_completed"].append(
            "Historical P/B: share counts for past dates are not stored, so past book value per share cannot be derived."
        )
    else:
        out["intrinsic"] = {
            "completed": False,
            "method": "discounted cash flow (FCFF/FCFE)",
            "reason": "a DCF needs explicit multi-year forecasts and a sourced discount rate; the evidence store holds history only, so none is built",
        }
        out["not_completed"].append(
            "Historical P/B and EV-based multiples: past share counts and enterprise-value inputs are not stored."
        )
    return out
