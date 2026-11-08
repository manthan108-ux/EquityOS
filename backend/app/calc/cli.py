"""Calculation commands. Run from ./backend with the database up:

python -m app.calc.cli run --symbol ICICIBANK            # validate, adjust, calculate, record gaps
python -m app.calc.cli run-all                           # every company with stored data
python -m app.calc.cli metrics --symbol ICICIBANK [--prefix RETURN] [--basis CONSOLIDATED]
python -m app.calc.cli gaps --symbol RELIANCE [--metric RATIO.]   # what could NOT be calculated
python -m app.calc.cli technicals --symbol ICICIBANK
python -m app.calc.cli adjustments --symbol ICICIBANK    # splits/bonuses applied, unexplained gaps
"""

import argparse
import sys
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.calc.engine import calculate_company
from app.core.database import get_engine
from app.models import (
    CalculationGap,
    CalculationRun,
    Company,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
    FinancialFact,
    PriceAdjustment,
    Security,
)
from app.providers.cli import find_company


def _fmt(record: EvidenceRecord) -> str:
    value = record.value
    if record.unit == "inr":
        return f"{value / 10**7:,.2f} crore"
    if record.unit in ("percent", "x", "index", "ratio"):
        return f"{value:,.2f} {'%' if record.unit == 'percent' else record.unit}"
    return f"{value:,.2f} {record.unit}"


def cmd_run(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        report = calculate_company(session, company.id)
        session.commit()
        _print_run(company, report)
    return 0


def _print_run(company, report) -> None:
    run = report.run
    print(f"{company.common_name}: calculation run {run.id} ({run.profile} statements)")
    print(f"  calculated: {run.calculated}   gaps (could not calculate): {run.gaps}")
    for category, n in sorted((run.summary or {}).get("by_category", {}).items()):
        print(f"    {category:12} {n}")
    if report.market.security:
        print(
            f"  prices: security {report.market.security.symbol} up to {report.market.price_date}"
        )
    print(
        f"  price adjustments applied: {run.summary['adjustments_applied']}, "
        f"unexplained reference-price gaps (not applied): {run.summary['unexplained_price_gaps']}"
    )
    v = report.validation.run
    print(f"  validation after calculation: {v.errors} error(s), {v.warnings} warning(s)")
    for f in report.validation.findings:
        if f.severity.value == "ERROR":
            print(f"    ERROR {f.rule}: {f.message[:160]}")


def cmd_run_all(args) -> int:
    with Session(get_engine()) as session:
        ids = set(session.scalars(select(FinancialFact.company_id).distinct()))
        companies = session.scalars(
            select(Company).where(Company.id.in_(ids)).order_by(Company.common_name)
        ).all()
        for company in companies:
            report = calculate_company(session, company.id)
            session.commit()
            print(
                f"  {company.common_name:28} calculated={report.run.calculated:<5} "
                f"gaps={report.run.gaps:<5} errors={report.validation.run.errors}"
            )
    return 0


def cmd_metrics(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        query = select(EvidenceRecord).where(
            EvidenceRecord.company_id == company.id,
            EvidenceRecord.kind == "CALCULATION",
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
        if args.prefix:
            query = query.where(EvidenceRecord.metric.like(f"{args.prefix}%"))
        if args.basis:
            query = query.where(EvidenceRecord.basis == args.basis.upper())
        quality = {
            r.selected_evidence_id: r.quality.value
            for r in session.scalars(
                select(EvidenceResolution).where(EvidenceResolution.company_id == company.id)
            )
        }
        for r in session.scalars(
            query.order_by(
                EvidenceRecord.metric, EvidenceRecord.basis, EvidenceRecord.period_end.desc()
            )
        ):
            print(
                f"{r.metric:34} {r.period_end} {r.period_type or '-':9} {r.basis or '-':12} "
                f"{_fmt(r):>22} {quality.get(r.id, '-'):6} [{r.id}]"
            )
    return 0


def cmd_gaps(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        run = session.scalar(
            select(CalculationRun)
            .where(CalculationRun.company_id == company.id)
            .order_by(CalculationRun.id.desc())
            .limit(1)
        )
        if run is None:
            raise SystemExit("No calculation run yet (use `run`).")
        query = select(CalculationGap).where(CalculationGap.run_id == run.id)
        if args.metric:
            query = query.where(CalculationGap.metric.like(f"{args.metric}%"))
        rows = session.scalars(
            query.order_by(CalculationGap.metric, CalculationGap.period_end)
        ).all()
        print(f"run {run.id}: {len(rows)} gap(s)")
        counts = Counter(g.metric for g in rows)
        for metric, n in counts.most_common():
            sample = next(g for g in rows if g.metric == metric)
            print(f"  {n:4} x {metric:34} e.g. {sample.reason[:120]}")
    return 0


def cmd_technicals(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        for r in session.scalars(
            select(EvidenceRecord)
            .where(
                EvidenceRecord.company_id == company.id,
                EvidenceRecord.kind == "TECHNICAL",
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
            )
            .order_by(EvidenceRecord.metric)
        ):
            print(f"{r.metric:28} {r.period_end} {_fmt(r):>22}  {r.dimension}")
    return 0


def cmd_adjustments(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        secs = {
            s.id: s
            for s in session.scalars(select(Security).where(Security.company_id == company.id))
        }
        rows = session.scalars(
            select(PriceAdjustment)
            .where(PriceAdjustment.security_id.in_(list(secs)))
            .order_by(PriceAdjustment.ex_date)
        ).all()
        print(f"{len(rows)} event(s)")
        for a in rows:
            print(
                f"  {secs[a.security_id].symbol} (sec {a.security_id}) {a.ex_date} "
                f"factor {a.factor:.4f} "
                f"{a.kind.value} {a.origin.value} {a.status.value} {a.confidence}: {a.detail[:110]}"
            )
        n = session.scalar(select(func.count()).select_from(PriceAdjustment))
        print(f"(all companies: {n} event rows)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.calc.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--symbol", required=True)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("run-all")
    p.set_defaults(func=cmd_run_all)
    p = sub.add_parser("metrics")
    p.add_argument("--symbol", required=True)
    p.add_argument("--prefix")
    p.add_argument("--basis")
    p.set_defaults(func=cmd_metrics)
    p = sub.add_parser("gaps")
    p.add_argument("--symbol", required=True)
    p.add_argument("--metric")
    p.set_defaults(func=cmd_gaps)
    p = sub.add_parser("technicals")
    p.add_argument("--symbol", required=True)
    p.set_defaults(func=cmd_technicals)
    p = sub.add_parser("adjustments")
    p.add_argument("--symbol", required=True)
    p.set_defaults(func=cmd_adjustments)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
