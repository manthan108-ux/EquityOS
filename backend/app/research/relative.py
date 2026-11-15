# ruff: noqa: E501
"""Historical and peer multiples, relative-valuation scenarios, and which valuation methods fit a sector.

Everything here is arithmetic on stored evidence; no multiple, growth rate or discount rate is assumed.

* Historical P/B: at each fiscal year end, (closing price on or just before the date x shares outstanding at
  that date) / owners' equity at that date, on one accounting basis. Price and share count are both as at the
  date, so splits and bonuses after it do not distort the ratio.
* Historical EV/EBITDA: at each year end, (market capitalisation + net debt (+ non-controlling interests)) /
  that year's EBITDA, on one basis, where all of them are stored.
* Historical comparison: the current multiple against the company's own year-end observations (minimum,
  median, maximum, and the share of observations at or below it), only with at least three observations;
  it positions the multiple, it does not call it cheap or dear.
* Relative scenarios: the lower quartile, median and upper quartile (lowest, median, highest with fewer than
  four peers) of the peers' multiple, applied to the company's own earnings, book value or EBITDA; at least
  three comparable peers, same period and basis. Bear/base/bull are these statistics; the only assumption is
  that peers' multiples are a fair guide, which is stated and not adjusted for quality or growth.
* The method plan says, for the company's sector, which methods apply, which were completed, and why any is
  not applicable (EV/EBITDA and FCFF are not used for banks and other lenders; SOTP only where several
  segments report a result).
"""

from datetime import date, timedelta
from decimal import Decimal
from statistics import median, quantiles

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EodPrice, Security
from app.research import analysis as an
from app.research import readiness as rd
from app.research.analysis import _s, dec
from app.research.sectors import AMOUNT, MULT, Metric

VERSION = "1.0"
MIN_PEERS = 3
MIN_HISTORY = 3
PRICE_WINDOW_DAYS = 7
HUNDRED = Decimal(100)
MULTIPLE_OFFSETS = (Decimal(-2), Decimal(-1), Decimal(0), Decimal(1), Decimal(2))
BASE_OFFSETS = (Decimal("-0.1"), Decimal("-0.05"), Decimal(0), Decimal("0.05"), Decimal("0.1"))

EQUITY = Metric("equity", "Owners' equity", ("BALANCE.owners_equity",), AMOUNT, ptype="ANNUAL")
SHARES = Metric(
    "shares", "Shares outstanding", ("BALANCE.shares_outstanding",), MULT, ptype="ANNUAL"
)
NET_DEBT = Metric("net_debt", "Net debt", ("LEVERAGE.net_debt",), AMOUNT, ptype="ANNUAL")
NCI = Metric("nci", "Non-controlling interests", ("BALANCE_SHEET.non_controlling_interests", "BALANCE_SHEET.minority_interest"), AMOUNT, ptype="ANNUAL")  # fmt: skip
EBITDA = Metric("ebitda", "EBITDA", ("INCOME.ebitda",), AMOUNT, ptype="ANNUAL")
NAMES = frozenset({n for m in (EQUITY, SHARES, NET_DEBT, NCI, EBITDA) for n in m.names} | {"VALUATION.enterprise_value", "VALUATION.ev_to_ebitda", "VALUATION.price_to_book", "VALUATION.pe_annual", "VALUATION.pe_ttm"})  # fmt: skip


def _on_basis(store, m: Metric, basis: str) -> dict:
    return {
        d: r for d, r in an._records(store, m.names, "ANNUAL", basis).items() if r.basis == basis
    }


def price_near(session: Session, company_id, on: date):
    """The latest official close on or within a week before `on` (NSE listing first), or None."""
    secs = sorted(session.scalars(select(Security).where(Security.company_id == company_id)), key=lambda s: (s.security_code is not None, s.id))  # fmt: skip
    for s in secs:
        px = session.scalar(
            select(EodPrice)
            .where(
                EodPrice.security_id == s.id,
                EodPrice.trade_date <= on,
                EodPrice.trade_date >= on - timedelta(days=PRICE_WINDOW_DAYS),
                EodPrice.close.is_not(None),
            )  # fmt: skip
            .order_by(EodPrice.trade_date.desc())
            .limit(1)
        )
        if px is not None:
            return px
    return None


# ---------------------------------------------------------------- historical multiples


def historical_pb(session: Session, company_id, store, basis: str) -> dict:
    equity, shares = _on_basis(store, EQUITY, basis), _on_basis(store, SHARES, basis)
    ends = sorted(set(equity) & set(shares))[-6:]
    if not ends:
        return {"available": False, "reason": f"owners' equity and shares outstanding are not both stored for any year end on the {basis.lower()} basis"}  # fmt: skip
    rows, skipped = [], []
    for end in ends:
        e, sh = equity[end], shares[end]
        px = price_near(session, company_id, end)
        if px is None:
            skipped.append({"fy": rd.fiscal_year(end), "reason": f"no stored price within {PRICE_WINDOW_DAYS} days before the year end"})  # fmt: skip
            continue
        if e.value is None or e.value <= 0 or not sh.value:
            skipped.append({"fy": rd.fiscal_year(end), "reason": "owners' equity or the share count is not positive"})  # fmt: skip
            continue
        mcap = px.close * sh.value
        rows.append({"fy": rd.fiscal_year(end), "period_end": end.isoformat(), "price_date": px.trade_date.isoformat(), "price": _s(px.close), "market_cap": _s(mcap.quantize(Decimal("1"))), "equity": _s(e.value), "pb": _s((mcap / e.value).quantize(Decimal("0.01"))), "evidence_ids": [e.id, sh.id]})  # fmt: skip
    return {"available": bool(rows), "basis": basis, "rows": rows, "skipped": skipped, "formula": "P/B at year end = (closing price on or just before the year end x shares outstanding at that date) / owners' equity at that date", **({} if rows else {"reason": "no year could be computed"})}  # fmt: skip


def historical_ev_ebitda(session: Session, company_id, store, basis: str) -> dict:
    shares, nd, ebitda, nci = _on_basis(store, SHARES, basis), _on_basis(store, NET_DEBT, basis), _on_basis(store, EBITDA, basis), _on_basis(store, NCI, basis)  # fmt: skip
    ends = sorted(set(shares) & set(nd) & set(ebitda))[-6:]
    if not ends:
        return {"available": False, "reason": f"shares outstanding, net debt and EBITDA are not all stored for any year end on the {basis.lower()} basis (EBITDA needs finance costs and depreciation)"}  # fmt: skip
    rows, skipped = [], []
    for end in ends:
        px = price_near(session, company_id, end)
        if px is None:
            skipped.append({"fy": rd.fiscal_year(end), "reason": f"no stored price within {PRICE_WINDOW_DAYS} days before the year end"})  # fmt: skip
            continue
        if ebitda[end].value is None or ebitda[end].value <= 0:
            skipped.append({"fy": rd.fiscal_year(end), "reason": "EBITDA is not positive"})
            continue
        ev = (
            px.close * shares[end].value
            + nd[end].value
            + (nci[end].value if end in nci else Decimal(0))
        )
        ids = [shares[end].id, nd[end].id, ebitda[end].id] + ([nci[end].id] if end in nci else [])
        rows.append({"fy": rd.fiscal_year(end), "period_end": end.isoformat(), "price_date": px.trade_date.isoformat(), "enterprise_value": _s(ev.quantize(Decimal("1"))), "ebitda": _s(ebitda[end].value), "ev_ebitda": _s((ev / ebitda[end].value).quantize(Decimal("0.01"))), "nci_included": end in nci, "evidence_ids": ids})  # fmt: skip
    return {"available": bool(rows), "basis": basis, "rows": rows, "skipped": skipped, "formula": "EV/EBITDA at year end = (closing price x shares + net debt + non-controlling interests where stored) / that year's EBITDA", **({} if rows else {"reason": "no year could be computed"})}  # fmt: skip


def context(history: dict, current: dict | None, key: str, label: str) -> dict | None:
    """The current multiple positioned against the company's own year-end observations."""
    if not history.get("available") or current is None:
        return None
    vals = [Decimal(r[key]) for r in history["rows"]]
    if len(vals) < MIN_HISTORY:
        return {"label": label, "available": False, "reason": f"only {len(vals)} year-end observation(s); {MIN_HISTORY} are needed to position a multiple"}  # fmt: skip
    now = Decimal(str(current["value"]))
    at_or_below = sum(1 for v in vals if v <= now)
    position = (
        "ABOVE_RANGE" if now > max(vals) else "BELOW_RANGE" if now < min(vals) else "WITHIN_RANGE"
    )
    return {
        "label": label,
        "available": True,
        "current": _s(now.quantize(Decimal("0.01"))),
        "current_as_of": current.get("period_end"),
        "current_evidence_id": current.get("evidence_id"),
        "observations": len(vals),
        "min": _s(min(vals).quantize(Decimal("0.01"))),
        "median": _s(Decimal(str(median(vals))).quantize(Decimal("0.01"))),
        "max": _s(max(vals).quantize(Decimal("0.01"))),
        "share_at_or_below_pct": _s(
            (Decimal(at_or_below) / Decimal(len(vals)) * HUNDRED).quantize(Decimal("1"))
        ),
        "position": position,
        "evidence_ids": [
            i
            for r in history["rows"]
            for i in r.get("evidence_ids", [r.get("eps_evidence_id")])
            if i
        ],
        "note": "year-end observations only; the position describes where today's multiple sits in the company's own past, it is not a verdict on cheapness",
    }


# ---------------------------------------------------------------- relative scenarios


def _peer_stats(row: dict | None):
    if not row:
        return None, "no comparable peer multiple"
    vals = [dec(p["value"]) for p in row["peers"] if p.get("comparable")]
    vals = [v for v in vals if v is not None and v > 0]
    if len(vals) < MIN_PEERS:
        return None, f"{len(vals)} comparable peer value(s); at least {MIN_PEERS} are needed"
    if len(vals) >= 4:
        q1, med, q3 = quantiles(vals, n=4)
        return (
            Decimal(str(q1)),
            Decimal(str(med)),
            Decimal(str(q3)),
            "lower quartile / median / upper quartile of the peers",
            len(vals),
        ), None
    return (
        min(vals),
        Decimal(str(median(vals))),
        max(vals),
        "lowest / median / highest of fewer than 4 peers",
        len(vals),
    ), None


def relative_on_price(price, own, peer_row: dict | None, label: str, per: str) -> dict:
    """P/E or P/B scenarios: value per share = price x (peer multiple / own multiple), i.e. the peer multiple
    applied to the company's own earnings (or book value) per share."""
    if price is None or own is None:
        return {"completed": False, "reason": f"price or {label} is not stored"}
    stats, why = _peer_stats(peer_row)
    if stats is None:
        return {"completed": False, "reason": why}
    low, mid, high, spread, n = stats
    own_v, px = dec(own.value), dec(price.value)
    if not own_v or own_v <= 0:
        return {"completed": False, "reason": f"the company's {label} is not positive"}
    out = {}
    for name, mult in (("bear", low), ("base", mid), ("bull", high)):
        value = px * mult / own_v
        out[name] = {"peer_multiple": _s(mult.quantize(Decimal("0.01"))), "value_per_share": _s(value.quantize(Decimal("1"))), "vs_price_pct": _s(((value / px - 1) * HUNDRED).quantize(Decimal("0.1"))), "evidence_ids": [price.id, own.id]}  # fmt: skip
    base_mult = mid
    grid = [{"multiple": _s((base_mult + d).quantize(Decimal("0.01"))), "cells": [{"own_" + per: _s((own_v * (1 + e)).quantize(Decimal("0.01"))), "value_per_share": _s(((base_mult + d) * px / own_v * (1 + e)).quantize(Decimal("1")))} for e in BASE_OFFSETS]} for d in MULTIPLE_OFFSETS if base_mult + d > 0]  # fmt: skip
    return {"completed": True, "label": label, "method": f"relative: value per share = price x (peer {label} / own {label}), i.e. the peer {label} applied to the company's own {per} per share", "spread": spread, "peer_count": n, "scenarios": out, "inputs": {"price": {"value": _s(px), "evidence_id": price.id}, "own": {"value": _s(own_v), "period_end": own.period_end.isoformat() if own.period_end else None, "evidence_id": own.id}}, "sensitivity": {"rows": "peer multiple (base statistic offset by turns)", "cols": f"the company's own {per} offset by -10% to +10%", "grid": grid}, "assumption": "that peers' multiples are a fair guide to this company; quality and growth differences are not adjusted for"}  # fmt: skip


def historical_scenarios(price, own, hist: dict | None) -> dict:
    """Scenarios from the company's OWN year-end P/E range (quartiles of four or more years, lowest / median /
    highest of three): value per share = the company's current EPS x that historical multiple. No rate or
    multiple is assumed; shown for context and not counted in the verdict (it assumes the past range still applies)."""
    rows = (hist or {}).get("rows") or []
    pseudo = {
        "peers": [
            {"value": r["pe"], "comparable": True, "evidence_id": r.get("eps_evidence_id")}
            for r in rows
        ]
    }
    out = relative_on_price(price, own, pseudo, "P/E", "EPS")
    if not out.get("completed"):
        n = len(rows)
        why = out.get("reason") or ""
        out["reason"] = (
            f"{n} year-end P/E observation(s) is too few; at least {MIN_PEERS} are needed"
            if "comparable peer value" in why
            else why
        )
        return out
    ids = sorted(
        {price.id, own.id} | {r["eps_evidence_id"] for r in rows if r.get("eps_evidence_id")}
    )
    for sc in out["scenarios"].values():
        sc["peer_multiple"] = sc["peer_multiple"]
        sc["evidence_ids"] = ids
    out.update(
        {
            "method": "historical: value per share = the company's current EPS x its own year-end P/E (price at each fiscal year end / that year's basic EPS)",
            "spread": out["spread"]
            .replace("of the peers", "of the company's own year-end P/E")
            .replace(
                "of fewer than 4 peers", "of its own year-end P/E (fewer than 4 years)"
            ),  # fmt: skip
            "assumption": "that the company's own past P/E range is a fair guide to its present multiple; changes in growth, returns and risk since then are not adjusted for",
            "multiple_label": "Own historical P/E",
            "count_label": "year-end observations",
            "in_verdict": False,
        }
    )
    out["sensitivity"]["rows"] = "historical P/E (median offset by turns)"
    return out


def relative_ev_ebitda(act: dict, ebitda_rec, peer_row: dict | None) -> dict:
    """EV/EBITDA scenarios: peer multiple x own EBITDA = enterprise value, less net debt and non-controlling interests, per share."""
    need = [k.replace("_", " ") for k in ("net_debt", "shares", "price") if not act.get(k)]
    if ebitda_rec is None:
        need.insert(0, "annual EBITDA")
    if need:
        return {
            "completed": False,
            "reason": "needs stored " + ", ".join(need) + "; none is assumed",
        }
    stats, why = _peer_stats(peer_row)
    if stats is None:
        return {"completed": False, "reason": why}
    low, mid, high, spread, n = stats
    ebitda = dec(ebitda_rec.value)
    if not ebitda or ebitda <= 0:
        return {"completed": False, "reason": "the company's EBITDA is not positive"}
    nd = dec(act["net_debt"]["value"])
    nci = (
        dec(act["non_controlling_interests"]["value"])
        if act.get("non_controlling_interests")
        else Decimal(0)
    )
    shares, px = dec(act["shares"]["value"]), dec(act["price"]["value"])
    ids = [ebitda_rec.id, act["net_debt"]["evidence_id"], act["shares"]["evidence_id"], act["price"]["evidence_id"]] + ([act["non_controlling_interests"]["evidence_id"]] if act.get("non_controlling_interests") else [])  # fmt: skip

    def value(mult, e=Decimal(1)):
        return (mult * ebitda * e - nd - nci) / shares

    out = {}
    for name, mult in (("bear", low), ("base", mid), ("bull", high)):
        v = value(mult)
        out[name] = {"peer_multiple": _s(mult.quantize(Decimal("0.01"))), "enterprise_value": _s((mult * ebitda).quantize(Decimal("1"))), "value_per_share": _s(v.quantize(Decimal("1"))), "vs_price_pct": _s(((v / px - 1) * HUNDRED).quantize(Decimal("0.1"))), "evidence_ids": ids}  # fmt: skip
    grid = [{"multiple": _s((mid + d).quantize(Decimal("0.01"))), "cells": [{"ebitda": _s((ebitda * (1 + e)).quantize(Decimal("1"))), "value_per_share": _s(value(mid + d, 1 + e).quantize(Decimal("1")))} for e in BASE_OFFSETS]} for d in MULTIPLE_OFFSETS if mid + d > 0]  # fmt: skip
    flags = []
    if not act.get("non_controlling_interests"):
        flags.append("non-controlling interests are not stored and are not deducted")
    if act["net_debt"]["period_end"] != (
        ebitda_rec.period_end.isoformat() if ebitda_rec.period_end else None
    ):
        flags.append(f"net debt is as at {act['net_debt']['period_end']} but EBITDA is for the year ended {ebitda_rec.period_end}")  # fmt: skip
    return {"completed": True, "label": "EV/EBITDA", "method": "relative: enterprise value = peer EV/EBITDA x the company's EBITDA; equity value = enterprise value - net debt - non-controlling interests; value per share = equity value / shares", "spread": spread, "peer_count": n, "scenarios": out, "inputs": {"ebitda": {"value": _s(ebitda), "period_end": ebitda_rec.period_end.isoformat(), "evidence_id": ebitda_rec.id}, "net_debt": act["net_debt"], "shares": act["shares"], "price": act["price"], "non_controlling_interests": act.get("non_controlling_interests")}, "flags": flags, "sensitivity": {"rows": "peer EV/EBITDA (base statistic offset by turns)", "cols": "the company's EBITDA offset by -10% to +10%", "grid": grid}, "assumption": "that peers' EV/EBITDA is a fair guide to this company; capital intensity, growth and quality differences are not adjusted for"}  # fmt: skip


# ---------------------------------------------------------------- which methods fit the sector


def method_plan(sector_code: str, valuation_kind: str, is_nbfc: bool, blocks: dict) -> list[dict]:
    """Per method: APPLIES / NOT_APPLICABLE, with status and reason, for the company's sector."""
    financial = valuation_kind == "BANK"
    lender = "an NBFC" if is_nbfc else "a bank"
    na_fin = f"not used for {lender}: its debt is raw material and its interest an operating cost, so enterprise value and free cash flow to the firm have no meaning"  # fmt: skip

    def status(key):
        b = blocks.get(key) or {}
        if b.get("completed") or b.get("available"):
            return "COMPLETED", None
        return "NOT_COMPLETED", b.get("reason") or (("needs " + "; ".join(sum(b.get("missing", {}).values(), []))) if isinstance(b.get("missing"), dict) and b.get("missing") else None)  # fmt: skip

    plan = []

    def add(method, applies, key=None, why=None):
        if not applies:
            plan.append(
                {"method": method, "applies": False, "status": "NOT_APPLICABLE", "reason": why}
            )
            return
        st, reason = status(key) if key else ("COMPLETED", None)
        plan.append({"method": method, "applies": True, "status": st, "reason": reason})

    add("Historical P/E", True, "historical")
    add("Peer P/E", True, "peer")
    add("Relative scenarios on P/E", True, "relative")
    add("Historical P/B", True, "historical_pb")
    add("Peer P/B", True, "peer")
    add("Relative scenarios on P/B", True, "relative_pb")
    if financial:
        add("P/B against ROE", True, "pb_vs_roe")
        add("Residual income (justified P/B)", True, "intrinsic")
        add("EV/EBITDA", False, why=na_fin)
        add("Discounted cash flow (FCFF)", False, why=na_fin)
        add(
            "Sum of the parts",
            False,
            why="not used for a lender: segment results are not valued by an operating multiple here",
        )
    else:
        add("EV/EBITDA (current, historical, peers)", True, "ev_ebitda")
        add("Relative scenarios on EV/EBITDA", True, "relative_ev_ebitda")
        add("Discounted cash flow (FCFF)", True, "dcf")
        sotp_ok = sector_code in ("AUTOMOBILES", "ENERGY", "CONGLOMERATE", "GENERIC_NONFIN")
        add("Sum of the parts", sotp_ok, "sotp", None if sotp_ok else "not used for a company that is essentially one business: segment values would add nothing the group multiple does not already give")  # fmt: skip
        add(
            "Residual income (justified P/B)",
            False,
            why="a lender's method; not used for a non-financial company",
        )
    return plan
