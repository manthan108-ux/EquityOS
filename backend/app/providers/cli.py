"""Provider router, verification, coverage and manual-data commands. Run from ./backend:

python -m app.providers.cli providers
python -m app.providers.cli verify --provider UPSTOX_FUNDAMENTALS --symbols RELIANCE,TCS
python -m app.providers.cli acquire --symbol RELIANCE [--datasets INCOME_ANNUAL,BALANCE_SHEET]
python -m app.providers.cli coverage --symbol RELIANCE
python -m app.providers.cli compare --symbols RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK
python -m app.providers.cli ingest-document --symbol TCS --type annual_report --file F.pdf --period FY2025
python -m app.providers.cli import-financials --file financials.csv
python -m app.providers.cli fetch-documents --symbol RELIANCE --kind annual_report --limit 3
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.acquisition.factory import build_client
from app.core.database import get_engine
from app.coverage import Status, build_coverage
from app.docintel.entity import company_names
from app.models import Company, DatasetVerification, RawPayload, Security
from app.providers.base import CompanyRef, DataProvider, Dataset, Outcome
from app.providers.company_ir import names_group_entity
from app.providers.documents import DocumentProvider
from app.providers.manual import ManualImportError, import_financials_csv, ingest_document_file
from app.providers.registry import build_providers, provider_statuses, sync_registry
from app.providers.router import PRIORITY, DataRouter
from app.services.company_resolver import ResolutionStatus, resolve_company


def company_ref(session: Session, company: Company) -> CompanyRef:
    securities = list(session.scalars(select(Security).where(Security.company_id == company.id)))
    nse = next((s for s in securities if s.security_code is None), None)
    bse = next((s for s in securities if s.security_code), None)
    isin = nse or bse or (securities[0] if securities else None)
    return CompanyRef(
        id=company.id,
        legal_name=company.legal_name,
        common_name=company.common_name,
        isin=isin.isin if isin else None,
        nse_symbol=nse.symbol if nse else None,
        bse_code=bse.security_code if bse else None,
        website=company.website,
    )


def find_company(session: Session, query: str) -> Company:
    """Resolve a symbol/name to exactly one company or stop with a clear message."""
    resolution = resolve_company(session, query)
    if resolution.status is not ResolutionStatus.RESOLVED:
        raise SystemExit(
            f"'{query}' did not resolve to exactly one company ({resolution.status.value})"
        )
    return session.get(Company, resolution.candidates[0].company_id)


def parse_datasets(text: str | None, provider: DataProvider | None = None) -> list[Dataset]:
    if text:
        return [Dataset(t.strip().upper()) for t in text.split(",")]
    if provider:
        return sorted(provider.spec.datasets, key=lambda d: d.value)
    return list(PRIORITY)


# ---------------------------------------------------------------------------------------------


def cmd_providers(args, providers, router) -> int:
    with Session(get_engine()) as session:
        sync_registry(session, providers)
        session.commit()
        for st in provider_statuses(session, providers):
            print(f"\n{st.spec.code}  (tier {st.spec.authority_tier})  {st.spec.name}")
            print(f"  configured : {st.configured}   auth: {st.spec.auth_requirement}")
            print(f"  access     : {st.spec.access_method}")
            print(f"  rate limits: {st.spec.rate_limits}")
            print(f"  verified   : {[d.value for d in st.verified] or 'none'}")
            print(f"  failing    : {[d.value for d in st.failing] or 'none'}")
            print(f"  UNVERIFIED : {[d.value for d in st.unverified] or 'none'}")
            print(f"  last ok    : {st.last_success_at}   last failure: {st.last_failure or '-'}")
            for note in st.notes:
                print(f"  ! {note}")
    return 0


def cmd_verify(args, providers, router) -> int:
    """Run LIVE retrievals and record the outcome. This is the only way a dataset becomes verified."""
    by_code = {p.spec.code: p for p in providers}
    provider = by_code.get(args.provider)
    if provider is None:
        raise SystemExit(f"Unknown provider {args.provider}; choose from {sorted(by_code)}")
    if not provider.is_configured():
        print(
            f"{args.provider}: UNCONFIGURED, so nothing was verified "
            f"(set {', '.join(provider.spec.env_vars)} in .env)."
        )
        return 2
    datasets = parse_datasets(args.datasets, provider)
    exit_code = 0
    with Session(get_engine()) as session:
        companies = [find_company(session, s.strip()) for s in args.symbols.split(",")]
        for company in companies:
            ref = company_ref(session, company)
            for dataset in datasets:
                result = provider.acquire(session, ref, dataset)
                if result.outcome in (Outcome.UNSUPPORTED, Outcome.UNCONFIGURED):
                    continue
                # Already-stored data legitimately reports 0 NEW rows, so periods also count as evidence.
                ok = result.outcome is Outcome.OK and (result.rows > 0 or bool(result.periods))
                session.add(
                    DatasetVerification(
                        provider_code=args.provider,
                        dataset=dataset.value,
                        company_id=company.id,
                        ok=ok,
                        detail=f"{result.outcome.value}: {result.message}"[:500],
                    )
                )
                session.commit()
                mark = "VERIFIED" if ok else "NOT VERIFIED"
                print(
                    f"{company.common_name:22} {dataset.value:22} {mark:13} {result.message[:70]}"
                )
                exit_code = exit_code or (0 if ok else 1)
    return exit_code


def cmd_acquire(args, providers, router) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        ref = company_ref(session, company)
        datasets = parse_datasets(args.datasets)
        for dataset in datasets:
            if dataset in (
                Dataset.SECURITY_MASTER,
                Dataset.NAME_HISTORY,
                Dataset.GOVERNMENT_STATISTICS,
            ):
                continue  # global datasets, run through their own commands
            route = router.acquire(session, ref, dataset)
            session.commit()
            winner = route.chosen.provider if route.chosen else "-"
            note = "  [FALLBACK USED]" if route.fallback_used else ""
            print(
                f"{dataset.value:24} {'OK' if route.ok else 'NOT OBTAINED':13} via {winner:20}{note}"
            )
            print(f"    {route.explain()}")
    return 0


def cmd_coverage(args, providers, router) -> int:
    with Session(get_engine()) as session:
        report = build_coverage(session, find_company(session, args.symbol).id)
        print(f"\nCoverage for {report.company_name} as of {report.as_of}")
        print(
            f"{'dataset':30} {'status':14} {'complete':>8} {'verification':18} {'confidence':10} sources"
        )
        for d in report.datasets:
            comp = f"{d.completeness:.0%}" if d.completeness is not None else "n/a"
            srcs = ",".join(s.provider for s in d.sources) or "-"
            print(
                f"{d.label:30} {d.status.value:14} {comp:>8} {d.verification.value:18} {d.confidence.value:10} {srcs}"
            )
            if d.missing_periods and d.status is not Status.MISSING:
                print(f"{'':30} missing: {', '.join(d.missing_periods)}")
            for lim in d.limitations:
                print(f"{'':30} - {lim}")
        overall = (
            f"{report.overall_completeness:.0%}"
            if report.overall_completeness is not None
            else "n/a"
        )
        print(f"\nOverall completeness (measurable datasets): {overall}")
        print(f"Not supported (no permitted source): {report.datasets_not_supported}")
        print(f"Research pillars with weakened evidence: {report.affected_pillars}")
    return 0


def cmd_compare(args, providers, router) -> int:
    """Which provider supplied which dataset for each company, and what stayed unavailable."""
    symbols = [s.strip() for s in args.symbols.split(",")]
    with Session(get_engine()) as session:
        reports = {s: build_coverage(session, find_company(session, s).id) for s in symbols}
    labels = [d.label for d in next(iter(reports.values())).datasets]
    width = 24
    print(f"\n{'dataset':32}" + "".join(f"{s:>{width}}" for s in symbols))
    for i, label in enumerate(labels):
        cells = []
        for s in symbols:
            d = reports[s].datasets[i]
            if d.status is Status.NOT_SUPPORTED:
                cell = "n/s (no source)"
            elif d.status is Status.ON_DEMAND:
                cell = "on demand"
            elif d.status is Status.MISSING:
                cell = "UNAVAILABLE"
            else:
                srcs = "+".join(
                    x.provider.replace("_FUNDAMENTALS", "").replace("MARKET_DATA", "MKT")[:9]
                    for x in d.sources
                )
                comp = f"{d.completeness:.0%}" if d.completeness is not None else ""
                cell = f"{srcs} {comp}".strip()
            cells.append(f"{cell:>{width}}")
        print(f"{label:32}" + "".join(cells))
    print(
        "\nLegend: UNAVAILABLE = no data retrieved; n/s = no permitted source exists; "
        "percent = share of required periods present."
    )
    return 0


def cmd_ingest_document(args, providers, router) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        try:
            document, created = ingest_document_file(
                session,
                Path(args.file),
                company_id=company.id,
                doc_type=args.type,
                title=args.title,
                financial_period=args.period,
            )
        except (ValueError, OSError) as exc:
            print(f"Refused: {exc}")
            return 1
        session.commit()
        print(
            f"{'Stored' if created else 'Already stored'}: {document.storage_path} "
            f"({document.size_bytes} bytes, sha256 {document.sha256[:12]})"
        )
    return 0


def cmd_import_financials(args, providers, router) -> int:
    path = Path(args.file)
    with Session(get_engine()) as session:
        try:
            report = import_financials_csv(session, path.read_bytes(), path.name)
        except ManualImportError as exc:
            print("Nothing was imported. Problems found:")
            for problem in exc.problems:
                print(f"  - {problem}")
            return 1
        session.commit()
        print(
            f"Imported {report.rows} facts for {report.companies} company(ies) from {report.filename} "
            f"(sha256 {report.sha256[:12]})."
        )
    return 0


def cmd_fetch_documents(args, providers, router) -> int:
    kind_to_dataset = {
        "annual_report": Dataset.ANNUAL_REPORTS,
        "quarterly_result": Dataset.QUARTERLY_RESULT_DOCS,
        "investor_presentation": Dataset.INVESTOR_PRESENTATIONS,
        "earnings_transcript": Dataset.EARNINGS_TRANSCRIPTS,
    }
    client = build_client()
    try:
        with Session(get_engine()) as session:
            company = find_company(session, args.symbol)
            payload = session.scalar(
                select(RawPayload)
                .where(
                    RawPayload.company_id == company.id,
                    RawPayload.dataset == kind_to_dataset[args.kind].value,
                    RawPayload.provider_code == "COMPANY_IR",
                )
                .order_by(RawPayload.id.desc())
                .limit(1)
            )
            if payload is None:
                print("No discovered links yet. Run: acquire --symbol ... first (IR discovery).")
                return 1
            links = [link for link in payload.payload if link.get("is_document")][: args.limit]
            if not links:
                print(
                    "Links were found but none points straight at a file; open the pages and use ingest-document."
                )
                return 1
            provider = DocumentProvider(client)
            names = company_names(session, company.id)
            for link in links:
                other = names_group_entity(link.get("text") or "", names)
                if other:
                    print(f"skipped {link['url']}: labelled for another entity ({other})")
                    continue
                stored = provider.download(
                    session,
                    link["url"],
                    doc_type=args.kind,
                    company_id=company.id,
                    title=link.get("text"),
                    financial_period=link.get("period_hint"),
                )
                session.commit()
                if stored.document:
                    print(
                        f"{'stored ' if stored.created else 'have   '} {link['url']}  period={link.get('period_hint') or '?'} "
                        "(period inferred from the link; confirm it)"
                    )
                else:
                    print(f"failed  {link['url']}: {stored.error}")
    finally:
        client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m app.providers.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("providers")
    p = sub.add_parser("verify")
    p.add_argument("--provider", required=True)
    p.add_argument("--symbols", required=True)
    p.add_argument("--datasets")
    p = sub.add_parser("acquire")
    p.add_argument("--symbol", required=True)
    p.add_argument("--datasets")
    p = sub.add_parser("coverage")
    p.add_argument("--symbol", required=True)
    p = sub.add_parser("compare")
    p.add_argument("--symbols", required=True)
    p = sub.add_parser("ingest-document")
    p.add_argument("--symbol", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--period")
    p.add_argument("--title")
    p = sub.add_parser("import-financials")
    p.add_argument("--file", required=True)
    p = sub.add_parser("fetch-documents")
    p.add_argument("--symbol", required=True)
    p.add_argument(
        "--kind",
        required=True,
        choices=[
            "annual_report",
            "quarterly_result",
            "investor_presentation",
            "earnings_transcript",
        ],
    )
    p.add_argument("--limit", type=int, default=3)
    args = parser.parse_args(argv)

    client = build_client()
    providers = build_providers(client)
    router = DataRouter(providers)
    handlers = {
        "providers": cmd_providers,
        "verify": cmd_verify,
        "acquire": cmd_acquire,
        "coverage": cmd_coverage,
        "compare": cmd_compare,
        "ingest-document": cmd_ingest_document,
        "import-financials": cmd_import_financials,
        "fetch-documents": cmd_fetch_documents,
    }
    try:
        return handlers[args.command](args, providers, router)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
