# ruff: noqa: E501
"""python -m app.research.backtest_cli run [--symbols A,B] [--basis CONSOLIDATED]

Back-tests the verdict rule subset on the stored history (read-only) and writes backtest_latest.json."""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.models import Security
from app.pipeline.cli import _pilot_symbols
from app.research import backtest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="backtest_cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--symbols", help="comma list (default: the 24 pilot companies)")
    r.add_argument("--basis", default="CONSOLIDATED")
    sub.add_parser("show")
    args = p.parse_args(argv)
    if args.cmd == "show":
        print(backtest.summary())
        return 0
    symbols = args.symbols.split(",") if args.symbols else _pilot_symbols()
    with Session(get_engine()) as s:
        companies = []
        for sym in symbols:
            cid = s.scalar(select(Security.company_id).where(Security.symbol == sym).limit(1))
            if cid is None:
                print(f"unknown symbol {sym}", file=sys.stderr)
                continue
            companies.append((cid, sym))
        res = backtest.run(s, companies, args.basis)
    path = backtest.save(res)
    print(f"{res['observations']} observations, {res['first_as_of']} to {res['last_as_of']} -> {path}")
    for h, block in res["results"].items():
        d = block["default_rule"]
        print(f"horizon {h}d default rule: {d['reading']}")
        for k, b in d["buckets"].items():
            print(f"  {k}: {b}")
    for c in res["threshold_validation"]["checks"]:
        print(("OK  " if c["ok"] else "NOTE"), c["check"], "-", c["detail"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
