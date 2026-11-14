# ruff: noqa: E501
"""Command line for the valuation assumption ledger.

    python -m app.research.assumption_cli list    --symbol HDFCBANK
    python -m app.research.assumption_cli set     --symbol HDFCBANK --input risk_free_rate --value 6.9 \\
        --as-of 2026-11-12 --author "A. Analyst" --source "RBI: 10-year G-sec yield" \\
        --rationale "benchmark 10-year government security yield on the as-of date"
    python -m app.research.assumption_cli set     --symbol HDFCBANK --input terminal_growth --scenario base \\
        --value 6 --as-of 2026-11-14 --author "A. Analyst" --rationale "long-run nominal growth, own view"
    python -m app.research.assumption_cli history --symbol HDFCBANK --input terminal_growth --scenario base
    python -m app.research.assumption_cli withdraw --id 123 --by "A. Analyst" --reason "superseded by a new view"

Entering a value for an input that already has one edits it (the old value is kept as superseded). Nothing
has a default; an input that was never entered does not exist.
"""

import argparse
from datetime import date

from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.providers.cli import find_company
from app.research import assumptions as asm


def _print(item: dict) -> None:
    unit = "%" if item["unit"] == "percent" else ""
    scen = f" [{item['scenario']}]" if item["scenario"] else ""
    src = item["source"] or f"{item['author']} (own assumption)"
    flag = " STALE" if item["stale"] else ""
    print(f"  #{item['evidence_id']:<6} {item['label']}{scen}: {item['value']}{unit}  as of {item['as_of']}{flag}  | {item['class']} | {src}  | {item['status']}")  # fmt: skip
    if item.get("rationale"):
        print(f"           why: {item['rationale']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="assumption_cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("list", "set", "history"):
        s = sub.add_parser(name)
        s.add_argument("--symbol", required=True)
        if name in ("set", "history"):
            s.add_argument("--input", required=True, choices=sorted(asm.SPECS))
            s.add_argument("--scenario", choices=asm.SCENARIOS)
        if name == "set":
            s.add_argument("--value", required=True)
            s.add_argument(
                "--as-of", required=True, help="YYYY-MM-DD: the date the figure refers to"
            )
            s.add_argument("--author", required=True)
            s.add_argument("--rationale", required=True)
            s.add_argument(
                "--source", help="the named source; without it the input is a user assumption"
            )
            s.add_argument("--source-url")
            s.add_argument("--edit-reason")
    w = sub.add_parser("withdraw")
    w.add_argument("--id", type=int, required=True)
    w.add_argument("--by", required=True)
    w.add_argument("--reason", required=True)
    args = p.parse_args(argv)
    with Session(get_engine()) as session:
        try:
            if args.cmd == "withdraw":
                r = asm.withdraw_input(session, args.id, args.reason, by=args.by)
                session.commit()
                print(f"withdrawn #{r.id}: {r.status_reason}")
                return 0
            company = find_company(session, args.symbol)
            if args.cmd == "list":
                inputs = asm.current_inputs(session, company.id)
                print(f"{company.common_name}: {len(inputs)} active valuation input(s)")
                for item in inputs.values():
                    _print(item)
                missing = asm.ledger_view(inputs, {"completed": False})["market_inputs_missing"]
                if missing:
                    print("  not entered (none is assumed): " + ", ".join(missing))
            elif args.cmd == "history":
                for item in asm.history(session, company.id, args.input, args.scenario):
                    _print(item)
            else:
                r = asm.set_input(
                    session, company.id, args.input, args.value, author=args.author, rationale=args.rationale,
                    as_of=date.fromisoformat(args.as_of), scenario=args.scenario, source=args.source,
                    source_url=args.source_url, edit_reason=args.edit_reason,
                )  # fmt: skip
                session.commit()
                _print(asm.current_inputs(session, company.id)[r.metric])
        except asm.AssumptionError as exc:
            session.rollback()
            print(f"refused: {exc}")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
