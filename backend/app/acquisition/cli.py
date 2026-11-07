"""Command line for data acquisition. Run from ./backend:

python -m app.acquisition.cli probe
python -m app.acquisition.cli nse-equity-list [--dry-run]
python -m app.acquisition.cli prices --exchange NSE --from 2026-11-03 --to 2026-11-06
python -m app.acquisition.cli wikidata [--apply] [--limit 500]
"""

import argparse
import sys
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.acquisition.factory import build_client
from app.acquisition.http import PoliteClient
from app.core.database import get_engine
from app.master_data.importer import SourceFile, import_master_data
from app.models import FetchStatus
from app.providers.company_ir import CompanyIrProvider
from app.providers.exchange_files import (
    BhavcopyProvider,
    NseEquityListProvider,
    store_prices,
)
from app.providers.feeds import CompanyNewsProvider, RbiPressReleases, SebiAnnouncements
from app.providers.nse_history import (
    NseNameHistoryProvider,
    apply_proposals,
    build_proposals,
    parse_name_changes,
    parse_symbol_changes,
)

NO_FILE = (FetchStatus.NOT_FOUND, FetchStatus.INVALID_CONTENT)


def cmd_probe(client: PoliteClient, args) -> int:
    """Fetch one small thing from each source and say plainly what happened."""
    checks = [
        ("RBI press releases", lambda: RbiPressReleases(client).latest().fetch),
        ("SEBI announcements", lambda: SebiAnnouncements(client).latest().fetch),
        (
            "Company news (Google RSS)",
            lambda: CompanyNewsProvider(client).headlines("Infosys").fetch,
        ),
        ("NSE equity list", lambda: NseEquityListProvider(client).fetch()),
        (
            "Company site (ril.com)",
            lambda: CompanyIrProvider(client).discover("https://www.ril.com/").fetch,
        ),
        (
            "Company site (tcs.com)",
            lambda: CompanyIrProvider(client).discover("https://www.tcs.com/").fetch,
        ),
    ]
    for name, run in checks:
        result = run()
        note = f"  ({result.error})" if result.error else ""
        print(f"{name:30} {result.status.value:18} http={result.http_status}{note}")
    return 0


def cmd_nse_equity_list(client: PoliteClient, args) -> int:
    provider = NseEquityListProvider(client)
    sources = []
    for filename, fetch in (
        ("EQUITY_L.csv", provider.fetch()),
        ("SME_EQUITY_L.csv", provider.fetch_sme()),
    ):
        if not fetch.ok:
            print(f"Could not fetch NSE {filename}: {fetch.status.value} {fetch.error or ''}")
            return 1
        sources.append(SourceFile("NSE", filename, fetch.content or b""))
    with Session(get_engine()) as session:
        report = import_master_data(session, sources)
        if args.dry_run:
            session.rollback()
            print("DRY RUN: nothing was saved.")
        else:
            session.commit()
    print(report.summary())
    return 0


def weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def cmd_prices(client: PoliteClient, args) -> int:
    provider = BhavcopyProvider(client, args.exchange)
    start, end = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    totals = {"days": 0, "no_file": 0, "stored": 0}
    for day in weekdays(start, end):
        fetch = provider.fetch(day)
        if fetch.status in NO_FILE:
            totals["no_file"] += 1
            print(f"{day}: no file ({fetch.status.value}) - market holiday or not yet published")
            continue
        if not fetch.ok:
            print(f"{day}: FAILED {fetch.status.value} {fetch.error or ''}")
            continue
        rows = provider.parse(fetch)
        with Session(get_engine()) as session, session.begin():
            report = store_prices(session, args.exchange, rows, fetch)
        totals["days"] += 1
        totals["stored"] += report.stored
        print(
            f"{day}: {report.rows_in_file} rows, stored {report.stored}, "
            f"already stored {report.already_stored}, not in master data {report.unmatched}"
            f"{' [cache]' if fetch.from_cache else ''}"
        )
    print(
        f"Done: {totals['days']} trading days, {totals['stored']} price rows stored, "
        f"{totals['no_file']} days without a file."
    )
    return 0


def cmd_name_history(client: PoliteClient, args) -> int:
    names, symbols = NseNameHistoryProvider(client).fetch()
    for label, fetch in (("namechange.csv", names), ("symbolchange.csv", symbols)):
        if not fetch.ok:
            print(f"Could not fetch NSE {label}: {fetch.status.value} {fetch.error or ''}")
            return 1
    with Session(get_engine()) as session:
        report = build_proposals(
            session, parse_name_changes(names.text), parse_symbol_changes(symbols.text)
        )
        for p in report.proposals[: args.show]:
            print(f"+ {p.company_name}: {p.kind} '{p.alias}'  [{p.source}]")
        if len(report.proposals) > args.show:
            print(f"  ... and {len(report.proposals) - args.show} more")
        for conflict in report.skipped_conflicts[:10]:
            print(f"! skipped (already belongs to another company): {conflict}")
        if args.apply:
            apply_proposals(session, report.proposals)
            session.commit()
            from app.services.search_index import invalidate_search_index

            invalidate_search_index()
        mode = "APPLIED" if args.apply else "DRY RUN (use --apply to save)"
        print(
            f"{mode}: {len(report.proposals)} aliases to add, "
            f"{report.already_known} already known, "
            f"{len(report.skipped_conflicts)} skipped as conflicts, "
            f"{report.unmatched_rows} rows for securities not in our data (mostly funds)."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    # Headlines contain symbols such as the rupee sign that a Windows console cannot encode.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m app.acquisition.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="Check what each source returns right now")
    p = sub.add_parser("nse-equity-list", help="Fetch the NSE equity list and import it")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("prices", help="Fetch and store official end-of-day prices")
    p.add_argument("--exchange", choices=["NSE", "BSE"], required=True)
    p.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    p = sub.add_parser("name-history", help="Add former names/symbols from NSE history files")
    p.add_argument("--apply", action="store_true", help="Save (default is a dry run)")
    p.add_argument("--show", type=int, default=15, help="How many proposals to print")
    args = parser.parse_args(argv)

    client = build_client()
    try:
        return {
            "probe": cmd_probe,
            "nse-equity-list": cmd_nse_equity_list,
            "prices": cmd_prices,
            "name-history": cmd_name_history,
        }[args.command](client, args)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
