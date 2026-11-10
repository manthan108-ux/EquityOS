"""Scoring command line.

python -m app.scoring.cli seed
python -m app.scoring.cli benchmarks              # build a new benchmark version
python -m app.scoring.cli benchmarks --show
python -m app.scoring.cli score --symbol HINDUNILVR [--method ABSOLUTE]
python -m app.scoring.cli score-all
python -m app.scoring.cli calibrate
"""

import argparse
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.models import BenchmarkStat, Company, Security
from app.scoring import benchmarks, engine, registry
from app.seeds.sample_universe import SAMPLE


def find(session: Session, symbol: str) -> Company:
    sec = session.scalar(select(Security).where(Security.symbol == symbol))
    if sec is None:
        raise SystemExit(f"unknown symbol {symbol}")
    return session.get(Company, sec.company_id)


def show(out) -> None:
    head = f"{out.company.common_name:32} {out.methodology:11}"
    if out.overall is not None:
        print(
            f"{head} {out.overall:>7.2f}  coverage {out.coverage}%  confidence {out.confidence}"
            f"  peers {out.peer_count} (eligible {len(out.eligible_peers)})"
        )
    else:
        print(f"{head} {'-':>7}  {out.reason}")


def cmd_score(args) -> int:
    with Session(get_engine()) as s:
        company = find(s, args.symbol)
        out = engine.score_company(s, company, args.method)
        s.commit()
        show(out)
        for pillar, value in out.pillar_scores.items():
            print(
                f"    {registry.PRETTY.get(pillar, pillar):24} {Decimal(value):>7.1f}  "
                f"(metric coverage {out.pillar_coverage.get(pillar)}%)"
            )
        for m in out.metrics:
            where = (m.benchmark or {}).get("type", "")
            print(
                f"    - {m.status:8} {m.spec.name[:44]:44} "
                f"{'' if m.value is None else format(m.value, '.4g'):>10} "
                f"{'' if m.score is None else format(m.score, '.1f'):>6} {where:10} "
                f"{m.reason or ''}"[:200]
            )
    return 0


def cmd_score_all(args) -> int:
    with Session(get_engine()) as s:
        pool = engine.all_candidates(s)
        for spec in SAMPLE:
            company = find(s, spec.symbol)
            show(engine.score_company(s, company, args.method, pool=pool))
        s.commit()
    return 0


def cmd_benchmarks(args) -> int:
    with Session(get_engine()) as s:
        if not args.show:
            version = benchmarks.build(s, notes=args.notes)
            s.commit()
            print(f"benchmark version {version.code}  (input hash {version.input_hash[:12]})")
        version = benchmarks.latest_version(s)
        if version is None:
            print("no benchmark version yet")
            return 0
        rows = list(s.scalars(select(BenchmarkStat).where(BenchmarkStat.version_id == version.id)))
        usable = [r for r in rows if r.stats]
        print(f"{version.code}: {len(rows)} distributions, {len(usable)} with enough data")
        for r in sorted(rows, key=lambda r: (r.scope_type, r.scope_code, r.metric_code)):
            print(
                f"  {r.scope_type[:3]}:{r.scope_code:30} {r.metric_code:30} n={r.n_observations:3} "
                f"cos={r.n_companies:2} {r.confidence}"
            )
    return 0


def cmd_seed(_args) -> int:
    with Session(get_engine()) as s:
        print(f"{registry.seed(s)} definition/config rows added")
        s.commit()
    return 0


def cmd_calibrate(_args) -> int:
    from app.models import ScoreRun

    with Session(get_engine()) as s:
        groups: dict[str, list[tuple[float, float]]] = {}
        for spec in SAMPLE:
            company = find(s, spec.symbol)
            rel = engine.score_company(s, company, engine.RELATIVE, persist=False)
            ab = engine.score_absolute(s, company)
            if rel.overall is not None and ab.overall is not None:
                groups.setdefault(rel.profile.code, []).append(
                    (float(ab.overall), float(rel.overall))
                )
        result = engine.calibration(groups)
        print(result)
        _ = ScoreRun
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.scoring.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed").set_defaults(fn=cmd_seed)
    p = sub.add_parser("benchmarks")
    p.add_argument("--show", action="store_true")
    p.add_argument("--notes")
    p.set_defaults(fn=cmd_benchmarks)
    p = sub.add_parser("score")
    p.add_argument("--symbol", required=True)
    p.add_argument("--method", default="AUTO", choices=["AUTO", "RELATIVE", "ABSOLUTE", "HYBRID"])
    p.set_defaults(fn=cmd_score)
    p = sub.add_parser("score-all")
    p.add_argument("--method", default="AUTO", choices=["AUTO", "RELATIVE", "ABSOLUTE", "HYBRID"])
    p.set_defaults(fn=cmd_score_all)
    sub.add_parser("calibrate").set_defaults(fn=cmd_calibrate)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
