"""Pipeline control from the terminal. Run from ./backend:

    python -m app.pipeline.cli run --symbols 63MOONS --stages extract,evidence,readiness
    python -m app.pipeline.cli run --limit 10                  # the next batch, every stage
    python -m app.pipeline.cli run --select missing-evidence --limit 20 --stages evidence,readiness
    python -m app.pipeline.cli retry-failed --limit 20
    python -m app.pipeline.cli status [--run 7] [--symbol TCS]
    python -m app.pipeline.cli serve                           # worker: schedule + queued runs
    python -m app.pipeline.cli pause|resume|cancel 7
    python -m app.pipeline.cli schedule --enable --interval 360 --batch 10 --workers 2
    python -m app.pipeline.cli review-websites [--symbols A,B] [--limit 20]
    python -m app.pipeline.cli mark-website --symbol AAPLUS --reason "..." [--status AMBIGUOUS]

`run` creates a run record and executes it in this process; `serve` executes runs created by the
API or the schedule. Both resume an interrupted run where it stopped.
"""

import argparse
import json
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.models import Company, PipelineEvent, PipelineRun, PipelineStageState
from app.pipeline import backlog, runner, scheduler
from app.pipeline import state as st
from app.pipeline.state import STAGES


def _factory():
    engine = get_engine()
    return lambda: Session(engine)


def _context(no_llm: bool = False) -> dict:
    """The shared, polite HTTP client and the optional services a run needs."""
    from app.acquisition.factory import build_client
    from app.llm.providers import default_router
    from app.providers.registry import build_providers

    client = build_client(shared_pacing=True)
    providers = build_providers(client)
    llm = None if no_llm else default_router()
    if llm is not None and not llm.providers:
        llm = None
    upstox = {p.spec.code: p for p in providers}.get("UPSTOX_FUNDAMENTALS")
    return {"client": client, "llm": llm, "upstox": upstox, "close": client.close}


def _timeouts(items) -> dict:
    """`--stage-timeout documents=600` (repeatable) -> {"documents": 600.0}."""
    out = {}
    for item in items or []:
        stage, _, seconds = item.partition("=")
        if stage not in STAGES or not seconds.replace(".", "", 1).isdigit():
            raise SystemExit(
                f"--stage-timeout wants STAGE=SECONDS with a stage from {list(STAGES)}"
            )
        out[stage] = float(seconds)
    return out


def _print_company(result) -> None:
    line = " | ".join(f"{k}: {v[:48]}" for k, v in result.steps.items())
    print(f"{result.name[:30]:30} {line}", flush=True)


def cmd_run(args) -> int:
    factory = _factory()
    ctx = _context(args.no_llm)
    try:
        with factory() as s:
            stages = runner.validate_stages(args.stages.split(",")) if args.stages else list(STAGES)
            symbols = [x.strip() for x in args.symbols.split(",")] if args.symbols else None
            options = {"workers": args.workers, "force": args.force}
            if args.select:
                options["select"] = args.select
            if args.respect_backoff:
                options["respect_backoff"] = True
            timeouts = _timeouts(args.stage_timeout)
            if timeouts:
                options["timeouts"] = timeouts
            trigger = "SINGLE" if symbols and len(symbols) == 1 else "MANUAL"
            if args.stages and (symbols or args.select):
                trigger = "STAGE" if trigger == "MANUAL" else trigger
            run = runner.create_run(
                s,
                trigger=trigger,
                stages=stages,
                symbols=symbols,
                batch_size=args.limit,
                options=options,
                created_by="cli",
            )
            run_id = run.id
            runner.claim_run(s, run_id)
        print(f"run {run_id}: stages {', '.join(stages)}", flush=True)
        done = runner.execute(
            factory,
            run_id,
            client=ctx["client"],
            llm=ctx["llm"],
            upstox=ctx["upstox"],
            workers=args.workers,
            on_company=_print_company,
            isolation="thread" if args.no_isolation else "process",
        )
        print(f"run {run_id} {done.status}: {json.dumps(done.summary, default=str)}")
    finally:
        ctx["close"]()
    return 0


def cmd_retry_failed(args) -> int:
    args.select, args.symbols, args.respect_backoff = "failed", None, True
    args.stages = args.stages or ",".join(STAGES)
    args.force = False
    return cmd_run(args)


def cmd_serve(args) -> int:
    factory = _factory()
    print("pipeline worker started; Ctrl+C to stop", flush=True)
    try:
        n = scheduler.serve(
            factory,
            lambda: _context(args.no_llm),
            poll_seconds=args.poll,
            once=args.once,
            isolation="thread" if args.no_isolation else "process",
        )
    except KeyboardInterrupt:
        print("stopped; any run in progress resumes when the worker restarts")
        return 0
    print(f"{n} run(s) executed")
    return 0


def cmd_control(args) -> int:
    with Session(get_engine()) as s:
        run = runner.request_control(s, args.run, args.command)
        print(f"run {run.id}: {run.status} (control {run.control})")
    return 0


def cmd_status(args) -> int:
    with Session(get_engine()) as s:
        if args.symbol:
            from app.providers.cli import find_company

            company = find_company(s, args.symbol)
            rows = st.states_for(s, [company.id])[company.id]
            for stage in STAGES:
                x = rows.get(stage)
                print(
                    f"{stage:10} {x.status if x else '-':10} attempts={x.attempts if x else 0} "
                    f"{(x.message or x.error or '')[:80] if x else ''}"
                )
            return 0
        counts = s.execute(
            select(PipelineStageState.stage, PipelineStageState.status, func.count())
            .group_by(PipelineStageState.stage, PipelineStageState.status)
            .order_by(PipelineStageState.stage)
        ).all()
        table: dict[str, dict[str, int]] = {}
        for stage, status, n in counts:
            table.setdefault(stage, {})[status] = n
        run = s.get(PipelineRun, args.run) if args.run else runner.active_run(s)
        if run is None:
            run = s.scalar(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1))
        out = {
            "backlog": backlog.summary(s),
            "stage_states": table,
            "run": None
            if run is None
            else {
                "id": run.id,
                "status": run.status,
                "trigger": run.trigger,
                "stages": run.stages,
                "summary": run.summary,
            },
        }
        print(json.dumps(out, indent=2, default=str))
    return 0


def _pilot_symbols() -> list[str]:
    from app.seeds.sample_universe import SAMPLE

    return [c.symbol for c in SAMPLE]


def cmd_schedule(args) -> int:
    symbols = _pilot_symbols() if args.pilot else None
    if args.symbols:
        symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    with Session(get_engine()) as s:
        try:
            row = scheduler.update_schedule(
                s,
                enabled=True if args.enable else False if args.disable else None,
                interval_minutes=args.interval,
                batch_size=args.batch,
                concurrency=args.workers,
                stages=[] if args.all_stages else args.stages.split(",") if args.stages else None,
                symbols=symbols,
                clear_symbols=args.clear_symbols,
                allow_universe=args.allow_universe,
            )
        except ValueError as exc:
            raise SystemExit(f"schedule not changed: {exc}") from exc
        print(
            json.dumps(
                {
                    "enabled": row.enabled,
                    "interval_minutes": row.interval_minutes,
                    "batch_size": row.batch_size,
                    "concurrency": row.concurrency,
                    "stages": row.stages,
                    "symbols": row.symbols,
                    "last_triggered_at": row.last_triggered_at,
                    "worker_seen_at": row.worker_seen_at,
                },
                indent=2,
                default=str,
            )
        )
    return 0


def cmd_metrics(args) -> int:
    from app.pipeline import metrics

    with Session(get_engine()) as s:
        rows = metrics.stage_metrics(s, hours=args.hours)
        runs = metrics.run_metrics(s, limit=args.runs)
    if args.json:
        print(json.dumps({"stages": rows, "runs": runs}, indent=2, default=str))
        return 0
    print(f"stage metrics, last {args.hours} h (seconds)")
    print(
        f"{'stage':10} {'att':>5} {'ok':>5} {'skip':>5} {'fail':>5} {'t/o':>4} "
        f"{'p50':>7} {'p95':>7} {'max':>7} {'budget':>7}"
    )
    for m in rows:

        def f(v):
            return "-" if v is None else f"{v:.1f}"

        print(
            f"{m['stage']:10} {m['attempts']:>5} {m['succeeded']:>5} {m['skipped']:>5} "
            f"{m['failed']:>5} {m['timed_out']:>4} {f(m['p50_seconds']):>7} "
            f"{f(m['p95_seconds']):>7} {f(m['max_seconds']):>7} {m['budget_seconds']:>7.0f}"
        )
    print("recent runs")
    for r in runs:
        print(
            f"  run {r['run_id']:>3} {r['trigger']:9} {r['status']:10} "
            f"companies={r['processed']}/{r['companies']} failed_stages={r['failed_stages']} "
            f"duration={r['duration_seconds']}s"
        )
    return 0


def cmd_health(args) -> int:
    from app.pipeline import metrics

    with Session(get_engine()) as s:
        report = metrics.health(s, hours=args.hours)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        w = report["worker"]
        print(f"{report['status']}  worker alive: {w['alive']}  last seen: {w['last_seen_at']}")
        for a in report["alerts"]:
            print(f"  [{a['level']}] {a['code']}: {a['message']}")
        if not report["alerts"]:
            print("  no alerts")
    return 0 if report["status"] != metrics.CRITICAL else 2


def cmd_review_websites(args) -> int:
    from app.acquisition import website_discovery as wd
    from app.models import CompanyWebsiteRegistry
    from app.providers.cli import find_company

    ctx = _context(no_llm=True)
    try:
        with Session(get_engine()) as s:
            if args.symbols:
                companies = [find_company(s, x.strip()) for x in args.symbols.split(",")]
            else:
                companies = list(
                    s.scalars(
                        select(Company)
                        .join(
                            CompanyWebsiteRegistry, CompanyWebsiteRegistry.company_id == Company.id
                        )
                        .where(CompanyWebsiteRegistry.status == wd.VERIFIED)
                        .order_by(Company.common_name)
                        .limit(args.limit)
                    )
                )
            for c in companies:
                r = wd.review_registered(s, c, ctx["client"])
                print(f"{c.common_name[:34]:34} {r['outcome']:10} {r['reason'][:110]}", flush=True)
    finally:
        ctx["close"]()
    return 0


def cmd_mark_website(args) -> int:
    from app.acquisition import website_discovery as wd
    from app.providers.cli import find_company

    with Session(get_engine()) as s:
        company = find_company(s, args.symbol)
        row = wd.mark_unverified(
            s, company, reason=args.reason, status=args.status, actor="cli-review"
        )
        print(f"{company.common_name}: {row.status}; website cleared from the company")
    return 0


def cmd_events(args) -> int:
    with Session(get_engine()) as s:
        q = select(PipelineEvent).order_by(PipelineEvent.id.desc()).limit(args.limit)
        if args.run:
            q = q.where(PipelineEvent.run_id == args.run)
        for e in reversed(list(s.scalars(q))):
            print(
                f"{e.at:%d/%m %H:%M:%S} run={e.run_id} {e.stage or '-':10} "
                f"{e.status:10} {e.message}"
            )
    return 0


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m app.pipeline.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    def run_args(p):
        p.add_argument("--limit", type=int, default=10)
        p.add_argument("--symbols")
        p.add_argument("--stages", help=f"comma list of {', '.join(STAGES)}")
        p.add_argument("--workers", type=int, default=1)
        p.add_argument("--no-llm", action="store_true")
        p.add_argument(
            "--stage-timeout",
            action="append",
            metavar="STAGE=SECONDS",
            help="override a stage's wall-clock budget (repeatable)",
        )
        p.add_argument(
            "--no-isolation",
            action="store_true",
            help="run every stage in the worker's own thread (no timeout can stop it)",
        )

    p = sub.add_parser("run")
    run_args(p)
    p.add_argument("--select", choices=["failed", "missing-evidence", "unread-documents", "auto"])
    p.add_argument("--force", action="store_true", help="repeat stages even when current")
    p.add_argument("--respect-backoff", action="store_true")
    p = sub.add_parser("retry-failed")
    run_args(p)
    p = sub.add_parser("serve")
    p.add_argument("--poll", type=int, default=30)
    p.add_argument("--once", action="store_true", help="execute what is queued, then exit")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--no-isolation", action="store_true")
    for name in ("pause", "resume", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("run", type=int)
    p = sub.add_parser("status")
    p.add_argument("--run", type=int)
    p.add_argument("--symbol")
    p = sub.add_parser("metrics", help="stage durations and outcomes")
    p.add_argument("--hours", type=int, default=168)
    p.add_argument("--runs", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("health", help="alerts: stuck stages, dead worker, failing stages")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("events")
    p.add_argument("--run", type=int)
    p.add_argument("--limit", type=int, default=40)
    p = sub.add_parser("schedule")
    p.add_argument("--enable", action="store_true")
    p.add_argument("--disable", action="store_true")
    p.add_argument("--interval", type=int)
    p.add_argument("--batch", type=int)
    p.add_argument("--workers", type=int)
    p.add_argument("--stages")
    p.add_argument("--all-stages", action="store_true", help="forget a stored stage list")
    p.add_argument("--symbols", help="comma list: the selected companies to work on")
    p.add_argument("--pilot", action="store_true", help="the 24-company pilot universe")
    p.add_argument("--clear-symbols", action="store_true")
    p.add_argument(
        "--allow-universe",
        action="store_true",
        help="let the schedule take batches from the whole universe (off by default)",
    )
    p = sub.add_parser("review-websites")
    p.add_argument("--symbols")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("mark-website")
    p.add_argument("--symbol", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--status", default="AMBIGUOUS", choices=["AMBIGUOUS", "FAILED"])
    args = parser.parse_args(argv)
    handlers = {
        "run": cmd_run,
        "retry-failed": cmd_retry_failed,
        "serve": cmd_serve,
        "pause": cmd_control,
        "resume": cmd_control,
        "cancel": cmd_control,
        "status": cmd_status,
        "events": cmd_events,
        "metrics": cmd_metrics,
        "health": cmd_health,
        "schedule": cmd_schedule,
        "review-websites": cmd_review_websites,
        "mark-website": cmd_mark_website,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
