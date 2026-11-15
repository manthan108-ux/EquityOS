"""Activate valuation inputs for the pilot companies.

    python -m app.research.setup_cli market  [--symbols A,B]   enter the dated market inputs
    python -m app.research.setup_cli history [--symbols A,B]   enter the history-rule company inputs
    python -m app.research.setup_cli show    [--symbols A,B]   what is entered

Nothing is assumed: an input that cannot be sourced or computed is reported and left unset.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.models import Company
from app.pipeline.cli import _pilot_symbols
from app.research import assumptions as asm
from app.research import valuation_setup as vs


def _company(session: Session, symbol: str):
    cid = session.execute(
        text("select company_id from securities where symbol=:s and security_code is null limit 1"),
        {"s": symbol},
    ).scalar()
    return session.get(Company, cid) if cid else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="setup_cli")
    ap.add_argument("cmd", choices=["market", "history", "show"])
    ap.add_argument("--symbols", help="comma separated; default: the pilot universe")
    args = ap.parse_args(argv)
    symbols = args.symbols.split(",") if args.symbols else _pilot_symbols()
    with Session(get_engine()) as s:
        for sym in symbols:
            c = _company(s, sym)
            if c is None:
                print(f"{sym:11} not found")
                continue
            try:
                if args.cmd == "market":
                    print(f"{sym:11} " + "; ".join(vs.apply_market(s, c, sym)))
                elif args.cmd == "history":
                    done, why = vs.apply_history(s, c)
                    print(f"{sym:11} entered: {', '.join(done) or 'none'}")
                    for k, v in why.items():
                        print(f"{'':11}   not computed - {k}: {v}")
                else:
                    inputs = asm.current_inputs(s, c.id)
                    print(
                        f"{sym:11} {len(inputs)} active input(s): "
                        + ", ".join(sorted(k.split(".", 1)[1] for k in inputs))
                    )
                s.commit()
            except asm.AssumptionError as exc:
                s.rollback()
                print(f"{sym:11} refused: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
