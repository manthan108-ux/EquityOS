"""Merge official exchange security lists into the company master tables.

Rules:
  * One company per ISIN issuer (see `issuer_key`): the NSE and BSE listings of one company merge.
  * Idempotent: importing the same file twice changes nothing.
  * Never deletes. Securities absent from a newer file are only REPORTED (they may be delisted,
    but a missing row is not proof, so we do not guess).
  * Never overwrites a company's name or verified fields; it only fills fields that are empty.
  * Every file is recorded in `master_data_imports` (name, SHA-256, row counts, skip reasons).
"""

import hashlib
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.identifiers import issuer_key
from app.core.text import normalize_text
from app.master_data.parsers import (
    MasterFileError,
    MasterRow,
    ParsedFile,
    decode_bytes,
    parse_bse_scrip_list,
    parse_nse_equity_list,
)
from app.models import (
    AliasType,
    Company,
    CompanyAlias,
    Exchange,
    MasterDataImport,
    Security,
)

_LEGAL_SUFFIX = re.compile(r"\s+(limited|ltd\.?)$", re.IGNORECASE)
_PARSERS = {"NSE": parse_nse_equity_list, "BSE": parse_bse_scrip_list}
_MAX_ALIAS = 200
_MAX_EXAMPLES = 20


@dataclass
class SourceFile:
    exchange: str  # "NSE" | "BSE"
    filename: str
    content: bytes


@dataclass
class FileReport:
    exchange: str
    filename: str
    sha256: str
    rows_total: int
    rows_imported: int
    skipped: dict[str, int]


@dataclass
class ImportReport:
    files: list[FileReport] = field(default_factory=list)
    companies_created: int = 0
    companies_matched: int = 0
    securities_created: int = 0
    securities_updated: int = 0
    securities_moved_between_companies: int = 0
    not_in_file: dict[str, int] = field(default_factory=dict)
    name_differences: list[str] = field(default_factory=list)  # existing name vs exchange name

    def summary(self) -> str:
        lines = []
        for f in self.files:
            lines.append(
                f"{f.exchange} {f.filename}: {f.rows_total} rows, {f.rows_imported} usable, "
                f"{sum(f.skipped.values())} skipped {f.skipped or ''}".rstrip()
            )
        lines += [
            f"Companies created: {self.companies_created}, "
            f"matched existing: {self.companies_matched}",
            f"Securities created: {self.securities_created}, updated: {self.securities_updated}, "
            f"moved between companies: {self.securities_moved_between_companies}",
        ]
        for exchange, count in self.not_in_file.items():
            lines.append(
                f"{exchange}: {count} stored securities were absent from the file (not deleted)"
            )
        if self.name_differences:
            lines.append(f"Name differences vs stored data (first {_MAX_EXAMPLES}):")
            lines += [f"  {d}" for d in self.name_differences]
        return "\n".join(lines)


def derive_common_name(legal_name: str) -> str:
    stripped = _LEGAL_SUFFIX.sub("", legal_name.strip())
    return stripped or legal_name.strip()


@dataclass
class _Context:
    """Lookup tables loaded once per import so rows can be merged without per-row queries."""

    session: Session
    report: ImportReport
    companies: dict[str, Company]  # issuer_key -> company
    alias_keys: set[tuple[uuid.UUID, str]]  # (company_id, normalized alias)
    by_symbol: dict[tuple[int, str], Security]  # (exchange_id, symbol)
    by_code: dict[tuple[int, str], Security]  # (exchange_id, BSE code)
    matched_company_ids: set[uuid.UUID] = field(default_factory=set)
    seen_existing_security_ids: set[int] = field(default_factory=set)
    new_securities: list[Security] = field(default_factory=list)


def import_master_data(session: Session, sources: list[SourceFile]) -> ImportReport:
    """Import the given files. Call inside a transaction; the caller commits."""
    report = ImportReport()
    exchanges = {e.code: e for e in session.scalars(select(Exchange))}

    ctx = _Context(
        session=session,
        report=report,
        companies={c.issuer_key: c for c in session.scalars(select(Company)) if c.issuer_key},
        alias_keys={
            (row[0], row[1])
            for row in session.execute(
                select(CompanyAlias.company_id, CompanyAlias.normalized_alias)
            )
        },
        by_symbol={},
        by_code={},
    )
    for sec in session.scalars(select(Security)):
        ctx.by_symbol[(sec.exchange_id, sec.symbol)] = sec
        if sec.security_code:
            ctx.by_code[(sec.exchange_id, sec.security_code)] = sec

    imported_exchanges: set[str] = set()
    for source in sources:
        if source.exchange not in _PARSERS:
            raise MasterFileError(f"Unsupported exchange '{source.exchange}'")
        exchange = exchanges.get(source.exchange)
        if exchange is None:
            raise MasterFileError(f"Exchange {source.exchange} is not in the database; seed first.")
        parsed: ParsedFile = _PARSERS[source.exchange](decode_bytes(source.content))
        sha = hashlib.sha256(source.content).hexdigest()

        record = MasterDataImport(
            exchange_code=source.exchange,
            filename=source.filename,
            sha256=sha,
            rows_total=parsed.total_rows,
            rows_imported=len(parsed.rows),
            rows_skipped=parsed.skipped_count,
            notes="; ".join(f"{r}: {n}" for r, n in sorted(parsed.skipped.items())) or None,
        )
        session.add(record)
        session.flush()
        report.files.append(
            FileReport(
                source.exchange,
                source.filename,
                sha,
                parsed.total_rows,
                len(parsed.rows),
                parsed.skipped,
            )
        )
        imported_exchanges.add(source.exchange)

        provenance = (
            f"Exchange security list import: {source.exchange} {source.filename} "
            f"(sha256 {sha[:12]})"
        )
        for row in parsed.rows:
            _merge_row(ctx, row, exchange, record.id, provenance)

    session.flush()
    seen_ids = ctx.seen_existing_security_ids | {s.id for s in ctx.new_securities}
    for code in imported_exchanges:
        exchange_id = exchanges[code].id
        stored = [s for (ex_id, _), s in ctx.by_symbol.items() if ex_id == exchange_id]
        report.not_in_file[code] = sum(1 for s in stored if s.id not in seen_ids)

    # The fuzzy-search index is cached in memory; make the next search see the new companies.
    from app.services.search_index import invalidate_search_index

    invalidate_search_index()
    return report


def _add_alias(ctx: _Context, company: Company, text: str, alias_type: AliasType) -> None:
    normalized = normalize_text(text)
    if not normalized or len(normalized) > _MAX_ALIAS or len(text) > _MAX_ALIAS:
        return
    key = (company.id, normalized)
    if key in ctx.alias_keys:
        return
    ctx.alias_keys.add(key)
    ctx.session.add(
        CompanyAlias(
            company_id=company.id, alias=text, normalized_alias=normalized, alias_type=alias_type
        )
    )


def _merge_company(ctx: _Context, row: MasterRow, provenance: str) -> Company:
    key = issuer_key(row.isin)
    company = ctx.companies.get(key)
    if company is None:
        company = Company(
            id=uuid.uuid4(),
            issuer_key=key,
            legal_name=row.company_name,
            common_name=derive_common_name(row.company_name),
            exchange_industry=row.industry,
            master_data_source=provenance,
        )
        ctx.session.add(company)
        ctx.session.flush()  # the securities and aliases below reference this row
        ctx.companies[key] = company
        ctx.report.companies_created += 1
        _add_alias(ctx, company, company.legal_name, AliasType.LEGAL_NAME)
        _add_alias(ctx, company, company.common_name, AliasType.COMMON_NAME)
    else:
        if company.id not in ctx.matched_company_ids:
            ctx.matched_company_ids.add(company.id)
            ctx.report.companies_matched += 1
        # Never overwrite an existing name; remember the exchange's wording as an alias instead.
        if normalize_text(company.legal_name) != normalize_text(row.company_name):
            if len(ctx.report.name_differences) < _MAX_EXAMPLES:
                ctx.report.name_differences.append(
                    f"{row.exchange} {row.symbol}: stored '{company.legal_name}' "
                    f"vs file '{row.company_name}'"
                )
            _add_alias(ctx, company, row.company_name, AliasType.OTHER)
        if company.exchange_industry is None and row.industry:
            company.exchange_industry = row.industry
    if row.security_name:
        _add_alias(ctx, company, row.security_name, AliasType.OTHER)
    return company


def _merge_row(
    ctx: _Context, row: MasterRow, exchange: Exchange, import_id: int, provenance: str
) -> None:
    company = _merge_company(ctx, row, provenance)

    security = ctx.by_symbol.get((exchange.id, row.symbol))
    if security is None and row.security_code:
        security = ctx.by_code.get((exchange.id, row.security_code))  # same code, renamed symbol
    if security is None:
        security = Security(
            company_id=company.id, exchange_id=exchange.id, symbol=row.symbol, isin=row.isin
        )
        ctx.session.add(security)
        ctx.new_securities.append(security)
        ctx.report.securities_created += 1
    else:
        ctx.report.securities_updated += 1
        ctx.seen_existing_security_ids.add(security.id)
        if security.company_id != company.id:
            ctx.report.securities_moved_between_companies += 1
            security.company_id = company.id
        if security.symbol != row.symbol:
            ctx.by_symbol.pop((exchange.id, security.symbol), None)
            security.symbol = row.symbol

    security.isin = row.isin
    security.status = row.status
    security.last_import_id = import_id
    if row.security_code:
        security.security_code = row.security_code
    if row.series:
        security.series = row.series
    if row.face_value is not None:
        security.face_value = row.face_value
    if row.listing_date is not None:
        security.listing_date = row.listing_date
    ctx.by_symbol[(exchange.id, row.symbol)] = security
    if row.security_code:
        ctx.by_code[(exchange.id, row.security_code)] = security
