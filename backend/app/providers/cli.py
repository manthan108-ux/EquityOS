"""Provider router, verification, coverage and manual-data commands. Run from ./backend:

python -m app.providers.cli providers
python -m app.providers.cli verify --provider UPSTOX_FUNDAMENTALS --symbols RELIANCE,TCS
python -m app.providers.cli acquire --symbol RELIANCE [--datasets INCOME_ANNUAL,BALANCE_SHEET]
python -m app.providers.cli coverage --symbol RELIANCE
python -m app.providers.cli compare --symbols RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK
python -m app.providers.cli ingest-document --symbol TCS --type annual_report --file F.pdf --period FY2025
python -m app.providers.cli import-financials --file financials.csv
python -m app.providers.cli fetch-documents --symbol RELIANCE --kind annual_report --limit 3
python -m app.providers.cli backfill-prices --from 2023-11-01 --to 2025-11-06
"""

import argparse
import sys
from datetime import UTC, datetime
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
from app.providers.documents import AssociationMismatch, DocumentProvider
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
                force_association=args.force_association,
            )
        except (ValueError, OSError) as exc:
            print(f"Refused: {exc}")
            if isinstance(exc, AssociationMismatch):
                print(
                    "If you are sure it belongs to this company, repeat with --force-association."
                )
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


def cmd_backfill_prices(args, providers, router) -> int:
    """Official NSE end-of-day files for past days that are not stored yet.

    Resumable and polite: days that already have NSE prices are skipped without a request, every
    other day is one request through the polite client (robots.txt, pacing, backoff), and each day
    is committed on its own, so an interruption loses at most one day. Weekends and exchange
    holidays have no file (reported as such, not as failures).
    """
    from datetime import date, timedelta

    from sqlalchemy import func

    from app.models import EodPrice, Exchange
    from app.providers.exchange_files import BhavcopyProvider, store_prices

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start > end:
        raise SystemExit("--from must not be after --to")
    provider = BhavcopyProvider(providers_client(providers), "NSE")
    done = missing = stored_rows = 0
    with Session(get_engine()) as session:
        nse = session.scalar(select(Exchange.id).where(Exchange.code == "NSE"))
        have = set(
            session.scalars(
                select(EodPrice.trade_date)
                .join(Security, Security.id == EodPrice.security_id)
                .where(Security.exchange_id == nse, EodPrice.trade_date.between(start, end))
                .group_by(EodPrice.trade_date)
                .having(func.count() > 500)  # a real market day, not a stray row
            )
        )
        day = start
        while day <= end:
            if day.weekday() < 5 and day not in have:
                fetch = provider.fetch(day)
                if fetch.ok:
                    report = store_prices(session, "NSE", provider.parse(fetch), fetch)
                    session.commit()
                    done += 1
                    stored_rows += report.stored
                    print(f"{day}: {report.stored} rows stored", flush=True)
                else:
                    missing += 1
                    print(f"{day}: no file ({fetch.status.value})", flush=True)
            day += timedelta(days=1)
    print(f"{done} day(s) stored ({stored_rows} rows), {missing} without a file")
    return 0


def providers_client(providers):
    """The shared polite client behind the providers (they were all built from one)."""
    for p in providers:
        client = getattr(p, "_client", None)
        if client is not None:
            return client
    raise SystemExit("no HTTP client available")


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
                if stored.document and stored.document.company_id is None and stored.created:
                    print(
                        f"stored but NOT linked to {company.common_name}: "
                        f"{stored.document.association_note}  {link['url']}"
                    )
                elif stored.document:
                    print(
                        f"{'stored ' if stored.created else 'have   '} {link['url']}  period={link.get('period_hint') or '?'} "
                        "(period inferred from the link; confirm it)"
                    )
                else:
                    print(f"failed  {link['url']}: {stored.error}")
    finally:
        client.close()
    return 0


def cmd_discover_websites(args, providers, router) -> int:
    """Find, verify and register official websites for companies that have none (batched, resumable)."""
    from sqlalchemy import select

    from app.acquisition import website_discovery as wd
    from app.llm.providers import default_router
    from app.models import Company, CompanyWebsiteRegistry, Security

    client = build_client()
    llm = None if args.no_llm else default_router()
    counts: dict[str, int] = {}
    try:
        with Session(get_engine()) as session:
            if args.symbols:
                companies = [find_company(session, sym.strip()) for sym in args.symbols.split(",")]
            else:
                # Listed on NSE first (the larger issuers), companies with no website and no pending retry.
                q = (
                    select(Company)
                    .join(Security, Security.company_id == Company.id)
                    .outerjoin(
                        CompanyWebsiteRegistry, CompanyWebsiteRegistry.company_id == Company.id
                    )
                    .where(Company.website.is_(None))
                    .where(
                        (CompanyWebsiteRegistry.company_id.is_(None))
                        | (CompanyWebsiteRegistry.next_retry_at.is_not(None))
                        & (CompanyWebsiteRegistry.next_retry_at <= datetime.now(UTC))
                    )
                    .group_by(Company.id)
                    .order_by(Company.common_name)
                    .limit(args.limit)
                )
                companies = list(session.scalars(q))
            if args.backfill:
                for c in session.scalars(select(Company).where(Company.website.is_not(None))):
                    wd.record_existing(session, c)
            for company in companies:
                res = wd.discover(
                    session, company, client, llm, use_llm=not args.no_llm, force=args.force
                )
                counts[res.status] = counts.get(res.status, 0) + 1
                print(
                    f"{res.status:13} {company.common_name[:34]:34} {res.website or ''} {res.investor_relations_url or ''} {res.note}"
                )
            print("this run:", counts or "nothing to do")
            print("registry:", wd.registry_summary(session))
    finally:
        client.close()
    return 0


def cmd_website_status(args, providers, router) -> int:
    from sqlalchemy import select

    from app.acquisition import website_discovery as wd
    from app.models import Company, CompanyWebsiteRegistry

    with Session(get_engine()) as session:
        print(wd.registry_summary(session))
        for status in ("AMBIGUOUS", "FAILED"):
            rows = session.execute(
                select(
                    Company.common_name,
                    CompanyWebsiteRegistry.website,
                    CompanyWebsiteRegistry.attempts,
                )
                .join(CompanyWebsiteRegistry, CompanyWebsiteRegistry.company_id == Company.id)
                .where(CompanyWebsiteRegistry.status == status)
                .limit(args.limit)
            ).all()
            if rows:
                print(f"{status} (flagged, not registered):")
                for name, site, attempts in rows:
                    print(f"  {name[:40]:40} {site or '-':40} attempts={attempts}")
    return 0


def cmd_upstox_refresh(args, providers, router) -> int:
    """Refresh Upstox fundamentals for the next batch of due companies (resumable, incremental)."""
    from app.providers import upstox_refresh as ur

    provider = {p.spec.code: p for p in providers}.get("UPSTOX_FUNDAMENTALS")
    if provider is None or not provider.is_configured():
        print("UPSTOX_FUNDAMENTALS is not configured (UPSTOX_ANALYTICS_TOKEN missing or disabled).")
        return 2
    with Session(get_engine()) as session:
        if args.summary:
            for key, value in ur.summary(session).items():
                print(f"{key:28} {value}")
            return 0
        if args.symbols:
            companies = [find_company(session, x.strip()) for x in args.symbols.split(",")]
        else:
            companies = ur.due_companies(session, args.limit)
        print(f"refreshing {len(companies)} companies")
        run = ur.run_batch(
            session,
            provider,
            companies,
            on_outcome=lambda o: print(
                f"{o.status:15} {o.name[:34]:34} rows={o.new_rows:<4} {o.note[:90]}"
            ),
        )
        if run.stopped:
            print("STOPPED:", run.stopped)
        if args.validate:
            from app.evidence.engine import validate_company

            for company in companies:
                validate_company(session, company.id)
            print("evidence synced and validated for the batch")
        for key, value in ur.summary(session).items():
            print(f"{key:28} {value}")
    return 0 if not run.stopped else 3


def cmd_enrich(args, providers, router) -> int:
    """Read a company's own about/leadership pages with a language model; keep verified quotes."""
    from app.llm.providers import default_router
    from app.research import enrich

    llm = default_router()
    client = build_client()
    try:
        with Session(get_engine()) as session:
            for sym in args.symbols.split(","):
                company = find_company(session, sym.strip())
                rep = enrich.enrich_company(session, company, client, llm)
                print(
                    f"{company.common_name:30} pages={rep.pages_read} stored={rep.stored} "
                    f"discarded_unverifiable={rep.discarded} {'; '.join(rep.notes)[:160]}"
                )
    finally:
        client.close()
    return 0


def cmd_universe(args, providers, router) -> int:
    """One batched, resumable pass over the universe (websites, Upstox, documents, enrichment)."""
    import json

    from app.llm.providers import default_router
    from app.providers import universe

    with Session(get_engine()) as session:
        if args.status:
            print(json.dumps(universe.coverage(session), indent=2, default=str))
            return 0
        steps = tuple(x.strip() for x in args.steps.split(",") if x.strip())
        unknown = set(steps) - set(universe.STEPS)
        if unknown:
            raise SystemExit(f"unknown step(s) {sorted(unknown)}; choose from {universe.STEPS}")
        llm = None if args.no_llm else default_router()
        if llm is not None and not llm.providers:
            llm = None
        upstox = {p.spec.code: p for p in providers}.get("UPSTOX_FUNDAMENTALS")
        client = build_client()
        try:
            if args.symbols:
                companies = [find_company(session, x.strip()) for x in args.symbols.split(",")]
            else:
                companies = universe.pick_batch(session, args.limit)
            print(f"{len(companies)} companies, steps: {', '.join(steps)}")
            universe.run_batch(
                session,
                companies,
                steps=steps,
                client=client,
                llm=llm,
                upstox=upstox,
                on_company=lambda c: print(
                    f"{c.name[:30]:30} " + " | ".join(f"{k}: {v[:60]}" for k, v in c.steps.items())
                ),
            )
        finally:
            client.close()
        print(json.dumps(universe.coverage(session), indent=2, default=str))
    return 0


def cmd_set_website(args, providers, router) -> int:
    from app.acquisition.evidence_fetch import WebsiteRejected, set_website

    client = build_client()
    try:
        with Session(get_engine()) as session:
            company = find_company(session, args.symbol)
            try:
                stored = set_website(
                    session, company, args.url, client, confirm_issuer=args.confirm_issuer
                )
            except WebsiteRejected as exc:
                print(f"not stored: {exc}")
                return 1
            print(f"stored website for {company.common_name}: {stored}")
    finally:
        client.close()
    return 0


def cmd_fetch_evidence(args, providers, router) -> int:
    from app.acquisition.evidence_fetch import fetch_evidence

    client = build_client()
    try:
        with Session(get_engine()) as session:
            company = find_company(session, args.symbol)
            report = fetch_evidence(session, company, client, force=args.force)
    finally:
        client.close()
    print(
        f"{company.common_name}: {report.status}  (candidates {report.candidates}, "
        f"downloaded {report.downloaded})"
    )
    for note in report.notes:
        print(f"  note: {note}")
    for o in report.outcomes:
        print(f"  {o.status:10} {o.kind:22} {o.period_hint or '?':10} {o.url}  {o.detail}")
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
    p.add_argument(
        "--force-association",
        action="store_true",
        help="link even if the document's first page names a different issuer",
    )
    p = sub.add_parser("import-financials")
    p.add_argument("--file", required=True)
    p = sub.add_parser("backfill-prices")
    p.add_argument("--from", dest="start", required=True, help="first day, YYYY-MM-DD")
    p.add_argument("--to", dest="end", required=True, help="last day, YYYY-MM-DD")
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
    p = sub.add_parser(
        "discover-websites",
        help="find, verify and register official websites for companies that have none",
    )
    p.add_argument(
        "--symbols", help="comma-separated symbols; default: the next batch without a website"
    )
    p.add_argument("--limit", type=int, default=25, help="batch size (default 25)")
    p.add_argument("--no-llm", action="store_true", help="documents and name guesses only")
    p.add_argument("--force", action="store_true", help="ignore the retry back-off")
    p.add_argument(
        "--backfill", action="store_true", help="register already-stored websites in the registry"
    )
    p = sub.add_parser(
        "upstox-refresh", help="refresh Upstox fundamentals for the next batch of due companies"
    )
    p.add_argument("--symbols", help="comma-separated symbols instead of the next due batch")
    p.add_argument("--limit", type=int, default=20, help="batch size (default 20)")
    p.add_argument("--validate", action="store_true", help="sync and validate evidence afterwards")
    p.add_argument("--summary", action="store_true", help="print the coverage summary and exit")
    p = sub.add_parser(
        "enrich", help="extract verified history/leadership quotes from the company's own pages"
    )
    p.add_argument("--symbols", required=True, help="comma-separated symbols")
    p = sub.add_parser(
        "universe", help="one batched, resumable pass: websites, Upstox, documents, enrichment"
    )
    p.add_argument("--symbols", help="comma-separated symbols instead of the next batch")
    p.add_argument("--limit", type=int, default=10, help="batch size (default 10)")
    p.add_argument(
        "--steps", default=",".join(("website", "upstox", "documents", "enrich", "evidence"))
    )
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--status", action="store_true", help="print coverage and exit")
    p = sub.add_parser(
        "website-status", help="registry coverage and the mappings that need a person"
    )
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("set-website", help="store a company's website (only if none is stored)")
    p.add_argument("--symbol", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--confirm-issuer", action="store_true", help="skip the name check")
    p = sub.add_parser(
        "fetch-evidence", help="download the newest public documents from the company's website"
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--force", action="store_true", help="ignore the 7-day cool-down")
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
        "set-website": cmd_set_website,
        "discover-websites": cmd_discover_websites,
        "website-status": cmd_website_status,
        "upstox-refresh": cmd_upstox_refresh,
        "enrich": cmd_enrich,
        "universe": cmd_universe,
        "fetch-evidence": cmd_fetch_evidence,
        "backfill-prices": cmd_backfill_prices,
    }
    try:
        return handlers[args.command](args, providers, router)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
