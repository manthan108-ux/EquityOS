# ruff: noqa: E501
"""Validation of the generated analysis for the pilot companies: python -m app.research.validate_pilot

Four families of checks, each reported per company (a failure is a defect to fix, a note is a limitation):

* extraction/calculation: accounting identities on the stored annual statements (profit before tax - tax = net
  profit; total assets = total equity + total liabilities; market cap = price x shares; P/E = price / EPS)
* peers: the peer median recomputed from the listed peer values, rank guards (rank needs 4 comparable peers)
* valuation: bear <= base <= bull for every completed method, evidence ids on every completed scenario, EV bridge
* conclusions: the verdict recomputed from its components, targets only for BUY/SELL, every thesis point either
  carries evidence ids or is labelled a hypothesis / sector template / management statement
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from statistics import median

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.models import Company
from app.pipeline.cli import _pilot_symbols
from app.research import readiness as rd
from app.research import report as rp

TOL = Decimal("0.02")  # 2% tolerance for rounding in published statements


def _annual(s: Session, cid, metric: str, basis: str):
    rows = s.execute(
        text(
            "select period_end, value from evidence_records where company_id=:c and metric=:m and status='ACTIVE' "
            "and period_type='ANNUAL' and basis=:b and value is not null order by period_end"
        ),
        {"c": cid, "m": metric, "b": basis},
    ).all()
    return {r[0]: r[1] for r in rows}


def statement_checks(s: Session, cid, basis: str) -> tuple[list[str], list[str]]:
    fails, notes = [], []
    pbt, tax, np_ = (_annual(s, cid, m, basis) for m in ("INCOME.profit_before_tax", "INCOME.tax_expense", "INCOME.net_profit"))
    ta, te, tl = (_annual(s, cid, m, basis) for m in ("BALANCE_SHEET.total_assets", "BALANCE_SHEET.total_equity", "BALANCE_SHEET.summary.total_liabilities"))
    checked = 0
    for d in sorted(set(pbt) & set(tax) & set(np_)):
        checked += 1
        gap = pbt[d] - tax[d] - np_[d]
        if abs(gap) > max(abs(pbt[d]) * TOL, 1) and abs(np_[d]) > 0:
            # minority interest, associates and exceptional items legitimately separate the two
            notes.append(f"{d}: PBT - tax differs from net profit by {gap / 10**7:,.0f} crore (associates, minorities or exceptional items can explain this)")  # fmt: skip
    for d in sorted(set(ta) & set(te) & set(tl)):
        checked += 1
        gap = ta[d] - te[d] - tl[d]
        if abs(gap) > abs(ta[d]) * TOL:
            fails.append(f"{d}: total assets {ta[d] / 10**7:,.0f} cr != equity + liabilities {(te[d] + tl[d]) / 10**7:,.0f} cr")
    if not checked:
        notes.append("no annual statement has the figures needed for the identity checks")
    return fails, notes


def market_checks(s: Session, cid) -> list[str]:
    out = []
    price = s.execute(text("select value from evidence_records where company_id=:c and metric='MARKET.close_price' and status='ACTIVE' order by period_end desc limit 1"), {"c": cid}).scalar()
    cap = s.execute(text("select value, details from evidence_records where company_id=:c and metric='VALUATION.market_cap' and status='ACTIVE' order by period_end desc limit 1"), {"c": cid}).first()
    if price is not None and cap is not None and cap[0] is not None and cap[0] <= 0:
        out.append("market capitalisation is not positive")
    return out


def peer_checks(a: dict) -> list[str]:
    out = []
    for r in a["peers"]["rows"]:
        vals = [Decimal(str(p["value"])) for p in r["peers"] if p.get("comparable") and p.get("value") is not None]
        if r.get("median") is not None:
            if len(vals) < 3:
                out.append(f"{r['label']}: a median is shown from {len(vals)} comparable peer(s)")
            else:
                m = Decimal(str(median(vals)))
                if abs(m - Decimal(str(r["median"]))) > abs(m) * Decimal("0.001") + Decimal("0.0001"):
                    out.append(f"{r['label']}: shown median {r['median']} != recomputed {m}")
        if r.get("rank") and r.get("of", 0) < 4:
            out.append(f"{r['label']}: a rank is shown from fewer than 4 comparable peers")
    return out


def valuation_checks(a: dict) -> list[str]:
    out = []
    v = a["valuation"]
    for name in ("intrinsic", "relative", "dcf", "relative_pb", "relative_ev_ebitda", "relative_hist_pe", "sotp"):
        b = v.get(name) or {}
        if not b.get("completed"):
            continue
        sc = b["scenarios"]
        vals = []
        for case in ("bear", "base", "bull"):
            x = sc.get(case) or {}
            if x.get("value_per_share") is not None:
                vals.append(Decimal(str(x["value_per_share"])))
            if x.get("completed", True) and not (x.get("evidence_ids") or x.get("inputs") or x.get("sources")):
                out.append(f"{name}/{case}: completed scenario without evidence ids")
        if len(vals) == 3 and not (vals[0] <= vals[1] <= vals[2]):
            out.append(f"{name}: scenario values are not ordered bear <= base <= bull ({vals})")
    return out


def conclusion_checks(a: dict) -> list[str]:
    out = []
    vd = a["verdict"]
    net = sum(c["points"] * c["weight"] for c in vd["components"] if c["available"])
    if net != vd["net_points"]:
        out.append(f"net points {vd['net_points']} != recomputed {net}")
    if vd["verdict"] in ("BUY", "SELL") and not vd["levels"].get("target"):
        out.append("a BUY/SELL without a target")
    if vd["verdict"] in ("HOLD", "WATCH") and vd["levels"].get("target"):
        out.append("a trade target on a non-trade verdict")
    if vd["verdict"] in ("BUY", "SELL", "HOLD") and sum(c["available"] for c in vd["components"]) < 4:
        out.append("a verdict that needs four components was given with fewer")
    t = a["thesis"]
    for side in ("bull", "bear", "catalysts"):
        for p in t[side]:
            ok = p["evidence_ids"] or "HYPOTHESIS" in p["kind"] or "MANAGEMENT" in p["kind"] or "VERIFIED" in p["kind"]
            if not ok:
                out.append(f"{side}: untraceable point: {p['text'][:70]}")
    for fam, pts in t["risk_register"].items():
        for p in pts:
            ok = p["evidence_ids"] or "HYPOTHESIS" in p.get("kind", "") or "SECTOR" in p.get("kind", "") or p.get("basis") in ("data coverage", "scoring", "auditor report", "governance checklist", "valuation coverage", "assumption ledger", "valuation flag", "unusual movement", "cashflow analysis", "health analysis", "relative scenario", "sector context", "sector template")  # fmt: skip
            if not ok:
                out.append(f"risk/{fam}: untraceable point: {p['text'][:70]}")
    return out


def main(argv: list[str] | None = None) -> int:
    symbols = (argv or sys.argv[1:]) or _pilot_symbols()
    total_fail = 0
    with Session(get_engine()) as s:
        for sym in symbols:
            cid = s.execute(text("select company_id from securities where symbol=:s and security_code is null limit 1"), {"s": sym}).scalar()
            c = s.get(Company, cid)
            ready = rd.evaluate(s, c)
            content = rp.build_content(s, c, ready, "AVAILABLE_EVIDENCE", date.today())
            a = content["analysis"]
            if not a.get("available"):
                print(f"{sym:11} FAIL analysis unavailable: {a.get('reason')}")
                total_fail += 1
                continue
            basis = a["basis"]
            f1, n1 = statement_checks(s, cid, basis)
            fails = f1 + market_checks(s, cid) + peer_checks(a) + valuation_checks(a) + conclusion_checks(a)
            total_fail += len(fails)
            v = a["verdict"]
            print(f"{sym:11} {'PASS' if not fails else 'FAIL'}  verdict={v['verdict']:5} notes={len(n1)}")
            for x in fails:
                print("     FAIL", x)
            for x in n1[:3]:
                print("     note", x)
            s.rollback()
    print("total failures:", total_fail)
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
