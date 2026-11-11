"""Compatibility check of the analytical report across the 24-company pilot universe (phase 7).

Read-only: for each pilot company it builds the report content in memory (nothing is stored), renders
the PDF to count pages, and prints what the analytical layer could and could not do. Use it after a
change to the analysis, the sector profiles or the renderer:

    python -m app.research.compat [--out DIR]

`--out` also writes each company's PDF into DIR.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.database import get_engine
from app.docintel.pdf import page_count
from app.models import Company, Security
from app.research import clock
from app.research import readiness as rd
from app.research import report as rp
from app.research.pdf_pro import render
from app.seeds.sample_universe import SAMPLE


def check(session, symbol: str, out: Path | None = None) -> dict:
    cid = session.scalar(
        select(Security.company_id).where(
            Security.symbol == symbol, Security.security_code.is_(None)
        )
    )
    if cid is None:
        return {"symbol": symbol, "error": "not in the master data"}
    company = session.get(Company, cid)
    row: dict = {"symbol": symbol}
    try:
        ready = rd.evaluate(session, company)
        mode = rp.COMPLETE if ready.eligibility == rd.AUTOMATIC else rp.AVAILABLE_EVIDENCE
        content = json.loads(
            json.dumps(rp.build_content(session, company, ready, mode, clock.today()), default=str)
        )
        a = content["analysis"]
        row |= {"profile": ready.profile, "eligibility": ready.eligibility, "mode": mode}
        if not a.get("available"):
            row["analysis"] = f"UNAVAILABLE: {a.get('reason')}"
            return row
        peers = a["peers"]
        val = a["valuation"]
        intr = val.get("intrinsic", {})
        row |= {
            "sector": a["sector"]["code"],
            "annual_rows": len(a["trend"]["annual"]),
            "quarterly_rows": len(a["trend"]["quarterly"]),
            "peers": len(peers["peers"]),
            "peer_rows": len(peers["rows"]),
            "ranked": peers["summary"]["ranked_metrics"],
            "hist_pe": len(val["historical"].get("rows", [])),
            "intrinsic": bool(intr.get("completed")),
            "relative": bool(val["relative"].get("completed")),
            "commentary": bool(a["management"].get("available")),
            "bull": len(a["thesis"]["bull"]),
            "bear": len(a["thesis"]["bear"]),
            "direction": a["thesis"]["trajectory"]["label"],
            "verdict": a["verdict"]["verdict"],
            "confidence": a["verdict"]["confidence"]["band"],
            "net": a["verdict"]["net_points"],
            "levels": a["verdict"]["levels"].get("target") is not None,
        }
        pdf = render(
            content,
            version=1,
            mode=mode,
            report_id="0" * 36,
            content_hash="0" * 64,
        )
        row["pages"] = page_count(pdf) or 0
        if out:
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{symbol}.pdf").write_bytes(pdf)
    except Exception as exc:  # noqa: BLE001 - the matrix reports it
        row["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    session = sessionmaker(bind=get_engine())()
    try:
        rows = [check(session, c.symbol, args.out) for c in SAMPLE]
    finally:
        session.rollback()
        session.close()
    print(
        f"{'symbol':<11}{'sector':<13}{'ann':>4}{'qtr':>4}{'peer':>5}{'rank':>5}{'hPE':>4}{'bull':>5}"
        f"{'bear':>5} {'direction':<14}{'verdict':<7}{'net':>4} {'conf':<7}{'lvls':<6}{'pg':>3}"
    )
    for r in rows:
        if "error" in r or "analysis" in r:
            print(f"{r['symbol']:<11}{r.get('error') or r['analysis']}")
            continue
        print(
            f"{r['symbol']:<11}{r['sector']:<13}{r['annual_rows']:>4}{r['quarterly_rows']:>4}"
            f"{r['peers']:>5}{r['ranked']:>5}{r['hist_pe']:>4}{r['bull']:>5}{r['bear']:>5} "
            f"{r['direction']:<14}{r['verdict']:<7}{r['net']:>+4} {r['confidence']:<7}"
            f"{str(r['levels']):<6}{r['pages']:>3}"
        )
    print(
        f"\n{sum('error' not in r for r in rows)} of {len(rows)} built without error ({date.today()})"
    )


if __name__ == "__main__":
    main()
