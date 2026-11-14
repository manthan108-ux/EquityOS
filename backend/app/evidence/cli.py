"""Evidence commands. Run from ./backend with the database up:

python -m app.evidence.cli validate --symbol ICICIBANK       # sync + resolve + validate + record
python -m app.evidence.cli validate-all                       # every company that has stored data
python -m app.evidence.cli summary  --symbol ICICIBANK       # what the evidence store holds
python -m app.evidence.cli findings --symbol ICICIBANK [--severity ERROR] [--rule CODE]
python -m app.evidence.cli resolved --symbol ICICIBANK --metric INCOME.net_profit
python -m app.evidence.cli show --id 123                     # one record, its source and lineage
python -m app.evidence.cli check-claim "Net profit rose 12%" --company ICICIBANK --cite 1,2
"""

import argparse
import sys
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.evidence.claims import CitedValue, check_figures
from app.evidence.engine import validate_company
from app.evidence.lineage import dependents, lineage_tree
from app.models import (
    Company,
    Document,
    EvidenceRecord,
    EvidenceResolution,
    FinancialFact,
    ShareholdingFact,
    ValidationFinding,
    ValidationRun,
)
from app.providers.cli import find_company


def cmd_validate(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        report = validate_company(session, company.id)
        session.commit()
        s = report.sync
        print(
            f"{company.common_name}: validation run {report.run.id} "
            f"(rules {report.run.rules_version})"
        )
        print(
            f"  evidence synced: {s.created} new, {s.unchanged} unchanged, {s.superseded} "
            f"superseded, {s.withdrawn} withdrawn, {s.reactivated} reactivated, {s.linked} link(s)"
        )
        print(f"  values checked: {report.run.evidence_checked}")
        summary = report.run.summary or {}
        print(
            "  resolution: "
            + ", ".join(f"{k}={v}" for k, v in sorted(summary["resolution"].items()))
        )
        print(
            "  quality   : " + ", ".join(f"{k}={v}" for k, v in sorted(summary["quality"].items()))
        )
        checks = summary.get("checks", {})
        for rule in sorted({k.rsplit(".", 1)[0] for k in checks}):
            done, ok = checks.get(f"{rule}.checked", 0), checks.get(f"{rule}.passed", 0)
            print(f"  check {rule:26} {ok}/{done} passed")
        print(
            f"  findings  : {report.run.errors} error(s), {report.run.warnings} warning(s), "
            f"{report.run.infos} info"
        )
        print(f"  blocked values (must not be used): {report.blocked}")
        shown = 0
        for finding in sorted(report.findings, key=lambda f: (f.severity.value, f.rule)):
            if finding.severity.value == "INFO" and not args.verbose:
                continue
            print(f"    {finding.severity.value:7} {finding.rule}: {finding.message[:150]}")
            shown += 1
            if shown >= args.limit:
                print(f"    ... (use `findings` to see all {len(report.findings)})")
                break
    return 0


def cmd_validate_all(args) -> int:
    """Validate every company that has stored facts, documents or shareholding data."""
    with Session(get_engine()) as session:
        ids = set(session.scalars(select(FinancialFact.company_id).distinct()))
        ids |= set(
            session.scalars(
                select(Document.company_id).where(Document.company_id.is_not(None)).distinct()
            )
        )
        ids |= set(session.scalars(select(ShareholdingFact.company_id).distinct()))
        companies = session.scalars(
            select(Company).where(Company.id.in_(ids)).order_by(Company.common_name)
        ).all()
        print(f"{len(companies)} company(ies) with stored data")
        for company in companies:
            report = validate_company(session, company.id)
            session.commit()
            print(
                f"  {company.common_name:32} values={report.run.evidence_checked:<6} "
                f"errors={report.run.errors:<3} warnings={report.run.warnings:<3} "
                f"blocked={report.blocked}"
            )
    return 0


def cmd_integrity(args) -> int:
    """Read-only audit: does the evidence store agree with what it was built from?"""
    import json

    from app.core.config import get_settings, resolve_data_dir
    from app.evidence import integrity

    with Session(get_engine()) as session:
        company = find_company(session, args.symbol) if args.symbol else None
        report = integrity.audit(
            session,
            company_id=company.id if company else None,
            data_dir=resolve_data_dir(get_settings()),
        )
        integrity.names(session, report)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(f"{report.scope}: {report.status}  ({report.evidence_rows} evidence rows)")
        for c in report.checks:
            flag = "ok " if c.clean else c.severity[:4]
            extra = "" if c.clean else f"  {c.count} in {c.companies} companies"
            print(f"  [{flag}] {c.code:20} {c.title}{extra}")
            for e in c.examples[: args.examples]:
                print("         ", {k: v for k, v in e.items() if k != "company_id"})
    return 1 if report.errors else 0


def cmd_summary(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        print(f"{company.common_name}")
        for label, column in (
            ("class", EvidenceRecord.evidence_class),
            ("status", EvidenceRecord.status),
            ("kind", EvidenceRecord.kind),
        ):
            rows = session.execute(
                select(column, func.count())
                .where(EvidenceRecord.company_id == company.id)
                .group_by(column)
            ).all()
            print(
                f"  by {label}: "
                + ", ".join(
                    f"{getattr(k, 'value', k)}={n}"
                    for k, n in sorted(rows, key=lambda r: str(r[0]))
                )
            )
        rows = session.execute(
            select(EvidenceResolution.status, EvidenceResolution.quality, func.count())
            .where(EvidenceResolution.company_id == company.id)
            .group_by(EvidenceResolution.status, EvidenceResolution.quality)
        ).all()
        for status, quality, n in sorted(rows, key=lambda r: (r[0].value, r[1].value)):
            print(f"  resolved {status.value:22} quality {quality.value:6} {n}")
        blocked = session.scalar(
            select(func.count()).where(
                EvidenceResolution.company_id == company.id, EvidenceResolution.blocked.is_(True)
            )
        )
        print(f"  blocked: {blocked}")
        run = session.scalar(
            select(ValidationRun)
            .where(ValidationRun.company_id == company.id)
            .order_by(ValidationRun.id.desc())
            .limit(1)
        )
        if run:
            print(
                f"  last validation: run {run.id} at {run.started_at:%Y-%m-%d %H:%M} - "
                f"{run.errors} error(s), {run.warnings} warning(s)"
            )
        else:
            print("  validation has not been run (use `validate`)")
    return 0


def cmd_findings(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        run = session.scalar(
            select(ValidationRun)
            .where(ValidationRun.company_id == company.id)
            .order_by(ValidationRun.id.desc())
            .limit(1)
        )
        if run is None:
            raise SystemExit("No validation run yet (use `validate`).")
        query = select(ValidationFinding).where(ValidationFinding.run_id == run.id)
        if args.severity:
            query = query.where(ValidationFinding.severity == args.severity.upper())
        if args.rule:
            query = query.where(ValidationFinding.rule_code == args.rule)
        rows = session.scalars(
            query.order_by(
                ValidationFinding.severity, ValidationFinding.rule_code, ValidationFinding.id
            )
        ).all()
        print(f"run {run.id}: {len(rows)} finding(s)")
        counts = Counter(f"{r.severity.value} {r.rule_code}" for r in rows)
        for key, n in counts.most_common():
            print(f"  {n:5} x {key}")
        if args.details:
            for r in rows[: args.limit]:
                print(
                    f"- {r.severity.value} {r.rule_code} "
                    f"[{', '.join(map(str, r.evidence_ids or []))}] {r.message}"
                )
    return 0


def cmd_resolved(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        query = select(EvidenceResolution).where(EvidenceResolution.company_id == company.id)
        if args.metric:
            query = query.where(EvidenceResolution.metric == args.metric)
        rows = session.scalars(
            query.order_by(EvidenceResolution.metric, EvidenceResolution.period_end.desc()).limit(
                args.limit
            )
        ).all()
        for r in rows:
            record = (
                session.get(EvidenceRecord, r.selected_evidence_id)
                if r.selected_evidence_id
                else None
            )
            value = f"{record.value:,.2f} {record.unit}" if record else "(none)"
            print(
                f"{r.metric:48} {r.period_end} {r.period_type or '-':9} {r.basis or '-':13} "
                f"{value:>28} {r.status.value:14} {r.quality.value:6}"
                f"{' BLOCKED' if r.blocked else ''}"
            )
    return 0


def _print_tree(node, depth: int = 0) -> None:
    pad = "  " * depth
    label = (
        f"{pad}[{node.evidence_id}] {node.evidence_class} {node.metric} "
        f"{node.period_end or ''} = {node.value} {node.unit or ''} ({node.status})"
    )
    where = f" doc {node.document_id} p.{node.page}" if node.document_id else ""
    alias = f" <{node.alias}>" if node.alias else ""
    print(label + alias + where)
    for child in node.inputs:
        _print_tree(child, depth + 1)


def cmd_show(args) -> int:
    with Session(get_engine()) as session:
        record = session.get(EvidenceRecord, args.id)
        if record is None:
            raise SystemExit(f"No evidence {args.id}")
        print(
            f"evidence {record.id}: {record.evidence_class.value} {record.metric} "
            f"{record.dimension}"
        )
        print(f"  period {record.period_end} {record.period_type or ''} {record.basis or ''}")
        print(
            f"  value  {record.value} {record.unit}   "
            f"(published: {record.original_value} {record.original_unit})"
        )
        print(f"  source {record.provider_code} tier {record.authority_tier}  {record.source_url}")
        if record.document_id:
            print(f"  document {record.document_id} page {record.source_page}")
        if record.expression:
            print(f"  formula {record.expression}")
        if record.statement_text:
            print(f"  text    {record.statement_text}")
        print(f"  status {record.status.value} {record.status_reason or ''}")
        print("  lineage:")
        _print_tree(lineage_tree(session, record.id), 2)
        print(f"  used by evidence: {dependents(session, record.id) or 'nothing'}")
    return 0


def cmd_check_claim(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.company)
        cited = []
        for raw in args.cite.split(","):
            record = session.get(EvidenceRecord, int(raw))
            if record is None or record.company_id != company.id or record.value is None:
                raise SystemExit(f"evidence {raw} is not a numeric record of {company.common_name}")
            cited.append(CitedValue(record.id, record.value, record.unit))
        result = check_figures(args.text, cited)
        for figure, evidence_id in result.matched:
            print(f"  traced   {figure.text!r} -> evidence {evidence_id}")
        for figure in result.untraced:
            print(f"  UNTRACED {figure.text!r}")
        print(
            "OK"
            if result.ok
            else "REFUSED: the statement uses numbers its evidence does not contain"
        )
        return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evidence.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="sync, resolve and validate one company's evidence")
    p.add_argument("--symbol", required=True)
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--verbose", action="store_true", help="also print INFO findings")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("validate-all", help="validate every company that has stored data")
    p.set_defaults(func=cmd_validate_all)

    p = sub.add_parser("integrity", help="read-only audit of the evidence store")
    p.add_argument("--symbol")
    p.add_argument("--json", action="store_true")
    p.add_argument("--examples", type=int, default=3)
    p.set_defaults(func=cmd_integrity)

    p = sub.add_parser("summary")
    p.add_argument("--symbol", required=True)
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("findings")
    p.add_argument("--symbol", required=True)
    p.add_argument("--severity")
    p.add_argument("--rule")
    p.add_argument("--details", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser("resolved")
    p.add_argument("--symbol", required=True)
    p.add_argument("--metric")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(func=cmd_resolved)

    p = sub.add_parser("show")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("check-claim")
    p.add_argument("text")
    p.add_argument("--company", required=True)
    p.add_argument("--cite", required=True, help="comma-separated evidence ids")
    p.set_defaults(func=cmd_check_claim)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
