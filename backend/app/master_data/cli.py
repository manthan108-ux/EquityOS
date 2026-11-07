"""Import the official NSE/BSE security lists.

Usage (from ./backend):
    python -m app.master_data.cli --nse path/to/EQUITY_L.csv --bse path/to/bse_scrips.csv

Download the files yourself from the exchange websites (see docs/DATA_SOURCES.md); this tool
deliberately does not scrape the exchanges.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.master_data.importer import SourceFile, import_master_data
from app.master_data.parsers import MasterFileError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import official exchange security lists.")
    parser.add_argument("--nse", type=Path, help="NSE EQUITY_L.csv")
    parser.add_argument("--bse", type=Path, help="BSE list-of-scrips CSV (Equity)")
    args = parser.parse_args(argv)

    sources = [
        SourceFile(exchange, path.name, path.read_bytes())
        for exchange, path in (("NSE", args.nse), ("BSE", args.bse))
        if path is not None
    ]
    if not sources:
        parser.error("Provide at least one of --nse / --bse")

    try:
        with Session(get_engine()) as session, session.begin():
            report = import_master_data(session, sources)
    except (MasterFileError, OSError) as exc:
        print(f"Import failed, nothing was changed: {exc}", file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
