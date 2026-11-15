# ruff: noqa: E501
"""Sum-of-the-parts valuation: each reported segment valued on its own result at an ENTERED multiple.

Used only where it is appropriate: a company that reports several segments with a result (automobiles and
energy groups, diversified groups). It is not used for a bank or for an essentially single-business company.

* Segment results come from the stored segment note (revenue and result by segment, latest annual period,
  one accounting basis), exactly as the company defines them (EBIT, EBITDA or "result"; the measure is shown).
* The multiple of each segment is an input in the assumption ledger (`sotp_multiple`, per segment and per
  scenario) with its source or author, date and rationale. There is NO default multiple, and the multiple must
  suit the measure the company reports (an EV/EBIT multiple for an EBIT result, and so on): that is the
  entering person's responsibility, and the measure is displayed next to it.
* Segment value = segment result x entered multiple. A segment with a zero or negative result is not valued by
  a profit multiple and is listed. Enterprise value = the sum of the segment values; equity value = enterprise
  value - net debt - non-controlling interests; value per share = equity value / shares.
* Not deducted (and said so): central or unallocated costs, and assets not in any segment. Segment results
  before them overstate the group's profit, so the value is an upper bound on what the segments alone support.
* A scenario missing any segment's multiple, or any bridge item, is NOT COMPLETED and lists what is missing.
"""

from decimal import Decimal

from app.research import assumptions as asm
from app.research import profile as pr
from app.research.analysis import _s

VERSION = "1.0"
MIN_SEGMENTS = 2
HUNDRED = Decimal(100)
OFFSETS = (Decimal(-2), Decimal(-1), Decimal(0), Decimal(1), Decimal(2))


def _d(x) -> Decimal:
    return Decimal(str(x))


def segment_results(store, basis: str) -> dict:
    """{segment: (result record, revenue record or None)} for the latest annual period with results."""
    res = pr._by_dimension(store, pr.SEG_RES, "ANNUAL", basis)
    rev = pr._by_dimension(store, pr.SEG_REV, "ANNUAL", basis)
    if not res:
        return {}
    latest = max(d for per in res.values() for d in per)
    return {
        name: (per[latest], (rev.get(name) or {}).get(latest))
        for name, per in res.items()
        if latest in per
    }


def build(
    store, basis: str, sector_code: str, act: dict, inputs: dict, price_v: Decimal | None
) -> dict:
    if sector_code not in ("AUTOMOBILES", "ENERGY", "CONGLOMERATE", "GENERIC_NONFIN"):
        return {"applicable": False, "reason": "a sum of the parts is not used for a company that is essentially one business: segment values would add nothing the group multiple does not already give"}  # fmt: skip
    segs = segment_results(store, basis)
    if len(segs) < MIN_SEGMENTS:
        return {"applicable": False, "reason": f"fewer than {MIN_SEGMENTS} segments report a result in the stored segment note on the {basis.lower()} basis ({len(segs)} found), so there is nothing to sum"}  # fmt: skip
    period = max(r.period_end for r, _ in segs.values())
    rows = []
    for name, (r, rev) in sorted(segs.items(), key=lambda kv: -kv[1][0].value):
        rows.append({"segment": name, "result": _s(r.value), "measure": r.metric.split(".", 1)[1].replace("segment_result", "result").replace("_", " ") or "result", "revenue": _s(rev.value) if rev is not None else None, "evidence_id": r.id, "valued": r.value > 0})  # fmt: skip
    scenarios, flags_common = {}, ["central or unallocated costs and assets outside the segments are not deducted: the value is an upper bound on what the segments alone support"]  # fmt: skip
    for s in asm.SCENARIOS:
        missing, parts = [], []
        for name, (r, _rev) in segs.items():
            if r.value <= 0:
                parts.append({"segment": name, "result": r.value, "multiple": None, "value": Decimal(0), "note": "a zero or negative result is not valued by a profit multiple", "evidence_ids": [r.id]})  # fmt: skip
                continue
            item = inputs.get(f"{asm.PREFIX}.sotp_multiple_{asm.slug(name)}_{s}")
            if item is None:
                missing.append(f"valuation multiple for the {name} segment in the {s} scenario")
                continue
            m = _d(item["value"])
            parts.append({"segment": name, "result": r.value, "multiple": m, "value": r.value * m, "input": item, "evidence_ids": [r.id, item["evidence_id"]]})  # fmt: skip
        for key, label in (("net_debt", "net debt"), ("shares", "shares outstanding")):
            if not act.get(key):
                missing.append(f"stored {label}")
        if missing:
            scenarios[s] = {"completed": False, "missing": missing}
            continue
        ev = sum((p["value"] for p in parts), Decimal(0))
        nci = (
            _d(act["non_controlling_interests"]["value"])
            if act.get("non_controlling_interests")
            else Decimal(0)
        )
        nd, shares = _d(act["net_debt"]["value"]), _d(act["shares"]["value"])
        equity = ev - nd - nci
        per_share = equity / shares if shares > 0 else None
        ids = sorted({i for p in parts for i in p["evidence_ids"]} | {act[k]["evidence_id"] for k in ("net_debt", "shares")} | ({act["non_controlling_interests"]["evidence_id"]} if act.get("non_controlling_interests") else set()))  # fmt: skip
        flags = list(flags_common)
        flags += sorted(
            {
                f"{p['input']['label']} for {p['segment']} is stale"
                for p in parts
                if p.get("input", {}).get("stale")
            }
        )
        if not act.get("non_controlling_interests"):
            flags.append("non-controlling interests are not stored and are not deducted")
        if act["net_debt"]["period_end"] != period.isoformat():
            flags.append(f"net debt is as at {act['net_debt']['period_end']} but segment results are for the year ended {period.isoformat()}")  # fmt: skip
        out = {
            "completed": True,
            "parts": [
                {
                    "segment": p["segment"],
                    "result": _s(p["result"]),
                    "multiple": _s(p["multiple"]) if p["multiple"] is not None else None,
                    "value": _s(p["value"].quantize(Decimal("1"))),
                    "note": p.get("note"),
                    "source": (p.get("input") or {}).get("source"),
                    "author": (p.get("input") or {}).get("author"),
                    "as_of": (p.get("input") or {}).get("as_of"),
                    "rationale": (p.get("input") or {}).get("rationale"),
                    "evidence_ids": p["evidence_ids"],
                }
                for p in parts
            ],  # fmt: skip
            "enterprise_value": _s(ev.quantize(Decimal("1"))),
            "bridge": {
                "net_debt": _s(nd.quantize(Decimal("1"))),
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
                out["vs_price_pct"] = _s(
                    ((per_share / price_v - 1) * HUNDRED).quantize(Decimal("0.1"))
                )
        scenarios[s] = out
    sens = None
    base = scenarios.get("base")
    if base and base["completed"]:
        parts_b = [
            (_d(p["result"]), _d(p["multiple"])) for p in base["parts"] if p["multiple"] is not None
        ]
        nd, nci, shares = _d(base["bridge"]["net_debt"]), _d(base["bridge"]["non_controlling_interests"]), _d(base["bridge"]["shares"])  # fmt: skip
        sens = {
            "rows": [
                {
                    "multiple_offset": _s(o),
                    "value_per_share": _s(
                        (
                            (
                                sum((r * max(m + o, Decimal(0)) for r, m in parts_b), Decimal(0))
                                - nd
                                - nci
                            )
                            / shares
                        ).quantize(Decimal("1"))
                    ),
                }
                for o in OFFSETS
            ],  # fmt: skip
            "note": "value per share (INR) with every segment's entered multiple offset by the same number of turns: a grid of hypotheticals around the entered base case, not inputs",  # fmt: skip
        }
    done = [s for s, v in scenarios.items() if v["completed"]]
    return {
        "version": VERSION,
        "applicable": True,
        "completed": "base" in done,
        "scenarios_complete": done,
        "period_end": period.isoformat(),
        "basis": basis,
        "segments": rows,
        "scenarios": scenarios,
        "sensitivity": sens,
        "method": [
            "Segment value = the segment's reported result x the multiple entered for it in the assumption ledger (per segment and scenario).",
            "Enterprise value = the sum of segment values; equity value = enterprise value - net debt - non-controlling interests; value per share = equity value / shares.",
            "A segment with a zero or negative result is not valued by a profit multiple.",
            "Central or unallocated costs and assets outside the segments are not deducted.",
        ],  # fmt: skip
        "needs": "an entered multiple for every segment with a positive result, in each scenario (sotp_multiple, per segment and scenario)",
        "how_to_enter": 'python -m app.research.assumption_cli set --symbol SYMBOL --input sotp_multiple --segment "SEGMENT NAME" --scenario bear|base|bull --value V --as-of YYYY-MM-DD --author NAME --rationale TEXT [--source NAME]',  # fmt: skip
    }
