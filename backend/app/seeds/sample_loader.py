"""Plan, apply and monitor the 15-company research sample. Run from ./backend:

python -m app.seeds.sample_loader plan            # what would change (changes nothing)
python -m app.seeds.sample_loader apply --yes     # fill EMPTY sector / industry / website fields
python -m app.seeds.sample_loader status          # per company: data held and what is still needed

`apply` follows the project rule for existing records: it only fills fields that are empty, never
overwrites a value that is already set (a differing value is reported as a conflict and left
alone), adds missing reference industries, and writes an audit row to `master_data_imports`.
"""

import argparse
import hashlib
import inspect
import sys
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.core.storage import data_root
from app.docintel.inbox import inbox_dir
from app.models import (
    CalculationRun,
    Company,
    Document,
    EvidenceRecord,
    EvidenceStatus,
    Exchange,
    FinancialFact,
    Industry,
    MasterDataImport,
    Sector,
    Security,
)
from app.seeds import sample_universe as universe
from app.seeds.sample_universe import SAMPLE, SampleCompany


@dataclass
class PlanRow:
    spec: SampleCompany
    company: Company | None
    set_fields: dict[str, str] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


def find_company(session: Session, symbol: str) -> Company | None:
    nse = session.scalar(select(Exchange.id).where(Exchange.code == "NSE"))
    sec = session.scalar(
        select(Security).where(Security.exchange_id == nse, Security.symbol == symbol)
    )
    return session.get(Company, sec.company_id) if sec else None


def plan(session: Session) -> list[PlanRow]:
    sectors = {s.code: s for s in session.scalars(select(Sector))}
    industries = {
        (sectors_by_id.code, i.code): i
        for i in session.scalars(select(Industry))
        for sectors_by_id in [session.get(Sector, i.sector_id)]
    }
    rows = []
    for spec in SAMPLE:
        company = find_company(session, spec.symbol)
        row = PlanRow(spec, company)
        rows.append(row)
        if company is None:
            continue
        wanted_sector = sectors.get(spec.sector)
        wanted_industry = industries.get((spec.sector, spec.industry))
        if company.sector_id is None:
            row.set_fields["sector"] = spec.sector
        elif wanted_sector is not None and company.sector_id != wanted_sector.id:
            row.conflicts.append(f"sector is already set to id {company.sector_id}")
        if company.industry_id is None:
            row.set_fields["industry"] = spec.industry
        elif wanted_industry is not None and company.industry_id != wanted_industry.id:
            row.conflicts.append(f"industry is already set to id {company.industry_id}")
        if not company.website:
            row.set_fields["website"] = spec.website
        elif company.website.rstrip("/") != spec.website.rstrip("/"):
            row.conflicts.append(f"website is already {company.website!r}")
    return rows


def apply(session: Session) -> tuple[int, list[str]]:
    """Fill empty fields and add missing industries. Returns (companies changed, change log)."""
    log: list[str] = []
    sectors = {s.code: s for s in session.scalars(select(Sector))}
    for (sector_code, code), name in universe.NEW_INDUSTRIES.items():
        sector = sectors[sector_code]
        exists = session.scalar(
            select(Industry).where(Industry.sector_id == sector.id, Industry.code == code)
        )
        if exists is None:
            session.add(Industry(sector_id=sector.id, code=code, name=name))
            log.append(f"new industry {sector_code}/{code}")
    session.flush()
    industries = {
        (session.get(Sector, i.sector_id).code, i.code): i
        for i in session.scalars(select(Industry))
    }
    changed = 0
    for row in plan(session):
        if row.company is None or not row.set_fields:
            continue
        c, spec = row.company, row.spec
        if "sector" in row.set_fields:
            c.sector_id = sectors[spec.sector].id
        if "industry" in row.set_fields:
            c.industry_id = industries[(spec.sector, spec.industry)].id
        if "website" in row.set_fields:
            c.website = spec.website
        changed += 1
        log.append(f"{spec.symbol}: set {', '.join(sorted(row.set_fields))}")
    for symbol, (sector_code, old, new) in universe.RECLASSIFIED.items():
        company = find_company(session, symbol)
        if company is None:
            continue
        old_row = industries.get((sector_code, old))
        new_row = industries.get((sector_code, new))
        if new_row is None or company.industry_id == new_row.id:
            continue
        if old_row is not None and company.industry_id == old_row.id:
            company.industry_id = new_row.id
            changed += 1
            log.append(f"{symbol}: industry {old} -> {new} (documented reclassification)")
        else:
            log.append(f"{symbol}: industry left as is (not {old}); needs a manual decision")
    session.flush()
    source = inspect.getsource(universe).encode()
    session.add(
        MasterDataImport(
            exchange_code="NSE",
            filename="app/seeds/sample_universe.py",
            sha256=hashlib.sha256(source).hexdigest(),
            rows_total=len(SAMPLE),
            rows_imported=changed,
            rows_skipped=len(SAMPLE) - changed,
            notes="research sample: empty sector/industry/website fields filled; "
            + "; ".join(log)[:3000],
        )
    )
    session.flush()
    return changed, log


def _count(session: Session, model, *where) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def cmd_plan(_args) -> int:
    with Session(get_engine()) as session:
        for r in plan(session):
            if r.company is None:
                print(f"  {r.spec.symbol:11} NOT FOUND in the master data")
            elif not r.set_fields and not r.conflicts:
                print(f"  {r.spec.symbol:11} {r.company.common_name[:28]:28} already complete")
            else:
                print(
                    f"  {r.spec.symbol:11} {r.company.common_name[:28]:28} would set "
                    f"{sorted(r.set_fields) or '-'}"
                    + (f"   CONFLICT: {'; '.join(r.conflicts)}" if r.conflicts else "")
                )
        print("nothing was changed (use `apply --yes` after reviewing)")
    return 0


def cmd_apply(args) -> int:
    if not args.yes:
        raise SystemExit("Refusing to change records without --yes (review `plan` first).")
    with Session(get_engine()) as session:
        changed, log = apply(session)
        session.commit()
    print(f"{changed} company record(s) had empty fields filled")
    for line in log:
        print("  " + line)
    return 0


def cmd_status(_args) -> int:
    root = data_root()
    with Session(get_engine()) as session:
        print(
            f"{'symbol':11} {'cohort':12} {'documents':10} {'docs':>4} {'facts':>6} "
            f"{'evidence':>8} calc  next step"
        )
        for spec in SAMPLE:
            company = find_company(session, spec.symbol)
            if company is None:
                print(f"{spec.symbol:11} NOT FOUND")
                continue
            docs = _count(session, Document, Document.company_id == company.id)
            facts = _count(session, FinancialFact, FinancialFact.company_id == company.id)
            evidence = _count(
                session,
                EvidenceRecord,
                EvidenceRecord.company_id == company.id,
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
            )
            calc = _count(session, CalculationRun, CalculationRun.company_id == company.id)
            folder = inbox_dir(root, spec.symbol)
            waiting = len(list(folder.glob("*.pdf"))) if folder.exists() else 0
            if facts:
                step = "ready"
            elif waiting:
                step = (
                    f"{waiting} PDF(s) waiting in {folder.relative_to(root.parent)}: "
                    "ingest-inbox, extract"
                )
            elif docs:
                step = (
                    f"{docs} document(s) stored but no value passed the checks yet: see "
                    "`python -m app.docintel.cli show --document-id ...` for the reasons"
                )
            elif spec.documents == "AUTOMATIC":
                step = "download via company IR (python -m app.providers.cli), then extract"
            else:
                step = f"download filings manually into {folder.relative_to(root.parent)}"
            print(
                f"{spec.symbol:11} {spec.cohort:12} {spec.documents:10} {docs:>4} {facts:>6} "
                f"{evidence:>8} {calc:>4}  {step}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seeds.sample_loader")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan").set_defaults(func=cmd_plan)
    p = sub.add_parser("apply")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_apply)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
