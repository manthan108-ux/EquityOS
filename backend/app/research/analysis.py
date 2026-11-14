"""Deterministic financial analysis over the validated evidence store (report phases 2 and 3).

Everything here is arithmetic on stored evidence plus rule-based wording; no LLM is involved and no
figure is estimated. Every data point carries the evidence id it came from. Three kinds of statement
are kept apart:

* FACT           - a stored value read from a document or feed
* DERIVED        - computed here or by the calculation engine from stored values (formula stated)
* INTERPRETATION - a rule-based reading of derived figures, with the rule's threshold disclosed

Basis discipline: a series uses the preferred accounting basis. If that basis has fewer than two
annual points but the other basis has more, the other basis is shown, labelled, in its own row; the two
are never combined in one series. Peers are compared only on the same period end, basis and unit.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, EvidenceClass, EvidenceRecord, Peer, PeerSet
from app.research import readiness as rd
from app.research.sectors import AMOUNT, PERSHARE, Metric, Sector

VERSION = "1.0"
STABLE_PCT = Decimal(
    "1"
)  # an amount that moved by less than 1% is called stable (presentation rule)
STABLE_PP = Decimal("0.25")  # a ratio that moved by less than 0.25 points is called stable
UNUSUAL_PCT = Decimal("25")  # an annual change of more than 25% is flagged for explanation
IN_LINE = Decimal("0.05")  # within 5% of the peer median counts as in line (presentation rule)
MIN_PEERS = 3  # no ranking or median unless at least three peers are comparable
QUARTER_DAYS = (80, 100)


def dec(value) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except InvalidOperation:
        return None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


# ---------------------------------------------------------------- picking records


def _tier(rec: EvidenceRecord) -> int:
    return rec.authority_tier if rec.authority_tier is not None else 9


def _best(recs: list[EvidenceRecord], names: tuple[str, ...]) -> EvidenceRecord:
    """The most trustworthy of several records for one period: metric priority, dimension, tier."""
    return min(
        recs,
        key=lambda r: (
            names.index(r.metric) if r.metric in names else 99,
            rd.DIMENSION_ORDER.get(r.dimension or "", 9),
            _tier(r),
            -r.id,
        ),
    )


def _records(store: rd._Store, names: tuple[str, ...], ptype: str, basis: str | None):
    out: dict[date, list[EvidenceRecord]] = {}
    for name in names:
        for r in store.by_metric.get(name, []):
            if r.value is None or r.period_end is None or r.period_type != ptype:
                continue
            if basis is not None and r.basis not in (basis, None):
                continue
            out.setdefault(r.period_end, []).append(r)
    return {d: _best(rs, names) for d, rs in out.items()}


def _point(rec: EvidenceRecord) -> dict:
    return {
        "period_end": rec.period_end.isoformat(),
        "fy": rd.fiscal_year(rec.period_end),
        "value": _s(rec.value),
        "unit": rec.unit,
        "basis": rec.basis,
        "evidence_id": rec.id,
        "document_id": str(rec.document_id) if rec.document_id else None,
        "page": rec.source_page,
        "kind": "FACT" if rec.evidence_class == EvidenceClass.FACT else "DERIVED",
    }


# the statement lines that decide whether a basis has usable annual statements
BASIS_CORE = (
    "INCOME.summary.revenue",
    "INCOME.revenue_from_operations",
    "INCOME.summary.total_income",
    "INCOME.net_profit",
    "INCOME.summary.net_profit",
    "BALANCE_SHEET.total_assets",
    "BALANCE_SHEET.summary.total_assets",
)
MIN_ANNUAL_POINTS = 2


def resolve_basis(store: rd._Store, prefer: str = "CONSOLIDATED") -> dict:
    """Which accounting basis the whole report is written on, and why.

    The requested basis (consolidated by default) is used when it has at least two annual statement
    years. If it does not, and the other basis does, the report falls back to the other basis and says
    so, with a warning. The choice is made ONCE and used for every figure, comparison and chart, so the
    two bases are never mixed. Both bases stay in the store; nothing is deleted.
    """
    other = "STANDALONE" if prefer == "CONSOLIDATED" else "CONSOLIDATED"
    points = {
        b: len({d for d, r in _records(store, BASIS_CORE, "ANNUAL", b).items() if r.basis == b})
        for b in (prefer, other)
    }
    used, warning = prefer, None
    if points[prefer] < MIN_ANNUAL_POINTS <= points[other]:
        used = other
        warning = (
            f"{prefer.title()} statements were not available for this company ({points[prefer]} annual "
            f"year(s) in the evidence store), so {other.lower()} statements are used throughout this "
            f"report ({points[other]} annual years). No {prefer.lower()} and {other.lower()} figures "
            "are combined."
        )
    return {
        "requested": prefer,
        "used": used,
        "fallback": used != prefer,
        "annual_years": points,
        "warning": warning,
    }


def series_for(store: rd._Store, m: Metric, prefer: str, ptype: str, limit: int):
    """(points, basis_used, basis_note): the latest `limit` points on one basis."""
    on_pref = _records(store, m.names, ptype, prefer)
    other_basis = "STANDALONE" if prefer == "CONSOLIDATED" else "CONSOLIDATED"
    note = None
    use = on_pref
    if len(on_pref) < 2 and ptype == "ANNUAL":
        alt = _records(store, m.names, ptype, other_basis)
        alt = {d: r for d, r in alt.items() if r.basis == other_basis}
        if len(alt) > len(on_pref):
            use = alt
            note = (
                f"shown on the {other_basis.lower()} basis: the {prefer.lower()} basis has "
                f"{len(on_pref)} annual value(s) in the evidence store"
            )
    recs = [use[d] for d in sorted(use)][-limit:]
    basis_used = recs[-1].basis if recs else None
    return [_point(r) for r in recs], basis_used, note


# ---------------------------------------------------------------- changes


def change(prev: dict, cur: dict, kind: str) -> dict | None:
    a, b = dec(prev["value"]), dec(cur["value"])
    if a is None or b is None:
        return None
    if kind in (AMOUNT, PERSHARE):
        if a <= 0:
            return None
        return {"period": cur["fy"], "type": "pct", "value": _s((b / a - 1) * 100)}
    return {"period": cur["fy"], "type": "points", "value": _s(b - a)}


def _stable(ch: dict) -> bool:
    v = abs(Decimal(ch["value"]))
    return v < (STABLE_PCT if ch["type"] == "pct" else STABLE_PP)


def direction(ch: dict | None) -> str | None:
    if ch is None:
        return None
    if _stable(ch):
        return "stable"
    return "up" if Decimal(ch["value"]) > 0 else "down"


def _ids(*points) -> list[int]:
    return [p["evidence_id"] for p in points if p and p.get("evidence_id")]


# ---------------------------------------------------------------- trends (phase 2)


def trends(store: rd._Store, sector: Sector, prefer: str) -> dict:
    annual, observations, unusual = [], [], []
    by_key: dict[str, dict] = {}
    for m in sector.trend:
        pts, basis, note = series_for(store, m, prefer, "ANNUAL", 5)
        if not pts:
            continue
        changes = [c for a, b in zip(pts, pts[1:], strict=False) if (c := change(a, b, m.kind))]
        row = {
            "key": m.key,
            "label": m.label,
            "group": m.group,
            "kind": m.kind,
            "better": m.better,
            "basis": basis,
            "basis_note": note,
            "points": pts,
            "changes": changes,
        }
        annual.append(row)
        by_key[m.key] = row
        for ch, (a, b) in zip(changes, zip(pts, pts[1:], strict=False), strict=False):
            if (
                m.kind in (AMOUNT, PERSHARE)
                and abs(Decimal(ch["value"])) > UNUSUAL_PCT
                and (m.group != "Cash flow" or sector.cash_flow_meaningful)
            ):
                unusual.append(
                    {
                        "key": m.key,
                        "label": m.label,
                        "period": ch["period"],
                        "change_pct": ch["value"],
                        "evidence_ids": _ids(a, b),
                        "text": (
                            f"{m.label} moved {Decimal(ch['value']).quantize(Decimal('0.1'))}% in "
                            f"{ch['period']}; the cause is not established from the evidence held."
                        ),
                    }
                )
    quarterly = quarterly_rows(store, sector, prefer)
    observations = _observations(by_key, quarterly)
    return {
        "annual": annual,
        "quarterly": quarterly,
        "observations": observations,
        "unusual": unusual,
        "rules": (
            f"stable = change under {STABLE_PCT}% (amounts) or {STABLE_PP} points (ratios); an annual "
            f"change above {UNUSUAL_PCT}% is flagged as unusual"
        ),
    }


def _same_quarter_last_year(d: date, pts: dict[date, dict]) -> dict | None:
    for p in pts:
        if abs((d - p).days - 365) <= 6:
            return pts[p]
    return None


def _previous_quarter(d: date, pts: dict[date, dict]) -> dict | None:
    for p in sorted(pts, reverse=True):
        if QUARTER_DAYS[0] <= (d - p).days <= QUARTER_DAYS[1]:
            return pts[p]
    return None


def quarterly_rows(store: rd._Store, sector: Sector, prefer: str) -> list[dict]:
    rows = []
    for m in sector.quarterly:
        pts, basis, note = series_for(store, m, prefer, "QUARTERLY", 6)
        if not pts:
            continue
        by_date = {date.fromisoformat(p["period_end"]): p for p in pts}
        latest = pts[-1]
        ld = date.fromisoformat(latest["period_end"])
        yoy_p, qoq_p = _same_quarter_last_year(ld, by_date), _previous_quarter(ld, by_date)
        yoy = change(yoy_p, latest, m.kind) if yoy_p else None
        qoq = change(qoq_p, latest, m.kind) if qoq_p else None
        rows.append(
            {
                "key": m.key,
                "label": m.label,
                "kind": m.kind,
                "better": m.better,
                "basis": basis,
                "points": pts,
                "yoy": yoy,
                "qoq": qoq,
                "evidence_ids": _ids(latest, yoy_p, qoq_p),
            }
        )
    return rows


def _fmt_change(ch: dict) -> str:
    v = Decimal(ch["value"]).quantize(Decimal("0.1"))
    return f"{abs(v)}%" if ch["type"] == "pct" else f"{abs(v)} points"


def _obs(text, kind, ids, key):
    return {"text": text, "kind": kind, "evidence_ids": ids, "key": key}


def _observations(by_key: dict[str, dict], quarterly: list[dict]) -> list[dict]:
    out = []
    for key in ("total_income", "revenue", "nii", "net_profit", "eps", "ebit_margin", "net_margin"):
        row = by_key.get(key)
        if not row or not row["changes"]:
            continue
        ch, label = row["changes"][-1], row["label"]
        pts = row["points"][-2:]
        d = direction(ch)
        if d == "stable":
            text = f"{label} was broadly stable in {ch['period']} ({_fmt_change(ch)} change)."
        else:
            verb = "rose" if d == "up" else "fell"
            text = f"{label} {verb} {_fmt_change(ch)} in {ch['period']}."
        if len(row["changes"]) >= 2:
            prev = row["changes"][-2]
            text += f" The previous year's change was {'+' if Decimal(prev['value']) >= 0 else '-'}{_fmt_change(prev)}."
        out.append(_obs(text, "DERIVED", _ids(*pts), key))
    for key, text_kind in (("roe", "Return on equity"), ("cost_to_income", "Cost-to-income ratio")):
        row = by_key.get(key)
        if row and row["changes"]:
            ch = row["changes"][-1]
            d = direction(ch)
            words = {
                "stable": "was broadly stable",
                "up": f"rose {_fmt_change(ch)}",
                "down": f"fell {_fmt_change(ch)}",
            }[d]
            out.append(
                _obs(
                    f"{text_kind} {words} in {ch['period']}.",
                    "DERIVED",
                    _ids(*row["points"][-2:]),
                    key,
                )
            )
    for row in quarterly:
        if row["yoy"] and row["key"] in (
            "net_profit",
            "nii",
            "revenue",
            "total_income",
            "advances",
            "deposits",
        ):
            ch = row["yoy"]
            d = direction(ch)
            verb = {"stable": "was flat", "up": "grew", "down": "fell"}[d]
            tail = "" if d == "stable" else f" {_fmt_change(ch)}"
            period = row["points"][-1]["period_end"]
            out.append(
                _obs(
                    f"{row['label']} {verb}{tail} year on year in the quarter ended {period}.",
                    "DERIVED",
                    row["evidence_ids"],
                    row["key"],
                )
            )
    return out


# ---------------------------------------------------------------- peers (phase 3)


def peer_companies(session: Session, company_id: uuid.UUID) -> list[tuple[Company, Peer]]:
    ps = session.scalar(
        select(PeerSet)
        .where(PeerSet.company_id == company_id)
        .order_by(PeerSet.built_at.desc())
        .limit(1)
    )
    if ps is None:
        return []
    rows = session.execute(
        select(Peer, Company)
        .join(Company, Company.id == Peer.company_id)
        .where(Peer.peer_set_id == ps.id)
        .order_by(Peer.rank)
    ).all()
    return [(c, p) for p, c in rows]


def _latest_any(store: rd._Store, m: Metric, prefer: str) -> EvidenceRecord | None:
    """The record with the latest period end (a quarter beats a longer period ending the same day)."""
    cands: list[EvidenceRecord] = []
    types = ("QUARTERLY", "ANNUAL") if m.ptype == "ANY" else (m.ptype,)
    for t in types:
        cands += list(_records(store, m.names, t, prefer).values())
    if not cands:
        return None
    order = {"QUARTERLY": 0, "ANNUAL": 1}
    return max(cands, key=lambda r: (r.period_end, -order.get(r.period_type, 2)))


def compare_metric(
    m: Metric, own_store: rd._Store, peer_stores: list[tuple[Company, rd._Store]], prefer: str
) -> dict | None:
    own = _latest_any(own_store, m, prefer)
    if own is None:
        return None
    row = {
        "key": m.key,
        "label": m.label,
        "group": m.group,
        "kind": m.kind,
        "better": m.better,
        "company": {**_point(own), "unit": own.unit, "period_type": own.period_type},
        "peers": [],
    }
    comparable: list[Decimal] = []
    for company, store in peer_stores:
        recs = _records(store, m.names, own.period_type, prefer)
        rec = recs.get(own.period_end)
        entry = {"name": company.common_name}
        if rec is not None and rec.basis == own.basis and rec.unit == own.unit:
            entry |= {
                "comparable": True,
                "value": _s(rec.value),
                "period_end": rec.period_end.isoformat(),
                "evidence_id": rec.id,
            }
            comparable.append(rec.value)
        else:
            latest = max(recs.values(), key=lambda r: r.period_end) if recs else None
            if latest is None:
                entry |= {"comparable": False, "reason": "not available"}
            elif latest.period_end != own.period_end:
                entry |= {
                    "comparable": False,
                    "value": _s(latest.value),
                    "period_end": latest.period_end.isoformat(),
                    "evidence_id": latest.id,
                    "reason": f"different period ({latest.period_end.isoformat()})",
                }
            else:
                entry |= {"comparable": False, "reason": "different basis or unit"}
        row["peers"].append(entry)
    row["n_comparable"] = len(comparable)
    own_v = own.value
    if len(comparable) >= MIN_PEERS:
        med = Decimal(str(median(comparable)))
        row["median"] = _s(med)
        if m.better:
            ranked = sorted(comparable + [own_v], reverse=(m.better == "higher"))
            row["rank"] = ranked.index(own_v) + 1
            row["of"] = len(ranked)
            tol = max(abs(med) * IN_LINE, Decimal("0.05"))
            diff = own_v - med
            if abs(diff) <= tol:
                row["position"] = "in line"
            else:
                good = diff > 0 if m.better == "higher" else diff < 0
                row["position"] = "better" if good else "worse"
        else:
            row["position"] = (
                "above median" if own_v > med else "below median" if own_v < med else "at median"
            )
    else:
        row["note"] = (
            f"{len(comparable)} comparable peer(s); fewer than {MIN_PEERS}, so no median or rank is given"
        )
    return row


def peer_analysis(
    session: Session, company: Company, own_store_fn, sector: Sector, prefer: str
) -> tuple[dict, rd._Store, list[tuple[Company, rd._Store]]]:
    names = {n for m in sector.peer for n in m.names}
    own_store = own_store_fn(company.id, names)
    peers = peer_companies(session, company.id)
    peer_stores = [(c, own_store_fn(c.id, names)) for c, _ in peers]
    rows = []
    other = "STANDALONE" if prefer == "CONSOLIDATED" else "CONSOLIDATED"
    for m in sector.peer:
        r = compare_metric(m, own_store, peer_stores, prefer)
        alt = compare_metric(m, own_store, peer_stores, other)
        # Each metric is compared on ONE basis for all companies: the preferred one unless the other
        # basis has more comparable peers. The row says which basis it is on.
        if (
            alt
            and (r is None or alt["n_comparable"] > r["n_comparable"])
            and alt["company"]["basis"] == other
        ):
            alt["basis_note"] = (
                f"compared on the {other.lower()} basis: more peers report it than on the "
                f"{prefer.lower()} basis ({alt['n_comparable']} against {r['n_comparable'] if r else 0})"
            )
            r = alt
        if r:
            r["basis"] = r["company"]["basis"]
            rows.append(r)
    missing = [m.label for m in sector.peer if m.key not in {r["key"] for r in rows}]
    ranked = [r for r in rows if r.get("rank")]
    better = [r for r in ranked if r["position"] == "better"]
    worse = [r for r in ranked if r["position"] == "worse"]
    result = {
        "peers": [{"name": c.common_name, "rank": p.rank, "reason": p.reason} for c, p in peers],
        "rows": rows,
        "unavailable_metrics": missing,
        "summary": {
            "ranked_metrics": len(ranked),
            "better": [r["label"] for r in better],
            "worse": [r["label"] for r in worse],
            "in_line": [r["label"] for r in ranked if r["position"] == "in line"],
        },
        "rules": (
            f"a peer is compared only on the same period end, accounting basis and unit; a median "
            f"or rank needs at least {MIN_PEERS} comparable peers; within {int(IN_LINE * 100)}% of the "
            "median counts as in line; valuation multiples are positioned, not judged better or worse"
        ),
        "not_available": "CET1 and Tier 1 capital ratios are not extracted for any company"
        if sector.code == "BANK"
        else None,
    }
    return result, own_store, peer_stores
