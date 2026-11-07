"""Parsers for the exchanges' official security-list files.

Sources (public downloads from the exchange websites, fetched by a person in a browser):
  * NSE  "Securities available for trading"  -> EQUITY_L.csv
  * BSE  "List of Scrips" (Equity, CSV)

Column headers are matched loosely (case, spaces and punctuation ignored). If a REQUIRED column is
missing the parse fails with a clear error instead of guessing: the file layout has probably
changed and silently mis-reading it would corrupt company master data.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.core.identifiers import is_valid_isin
from app.models import SecurityStatus

_BSE_CODE = re.compile(r"^[0-9]{6}$")


class MasterFileError(ValueError):
    """The file is unreadable or does not have the expected columns."""


@dataclass(frozen=True)
class MasterRow:
    exchange: str  # "NSE" | "BSE"
    symbol: str  # NSE symbol or BSE scrip id
    security_code: str | None  # BSE 6-digit code
    company_name: str
    security_name: str | None
    isin: str
    series: str | None  # NSE series or BSE group
    face_value: Decimal | None
    listing_date: date | None
    status: SecurityStatus
    industry: str | None  # exchange-published industry label (BSE)


@dataclass
class ParsedFile:
    exchange: str
    rows: list[MasterRow] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    skipped_examples: dict[str, list[str]] = field(default_factory=dict)  # reason -> a few rows
    total_rows: int = 0

    def skip(self, reason: str, example: str = "") -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        examples = self.skipped_examples.setdefault(reason, [])
        if example and len(examples) < 5:
            examples.append(example)

    @property
    def skipped_count(self) -> int:
        return sum(self.skipped.values())


def decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MasterFileError("File is not valid UTF-8 or Windows-1252 text.")


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _read(text: str, required: dict[str, tuple[str, ...]], optional: dict[str, tuple[str, ...]]):
    """Yield dicts keyed by our field names. `required`/`optional` map field -> accepted headers."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise MasterFileError("File is empty.") from exc
    index = {_key(h): i for i, h in enumerate(header)}

    def locate(candidates: tuple[str, ...]) -> int | None:
        for candidate in candidates:
            if candidate in index:
                return index[candidate]
        return None

    columns: dict[str, int | None] = {}
    missing = []
    for name, candidates in required.items():
        columns[name] = locate(candidates)
        if columns[name] is None:
            missing.append(name)
    if missing:
        raise MasterFileError(
            f"Missing required column(s) {missing}. Found headers: {[h.strip() for h in header]}"
        )
    for name, candidates in optional.items():
        columns[name] = locate(candidates)

    for raw_row in reader:
        if not any(cell.strip() for cell in raw_row):
            continue  # blank line
        yield {
            name: (raw_row[i].strip() if i is not None and i < len(raw_row) else "")
            for name, i in columns.items()
        }


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value) if value else None
    except InvalidOperation:
        return None


def _date(value: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


_COMPANY_ISIN_PREFIXES = ("INE", "IN9")


def _isin_problem(isin: str) -> str | None:
    """Why this ISIN cannot identify a listed company, or None if it can.

    'INE' (and 'IN9', used for e.g. differential-voting shares) are company securities. 'INF'
    ISINs are mutual-fund / ETF units, which are not companies, so they are excluded.
    """
    if not (isin.startswith("IN") and is_valid_isin(isin)):
        return "invalid ISIN"
    if not isin.startswith(_COMPANY_ISIN_PREFIXES):
        return f"not a company security (ISIN prefix {isin[:3]}: fund/ETF/other)"
    return None


def parse_nse_equity_list(text: str) -> ParsedFile:
    parsed = ParsedFile(exchange="NSE")
    required = {
        "symbol": ("symbol",),
        "name": ("nameofcompany", "companyname"),
        "isin": ("isinnumber", "isin"),
    }
    optional = {
        "series": ("series",),
        "listing": ("dateoflisting",),
        "face_value": ("facevalue",),
    }
    seen: set[str] = set()
    for row in _read(text, required, optional):
        parsed.total_rows += 1
        symbol = row["symbol"].upper()
        isin = row["isin"].upper()
        if not symbol:
            parsed.skip("missing symbol")
        elif not row["name"]:
            parsed.skip("missing company name")
        elif problem := _isin_problem(isin):
            parsed.skip(problem, f"{symbol} ({row['name']}) ISIN={isin or '(empty)'}")
        elif symbol in seen:
            parsed.skip("duplicate symbol in file")
        else:
            seen.add(symbol)
            parsed.rows.append(
                MasterRow(
                    exchange="NSE",
                    symbol=symbol,
                    security_code=None,
                    company_name=row["name"],
                    security_name=None,
                    isin=isin,
                    series=row["series"] or None,
                    face_value=_decimal(row["face_value"]),
                    listing_date=_date(row["listing"]),
                    status=SecurityStatus.ACTIVE,  # the NSE list contains tradable securities only
                    industry=None,
                )
            )
    return parsed


_BSE_STATUS = {"active": SecurityStatus.ACTIVE, "suspended": SecurityStatus.SUSPENDED}


def parse_bse_scrip_list(text: str) -> ParsedFile:
    parsed = ParsedFile(exchange="BSE")
    required = {
        "code": ("securitycode", "scripcode"),
        "symbol": ("securityid", "scripid"),
        "issuer": ("issuername", "companyname"),
        "isin": ("isinno", "isinnumber", "isin"),
        "status": ("status",),
    }
    optional = {
        "security_name": ("securityname", "scripname"),
        "group": ("group",),
        "face_value": ("facevalue",),
        "industry": ("industry",),
        "instrument": ("instrument",),
    }
    seen_symbols: set[str] = set()
    seen_codes: set[str] = set()
    for row in _read(text, required, optional):
        parsed.total_rows += 1
        symbol = row["symbol"].upper()
        isin = row["isin"].upper()
        status = _BSE_STATUS.get(row["status"].lower())
        instrument = row["instrument"].lower()
        if instrument and instrument != "equity":
            parsed.skip(
                "non-equity instrument",
                f"{row['code']} {symbol} ({row['issuer']}) [{row['instrument']}]",
            )
        elif not _BSE_CODE.match(row["code"]):
            parsed.skip("invalid BSE security code")
        elif not symbol:
            parsed.skip("missing symbol")
        elif not row["issuer"]:
            parsed.skip("missing issuer name")
        elif problem := _isin_problem(isin):
            parsed.skip(
                problem, f"{row['code']} {symbol} ({row['issuer']}) ISIN={isin or '(empty)'}"
            )
        elif status is None:
            parsed.skip(f"unsupported status '{row['status']}'")
        elif symbol in seen_symbols or row["code"] in seen_codes:
            parsed.skip("duplicate symbol or code in file")
        else:
            seen_symbols.add(symbol)
            seen_codes.add(row["code"])
            parsed.rows.append(
                MasterRow(
                    exchange="BSE",
                    symbol=symbol,
                    security_code=row["code"],
                    company_name=row["issuer"],
                    security_name=row["security_name"] or None,
                    isin=isin,
                    series=row["group"] or None,
                    face_value=_decimal(row["face_value"]),
                    listing_date=None,
                    status=status,
                    industry=row["industry"] or None,
                )
            )
    return parsed
