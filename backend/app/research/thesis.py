"""Investment thesis and research conclusion (report phase 6): rule-based, evidence-linked.

Every bull or bear point is produced by a stated rule over analysis output (trends, peer positions,
valuation) and carries the evidence ids it rests on. Points are INTERPRETATIONS or, where they go beyond
the data (catalysts, valuation re-rating), ANALYTICAL HYPOTHESES, and are labelled so. Sector risk
templates are labelled as sector-generic. Management statements are quoted as statements.

The verdict (BUY / SELL / WATCH) is produced by verdict.py under its own stated rules; a
conclusion that the evidence cannot support is stated as "cannot be established".
"""

from __future__ import annotations

from decimal import Decimal

from app.research.analysis import STABLE_PCT, STABLE_PP, direction
from app.research.sectors import PCT, Sector

VERSION = "1.0"
PREMIUM_PCT = Decimal("10")  # a P/E or P/B this far from the peer median is a premium or discount
MAX_POINTS = 6
HYPOTHESIS = "ANALYTICAL HYPOTHESIS"


def _p(side, text, ids, basis, kind="INTERPRETATION"):
    return {
        "side": side,
        "text": text,
        "evidence_ids": sorted({i for i in ids if i}),
        "basis": basis,
        "kind": kind,
    }


def _row(rows: list[dict], key: str) -> dict | None:
    return next((r for r in rows if r["key"] == key), None)


def _fmt(v: str | None, kind: str) -> str:
    if v is None:
        return "n/a"
    d = Decimal(v).quantize(Decimal("0.01"))
    return f"{d}%" if kind == PCT else f"{d}"


def _move(ch: dict) -> tuple[str, str]:
    v = abs(Decimal(ch["value"])).quantize(Decimal("0.1"))
    return str(v), "%" if ch["type"] == "pct" else " points"


def _trend_signals(analysis: dict) -> list[dict]:
    out = []
    for row in analysis["trend"]["annual"]:
        if not row["changes"] or not row["better"]:
            continue
        ch = row["changes"][-1]
        d = direction(ch)
        if d == "stable":
            continue
        good = (d == "up") == (row["better"] == "higher")
        v, unit = _move(ch)
        out.append(
            _p(
                "bull" if good else "bear",
                f"{row['label']} {'rose' if d == 'up' else 'fell'} {v}{unit} in {ch['period']}.",
                [p["evidence_id"] for p in row["points"][-2:]],
                "annual trend",
            )
        )
    for row in analysis["trend"]["quarterly"]:
        ch = row.get("yoy")
        if not ch or not row.get("better"):
            continue
        d = direction(ch)
        if d == "stable":
            continue
        good = (d == "up") == (row["better"] == "higher")
        v, unit = _move(ch)
        out.append(
            _p(
                "bull" if good else "bear",
                f"{row['label']} {'rose' if d == 'up' else 'fell'} {v}{unit} year on year in the "
                f"quarter ended {row['points'][-1]['period_end']}.",
                row["evidence_ids"],
                "quarterly trend",
            )
        )
    return out


def _peer_signals(analysis: dict) -> list[dict]:
    out = []
    for r in analysis["peers"]["rows"]:
        if not r.get("rank") or r["position"] not in ("better", "worse"):
            continue
        side = "bull" if r["position"] == "better" else "bear"
        word = "better than" if side == "bull" else "worse than"
        ids = [r["company"]["evidence_id"]] + [
            p["evidence_id"] for p in r["peers"] if p.get("comparable")
        ]
        out.append(
            _p(
                side,
                f"{r['label']} is {_fmt(r['company']['value'], r['kind'])} "
                f"({r['company']['period_end']}, {str(r.get('basis') or 'basis n/a').lower()}), {word} the peer median of "
                f"{_fmt(r['median'], r['kind'])}; rank {r['rank']} of {r['of']}.",
                ids,
                "peer-relative",
            )
        )
    return out


def _valuation_signals(analysis: dict) -> list[dict]:
    out = []
    roe = _row(analysis["peers"]["rows"], "roe")
    for r in analysis["valuation"]["peer"]["rows"]:
        if "premium_pct" not in r:
            continue
        prem = Decimal(r["premium_pct"])
        if abs(prem) < PREMIUM_PCT:
            continue
        word = "premium" if prem > 0 else "discount"
        text = (
            f"{r['label']} of {Decimal(r['company']).quantize(Decimal('0.01'))}x is a "
            f"{abs(prem).quantize(Decimal('0.1'))}% {word} to the peer median of "
            f"{Decimal(r['median']).quantize(Decimal('0.01'))}x."
        )
        if r["key"] == "pb" and roe and roe.get("position"):
            text += f" Its ROE against the peer median is: {roe['position']}."
        out.append(
            _p(
                "bull" if prem < 0 else "bear",
                text + " The gap may reflect factors not in the evidence.",
                [p["evidence_id"] for p in r["peers"]],
                "valuation",
                HYPOTHESIS,
            )
        )
    return out


def _catalysts(bull: list[dict], mgmt: dict, applied: list[dict]) -> list[dict]:
    out = [
        _p(
            "catalyst",
            f"Continuation of the recent quarterly momentum: {p['text']}",
            p["evidence_ids"],
            "quarterly trend",
            HYPOTHESIS,
        )
        for p in bull
        if p["basis"] == "quarterly trend"
    ]
    if mgmt.get("available"):
        for t in mgmt["topics"]:
            if t["key"] in ("outlook", "priorities", "expansion") and t["items"]:
                it = t["items"][0]
                out.append(
                    {
                        "side": "catalyst",
                        "text": it["text"],
                        "evidence_ids": [],
                        "basis": f"management statement, annual report page {it['page']} "
                        f"({t['title'].lower()})",
                        "kind": "MANAGEMENT STATEMENT",
                    }
                )
    for a in applied:
        out.append(
            _p(
                "catalyst",
                f"Verified corporate action: {a.get('ratio') or ''} with ex-date {a['ex_date']}.",
                [],
                "verified corporate action",
                "VERIFIED FACT",
            )
        )
    return out[: MAX_POINTS + 2]


def _risks(analysis: dict, sector: Sector, bear: list[dict], score: dict, auditor_flags) -> dict:
    company = [
        p for p in bear if p["basis"] in ("peer-relative", "annual trend", "quarterly trend")
    ]
    company += [
        _p("risk", u["text"], u["evidence_ids"], "unusual movement")
        for u in analysis["trend"]["unusual"]
    ]
    sector_risks = [
        {"title": t, "text": x, "kind": "SECTOR TEMPLATE (not evidence about this company)"}
        for t, x in sector.risks
    ]
    val = []
    intr = analysis["valuation"].get("intrinsic", {})
    if intr.get("completed"):
        implied = {
            r["cost_of_equity"]: Decimal(r["implied_roe"]) for r in intr["market_implied"]["rows"]
        }
        roe = Decimal(intr["inputs"]["roe"]["value"])
        base = implied.get("12")
        if base is not None and base > roe + 2:
            val.append(
                _p(
                    "risk",
                    f"At a 12% cost of equity and 6% growth (illustrative assumptions), the current "
                    f"P/B needs an ROE of {base}%, against the latest annual ROE of "
                    f"{roe.quantize(Decimal('0.1'))}%.",
                    [intr["inputs"]["roe"]["evidence_id"]],
                    "market-implied ROE",
                    HYPOTHESIS,
                )
            )
    rel = analysis["valuation"].get("relative", {})
    if rel.get("completed") and rel["scenarios"]["bear"]["vs_price_pct"]:
        v = Decimal(rel["scenarios"]["bear"]["vs_price_pct"])
        if v < 0:
            val.append(
                _p(
                    "risk",
                    f"Applying the lowest peer P/E to the company's earnings implies {abs(v)}% below "
                    "the current price.",
                    [],
                    "relative scenario",
                    HYPOTHESIS,
                )
            )
    data = []
    if analysis["peers"]["unavailable_metrics"]:
        text = (
            "Metrics with no company value: "
            + ", ".join(analysis["peers"]["unavailable_metrics"])
            + "."
        )
        data.append(_p("risk", text, [], "data coverage"))
    if score.get("available") and str(score.get("confidence")).upper() == "LOW":
        data.append(_p("risk", "The financial score has LOW confidence.", [], "scoring"))
    if len(analysis["peers"]["peers"]) < 3:
        data.append(
            _p(
                "risk",
                "Fewer than three peers: peer-relative conclusions are weak.",
                [],
                "data coverage",
            )
        )
    if auditor_flags:
        gov = [
            _p("risk", f"Auditor-report phrases detected: {auditor_flags}.", [], "auditor report")
        ]
    else:
        gov = [
            _p(
                "risk",
                "No auditor-report qualification phrase was detected (a phrase search, not a view of "
                "audit quality). No other governance evidence is assessed.",
                [],
                "auditor report",
            )
        ]
    return {
        "company": company[: MAX_POINTS + 2],
        "sector": sector_risks,
        "valuation": val,
        "data": data,
        "governance": gov,
    }


def _monitoring(analysis: dict, sector: Sector) -> list[dict]:
    tr = {r["key"]: r for r in analysis["trend"]["annual"]}
    q = {r["key"]: r for r in analysis["trend"]["quarterly"]}
    pr = {r["key"]: r for r in analysis["peers"]["rows"]}
    meta = {m.key: m for m in (*sector.trend, *sector.quarterly, *sector.peer)}
    out = []
    for key in sector.monitor:
        m = meta.get(key)
        item = {
            "key": key,
            "label": m.label if m else key,
            "better": m.better if m else None,
            "available": False,
        }
        if key in q:
            p = q[key]["points"][-1]
            item |= {
                "available": True,
                "value": p["value"],
                "unit": p["unit"],
                "period_end": p["period_end"],
                "yoy": q[key]["yoy"],
                "evidence_id": p["evidence_id"],
            }
        elif key in tr:
            p = tr[key]["points"][-1]
            item |= {
                "available": True,
                "value": p["value"],
                "unit": p["unit"],
                "period_end": p["period_end"],
                "change": tr[key]["changes"][-1] if tr[key]["changes"] else None,
                "evidence_id": p["evidence_id"],
            }
        elif key in pr:
            c = pr[key]["company"]
            item |= {
                "available": True,
                "value": c["value"],
                "unit": c["unit"],
                "period_end": c["period_end"],
                "evidence_id": c["evidence_id"],
            }
        if key in pr and pr[key].get("median"):
            item["peer_median"] = pr[key]["median"]
        out.append(item)
    return out


def trajectory(analysis: dict) -> dict:
    """improving / deteriorating / mixed / stable from the latest annual change of up to four measures."""
    ups = downs = 0
    used = []
    by = {r["key"]: r for r in analysis["trend"]["annual"]}
    for key in ("net_profit", "eps", "roe", "total_income", "revenue", "nii"):
        r = by.get(key)
        if not r or not r["changes"]:
            continue
        d = direction(r["changes"][-1])
        used.append((r["label"], d, r["changes"][-1]["period"]))
        ups += d == "up"
        downs += d == "down"
        if len(used) == 4:
            break
    if not used:
        return {
            "label": "cannot be established",
            "basis": "no annual change is available",
            "used": [],
        }
    if ups >= 2 and downs == 0:
        label = "improving"
    elif downs >= 2 and ups == 0:
        label = "deteriorating"
    elif ups == 0 and downs == 0:
        label = "stable"
    else:
        label = "mixed"
    return {
        "label": label,
        "used": used,
        "basis": (
            f"latest annual change of {len(used)} measure(s): {ups} up, {downs} down, the rest within "
            f"{STABLE_PCT}% / {STABLE_PP} points"
        ),
    }


def _valuation_text(val: dict) -> str:
    parts = []
    snap = val["snapshot"]
    for k in ("pe", "pb"):
        if snap[k]:
            parts.append(f"{k.upper()} {Decimal(snap[k]['value']).quantize(Decimal('0.01'))}x")
    for r in val["peer"]["rows"]:
        if "premium_pct" in r:
            prem = Decimal(r["premium_pct"]).quantize(Decimal("0.1"))
            parts.append(
                f"{r['label']} {'premium' if prem > 0 else 'discount'} to peers {abs(prem)}%"
            )
    intr = val.get("intrinsic", {})
    if intr.get("completed") and intr["scenarios"]["base"].get("justified_pb"):
        parts.append(
            f"justified P/B {intr['scenarios']['base']['justified_pb']}x in the base scenario "
            "(illustrative assumptions)"
        )
    lead = "; ".join(parts) + ". " if parts else ""
    return (
        lead
        + "These are measurements against peers and scenario-implied values under stated assumptions, "
        "not a verdict on cheapness; the verdict in section 7 uses them under its stated rules."
    )


def _answers(analysis, bull, bear, traj, score, missing, monitoring) -> list[dict]:
    peers, val = analysis["peers"], analysis["valuation"]
    ans = []

    def a(q, text, ids=()):
        ans.append({"question": q, "answer": text, "evidence_ids": sorted({i for i in ids if i})})

    heads = ", ".join(
        f"{r['label'].lower()} {_fmt(r['company']['value'], r['kind'])}"
        for r in peers["rows"]
        if r["key"] in ("roe", "gnpa", "nim", "ebit_margin")
    )
    score_text = (
        f" The financial score is {score['overall']} / 100 ({str(score['methodology']).lower()}, "
        f"confidence {str(score['confidence']).lower()})."
        if score.get("available")
        else " No financial score is available."
    )
    a(
        "What does the financial evidence suggest?",
        f"{len(bull)} supportive and {len(bear)} adverse evidence-based points were found"
        + (f"; headline measures: {heads}." if heads else ".")
        + score_text,
    )
    a(
        "Is financial performance improving, deteriorating or stable?",
        f"{traj['label'].capitalize()} ({traj['basis']}).",
    )
    s = peers["summary"]
    if s["ranked_metrics"]:
        a(
            "How does the company compare with its peers?",
            f"Ranked on {s['ranked_metrics']} metric(s): better than the peer median on "
            f"{', '.join(s['better']) or 'none'}; worse on {', '.join(s['worse']) or 'none'}; in line "
            f"on {', '.join(s['in_line']) or 'none'}.",
        )
    else:
        a(
            "How does the company compare with its peers?",
            "Cannot be established: fewer than three peers have comparable figures for any metric.",
        )
    hist = max(
        (
            len(r["points"])
            for r in analysis["trend"]["annual"]
            if r["key"] in ("roe", "net_profit")
        ),
        default=0,
    )
    a(
        "Is profitability sustainable?",
        f"Cannot be established from this evidence. {hist} year(s) of annual history are stored for "
        f"profit and ROE; {len(analysis['trend']['unusual'])} unusual annual movement(s) are flagged for "
        "explanation. Sustainability needs forward-looking evidence that is not part of this edition.",
    )
    a("What does the valuation indicate?", _valuation_text(val))
    a(
        "What are the most important upside drivers?",
        " ".join(p["text"] for p in bull[:2]) or "None established from the evidence.",
        [i for p in bull[:2] for i in p["evidence_ids"]],
    )
    a(
        "What are the most important downside risks?",
        " ".join(p["text"] for p in bear[:2])
        or "No adverse evidence-based point was found; sector risks are listed separately.",
        [i for p in bear[:2] for i in p["evidence_ids"]],
    )
    gaps = list(peers["unavailable_metrics"])
    text = ""
    if missing:
        text += "Missing requirements: " + ", ".join(missing) + ". "
    if gaps:
        text += "Metrics with no company value: " + ", ".join(gaps) + ". "
    if val["not_completed"]:
        text += "Not completed: " + "; ".join(val["not_completed"])
    a("What evidence is still missing?", text or "No material gap was recorded.")
    mon = [m["label"] for m in monitoring if m["available"]][:5]
    a(
        "What should be monitored?",
        (", ".join(mon) + ".") if mon else "No indicator has a stored value.",
    )
    return ans


def build(
    analysis: dict,
    sector: Sector,
    *,
    score: dict,
    missing: list[str],
    mgmt: dict,
    applied: list[dict],
    auditor_flags,
) -> dict:
    signals = _peer_signals(analysis) + _trend_signals(analysis) + _valuation_signals(analysis)
    all_bear = [s for s in signals if s["side"] == "bear"]
    bull = [s for s in signals if s["side"] == "bull"][:MAX_POINTS]
    bear = all_bear[:MAX_POINTS]
    traj = trajectory(analysis)
    monitoring = _monitoring(analysis, sector)
    return {
        "version": VERSION,
        "bull": bull,
        "bear": bear,
        "catalysts": _catalysts(bull, mgmt, applied),
        "risks": _risks(analysis, sector, all_bear, score, auditor_flags),
        "monitoring": monitoring,
        "trajectory": traj,
        "conclusion": {
            "answers": _answers(analysis, bull, bear, traj, score, missing, monitoring),
            "recommendation": "see the verdict block (EquityOS verdict methodology v1)",
        },
        "rules": (
            "points come from stated rules over the analysis: peer rank against the peer median, the "
            f"direction of the latest annual and quarterly changes (stable below {STABLE_PCT}% / "
            f"{STABLE_PP} points), and a P/E or P/B beyond {PREMIUM_PCT}% of the peer median; "
            "hypotheses and sector templates are labelled"
        ),
    }
