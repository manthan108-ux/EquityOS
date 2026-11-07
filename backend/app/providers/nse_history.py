"""NSE's official company name-change and symbol-change history.

Files (static downloads on NSE's archive host):
  * namechange.csv    SYMBOL, OLD NAME OF THE COMPANY, NEW NAME OF THE COMPANY, EFFECTIVE DATE
  * symbolchange.csv  NAME OF THE COMPANY, OLD SYMBOL, NEW SYMBOL, EFFECTIVE DATE

They let people find a company by a name or ticker it no longer uses (e.g. "Zomato", now Eternal).
Each former name/symbol becomes a FORMER_NAME alias whose `source` records the NSE file and the
effective date. Aliases are only ADDED; nothing existing is changed.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.acquisition.http import FetchResult, PoliteClient
from app.core.text import normalize_text
from app.models import AliasType, Company, CompanyAlias, Exchange, Security

NAME_CHANGE_URL = "https://archives.nseindia.com/content/equities/namechange.csv"
SYMBOL_CHANGE_URL = "https://archives.nseindia.com/content/equities/symbolchange.csv"
TTL = timedelta(days=1)
MIN_ALIAS_LENGTH = 3


@dataclass(frozen=True)
class NameChange:
    symbol: str  # symbol the company traded under when the name changed
    old_name: str
    new_name: str
    effective: date | None


@dataclass(frozen=True)
class SymbolChange:
    old_symbol: str
    new_symbol: str
    effective: date | None


def _validate(first_header: str):
    def check(body: bytes, _ctype: str | None) -> str | None:
        head = body[:300].decode("utf-8-sig", errors="replace").upper()
        return None if head.lstrip().startswith(first_header) else "not the expected NSE CSV"

    return check


def _date(value: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _rows(text: str):
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [(f or "").strip().upper() for f in reader.fieldnames or []]
    for raw in reader:
        yield {k: (v or "").strip() for k, v in raw.items() if k}


def parse_name_changes(text: str) -> list[NameChange]:
    out = []
    for r in _rows(text):
        symbol, old, new = (
            r.get("SYMBOL", ""),
            r.get("OLD NAME OF THE COMPANY", ""),
            r.get("NEW NAME OF THE COMPANY", ""),
        )
        if symbol and old and new:
            out.append(NameChange(symbol.upper(), old, new, _date(r.get("EFFECTIVE DATE", ""))))
    return out


def parse_symbol_changes(text: str) -> list[SymbolChange]:
    out = []
    for r in _rows(text):
        old, new = r.get("OLD SYMBOL", "").upper(), r.get("NEW SYMBOL", "").upper()
        if old and new and old != new:
            out.append(SymbolChange(old, new, _date(r.get("EFFECTIVE DATE", ""))))
    return out


@dataclass
class AliasProposal:
    company_id: object
    company_name: str
    alias: str
    kind: str  # "former name" | "former symbol"
    source: str


@dataclass
class HistoryReport:
    proposals: list[AliasProposal] = field(default_factory=list)
    already_known: int = 0
    unmatched_rows: int = 0  # rows for securities not in our master data (mostly mutual funds)
    skipped_conflicts: list[str] = field(default_factory=list)


def build_proposals(
    session: Session, name_changes: list[NameChange], symbol_changes: list[SymbolChange]
) -> HistoryReport:
    """Work out which former names/symbols can be safely added. Does not write anything."""
    nse = session.scalar(select(Exchange).where(Exchange.code == "NSE"))
    current: dict[str, object] = {}  # NSE symbol -> company id
    if nse is not None:
        for symbol, company_id in session.execute(
            select(Security.symbol, Security.company_id).where(Security.exchange_id == nse.id)
        ):
            current[symbol.upper()] = company_id
    names = {c.id: c.common_name for c in session.scalars(select(Company))}
    existing: dict[str, set] = {}
    for company_id, normalized in session.execute(
        select(CompanyAlias.company_id, CompanyAlias.normalized_alias)
    ):
        existing.setdefault(normalized, set()).add(company_id)
    live_symbols = {s.lower(): cid for s, cid in current.items()}

    # Resolve renamed symbols forward to a symbol that exists today (A -> B -> C).
    forward = {c.old_symbol: c.new_symbol for c in symbol_changes}

    def resolve(symbol: str) -> object | None:
        for _ in range(10):
            if symbol in current:
                return current[symbol]
            if symbol not in forward:
                return None
            symbol = forward[symbol]
        return None

    report = HistoryReport()
    seen: set[tuple[object, str]] = set()

    def propose(company_id, text: str, kind: str, source: str) -> None:
        norm = normalize_text(text)
        if len(norm) < MIN_ALIAS_LENGTH or len(text) > 200:
            return
        if company_id in existing.get(norm, set()) or (company_id, norm) in seen:
            report.already_known += 1
            return
        others = existing.get(norm, set()) - {company_id}
        if others or live_symbols.get(norm, company_id) != company_id:
            # The text already identifies, or is the live ticker of, a different company.
            report.skipped_conflicts.append(f"'{text}' for {names[company_id]}")
            return
        seen.add((company_id, norm))
        report.proposals.append(AliasProposal(company_id, names[company_id], text, kind, source))

    for change in name_changes:
        company_id = resolve(change.symbol)
        if company_id is None:
            report.unmatched_rows += 1
            continue
        when = change.effective.isoformat() if change.effective else "date unknown"
        propose(company_id, change.old_name, "former name", f"NSE namechange.csv ({when})")
    for change in symbol_changes:
        company_id = resolve(change.new_symbol)
        if company_id is None:
            report.unmatched_rows += 1
            continue
        when = change.effective.isoformat() if change.effective else "date unknown"
        propose(company_id, change.old_symbol, "former symbol", f"NSE symbolchange.csv ({when})")
    return report


def apply_proposals(session: Session, proposals: list[AliasProposal]) -> int:
    for p in proposals:
        session.add(
            CompanyAlias(
                company_id=p.company_id,
                alias=p.alias,
                normalized_alias=normalize_text(p.alias),
                alias_type=AliasType.FORMER_NAME,
                source=p.source,
            )
        )
    session.flush()
    return len(proposals)


class NseNameHistoryProvider:
    source_code = "NSE_ARCHIVES"

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def fetch(self) -> tuple[FetchResult, FetchResult]:
        names = self._client.fetch(
            NAME_CHANGE_URL, source=self.source_code, ttl=TTL, validate=_validate("SYMBOL")
        )
        symbols = self._client.fetch(
            SYMBOL_CHANGE_URL, source=self.source_code, ttl=TTL, validate=_validate("NAME OF THE")
        )
        return names, symbols
