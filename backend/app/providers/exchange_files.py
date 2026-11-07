"""Official exchange files published as static downloads.

  * NSE equity list   https://archives.nseindia.com/content/equities/EQUITY_L.csv
  * NSE bhavcopy      https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
  * BSE bhavcopy      https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV

These are public files on the exchanges' own download paths. The exchanges' interactive APIs are
protected against automated access and are NOT used. The data is for our own research use; it must
not be redistributed. Prices are unadjusted for corporate actions.
"""

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.acquisition.http import FetchResult, PoliteClient
from app.models import EodPrice, Exchange, Security

NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_SME_LIST_URL = "https://archives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
NSE_BHAV_URL = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
BSE_BHAV_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"
)
IMMUTABLE = timedelta(days=3650)  # a closed trading day's file never changes
EQUITY_LIST_TTL = timedelta(hours=12)


@dataclass(frozen=True)
class PriceRow:
    symbol: str  # NSE symbol, or BSE security code (6 digits) for BSE rows
    trade_date: date
    series: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    last: Decimal | None
    prev_close: Decimal | None
    volume: int | None
    turnover_inr: Decimal | None
    trades: int | None
    delivery_qty: int | None
    delivery_pct: Decimal | None


# ---- content validators (reject HTTP-200 error pages) ---------------------------------------


def _first_line(body: bytes) -> str:
    return body[:400].decode("utf-8-sig", errors="replace").splitlines()[0] if body else ""


def validate_nse_equity_list(body: bytes, _ctype: str | None) -> str | None:
    head = _first_line(body).upper()
    return None if head.startswith("SYMBOL") and "ISIN" in head else "not the NSE equity list CSV"


def validate_nse_bhav(body: bytes, _ctype: str | None) -> str | None:
    head = _first_line(body).upper()
    ok = head.startswith("SYMBOL") and "CLOSE_PRICE" in head
    return None if ok else "not an NSE bhavcopy CSV"


def validate_bse_bhav(body: bytes, _ctype: str | None) -> str | None:
    head = _first_line(body)
    ok = head.startswith("TradDt") and "FinInstrmId" in head
    return None if ok else "not a BSE bhavcopy CSV"


# ---- parsing --------------------------------------------------------------------------------


def _num(value: str | None) -> Decimal | None:
    v = (value or "").strip()
    if v in ("", "-", "NA"):
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def _int(value: str | None) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def parse_nse_bhavcopy(text: str) -> list[PriceRow]:
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [f.strip() for f in reader.fieldnames or []]
    rows = []
    for raw in reader:
        row = {k: (v or "").strip() for k, v in raw.items() if k}
        try:
            trade_date = datetime.strptime(row["DATE1"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue  # a row without a valid date cannot be stored truthfully
        turnover_lacs = _num(row.get("TURNOVER_LACS"))
        rows.append(
            PriceRow(
                symbol=row["SYMBOL"].upper(),
                trade_date=trade_date,
                series=row.get("SERIES", ""),
                open=_num(row.get("OPEN_PRICE")),
                high=_num(row.get("HIGH_PRICE")),
                low=_num(row.get("LOW_PRICE")),
                close=_num(row.get("CLOSE_PRICE")),
                last=_num(row.get("LAST_PRICE")),
                prev_close=_num(row.get("PREV_CLOSE")),
                volume=_int(row.get("TTL_TRD_QNTY")),
                turnover_inr=turnover_lacs * 100000 if turnover_lacs is not None else None,
                trades=_int(row.get("NO_OF_TRADES")),
                delivery_qty=_int(row.get("DELIV_QTY")),
                delivery_pct=_num(row.get("DELIV_PER")),
            )
        )
    return rows


def parse_bse_bhavcopy(text: str) -> list[PriceRow]:
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {k: (v or "").strip() for k, v in raw.items() if k}
        if row.get("Sgmt") not in (None, "", "CM") or row.get("FinInstrmTp") not in (
            None,
            "",
            "STK",
        ):
            continue
        try:
            trade_date = datetime.strptime(row["TradDt"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        rows.append(
            PriceRow(
                symbol=row["FinInstrmId"],
                trade_date=trade_date,
                series=row.get("SctySrs", ""),
                open=_num(row.get("OpnPric")),
                high=_num(row.get("HghPric")),
                low=_num(row.get("LwPric")),
                close=_num(row.get("ClsPric")),
                last=_num(row.get("LastPric")),
                prev_close=_num(row.get("PrvsClsgPric")),
                volume=_int(row.get("TtlTradgVol")),
                turnover_inr=_num(row.get("TtlTrfVal")),
                trades=_int(row.get("TtlNbOfTxsExctd")),
                delivery_qty=None,  # BSE's bhavcopy does not publish delivery data
                delivery_pct=None,
            )
        )
    return rows


# ---- providers ------------------------------------------------------------------------------


class NseEquityListProvider:
    source_code = "NSE_ARCHIVES"

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def fetch_sme(self) -> FetchResult:
        """NSE Emerge (SME platform) companies, published as a separate list."""
        return self._client.fetch(
            NSE_SME_LIST_URL,
            source=self.source_code,
            ttl=EQUITY_LIST_TTL,
            validate=validate_nse_equity_list,
        )

    def fetch(self) -> FetchResult:
        return self._client.fetch(
            NSE_EQUITY_LIST_URL,
            source=self.source_code,
            ttl=EQUITY_LIST_TTL,
            validate=validate_nse_equity_list,
        )


class BhavcopyProvider:
    """Fetches and parses one exchange's end-of-day file for a date."""

    def __init__(self, client: PoliteClient, exchange: str) -> None:
        if exchange not in ("NSE", "BSE"):
            raise ValueError(f"No bhavcopy provider for exchange '{exchange}'")
        self._client, self.exchange = client, exchange
        self.source_code = "NSE_ARCHIVES" if exchange == "NSE" else "BSE_BHAVCOPY"

    def url(self, d: date) -> str:
        return (NSE_BHAV_URL if self.exchange == "NSE" else BSE_BHAV_URL).format(d=d)

    def fetch(self, d: date) -> FetchResult:
        validator = validate_nse_bhav if self.exchange == "NSE" else validate_bse_bhav
        # Today's file may still be updated, so only past days are treated as immutable.
        ttl = IMMUTABLE if d < datetime.now(UTC).date() else timedelta(hours=1)
        return self._client.fetch(self.url(d), source=self.source_code, ttl=ttl, validate=validator)

    def parse(self, result: FetchResult) -> list[PriceRow]:
        text = result.text
        return parse_nse_bhavcopy(text) if self.exchange == "NSE" else parse_bse_bhavcopy(text)


# ---- storing prices -------------------------------------------------------------------------


@dataclass
class StoreReport:
    rows_in_file: int
    stored: int
    already_stored: int
    unmatched: int  # rows whose security is not in our master data (bonds, ETFs, SME, ...)


def store_prices(
    session: Session, exchange: str, rows: list[PriceRow], result: FetchResult
) -> StoreReport:
    """Insert prices for securities we know. Existing rows are never overwritten."""
    exchange_row = session.scalar(select(Exchange).where(Exchange.code == exchange))
    if exchange_row is None:
        raise ValueError(f"Exchange {exchange} is not in the database")
    key_column = Security.symbol if exchange == "NSE" else Security.security_code
    ids = dict(
        session.execute(
            select(key_column, Security.id).where(
                Security.exchange_id == exchange_row.id, key_column.is_not(None)
            )
        ).all()
    )
    sha = hashlib.sha256(result.content or b"").hexdigest()
    retrieved = result.retrieved_at or datetime.now(UTC)
    payload, unmatched = [], 0
    for row in rows:
        security_id = ids.get(row.symbol)
        if security_id is None:
            unmatched += 1
            continue
        payload.append(
            {
                "security_id": security_id,
                "trade_date": row.trade_date,
                "series": row.series,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "last": row.last,
                "prev_close": row.prev_close,
                "volume": row.volume,
                "turnover_inr": row.turnover_inr,
                "trades": row.trades,
                "delivery_qty": row.delivery_qty,
                "delivery_pct": row.delivery_pct,
                "source_url": result.url,
                "file_sha256": sha,
                "retrieved_at": retrieved,
            }
        )
    stored = 0
    for start in range(0, len(payload), 2000):
        chunk = payload[start : start + 2000]
        stmt = (
            insert(EodPrice)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=["security_id", "trade_date", "series"])
            .returning(EodPrice.id)
        )
        # RETURNING counts the rows actually inserted; rowcount is -1 for multi-row inserts.
        stored += len(session.execute(stmt).all())
    return StoreReport(len(rows), stored, len(payload) - stored, unmatched)
