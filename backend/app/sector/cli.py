"""Sector and peer commands. Run from ./backend with the database up:

python -m app.sector.cli analyze --symbol TCS         # peers, comparisons, scores (records the run)
python -m app.sector.cli analyze-all                  # every company with calculated metrics
python -m app.sector.cli peers --symbol TCS           # who the peers are and why
python -m app.sector.cli scorecard --symbol TCS       # pillar scores, weights, every comparison
python -m app.sector.cli weights [--sector banking]   # the curated weight sets and their reasons
"""

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.models import (
    Company,
    EvidenceRecord,
    EvidenceStatus,
    Peer,
    PeerSet,
    SectorWeightSet,
    WeightAdjustment,
)
from app.providers.cli import find_company
from app.sector.engine import analyze_all, analyze_company, ensure_weight_sets
from app.sector.profiles import PILLARS


def _print_report(r) -> None:
    c = r.company
    if r.candidate is None:
        print(f"{c.common_name}: no calculated metrics yet (run `python -m app.calc.cli run`)")
        return
    ps = r.peer_set
    print(
        f"{c.common_name} [{r.candidate.profile}, sector {r.candidate.sector_code or 'NOT SET'}, "
        f"weights {r.profile.sector_code}]  basis {r.basis}"
    )
    print(
        f"  peers ({len(r.picks)}{', THIN: fewer than 3' if ps.thin else ''}): "
        + (", ".join(p.candidate.name for p in r.picks) or "none")
    )
    compared = [o for o in r.outcomes if o.status == "COMPARED"]
    print(f"  metrics compared: {len(compared)}   gaps: {len(r.outcomes) - len(compared)}")
    card = r.scorecard
    if card and card.pillar_scores:
        for pillar in PILLARS:
            if pillar in card.pillar_scores:
                print(
                    f"    {pillar:14} {float(card.pillar_scores[pillar]):6.1f}   "
                    f"weight {float(card.weights.get(pillar, 0)):5.1f}"
                )
        if card.overall is not None:
            print(
                f"  OVERALL {float(card.overall):.1f}   "
                f"(weight coverage {float(card.coverage):.0f}%)"
            )
        else:
            print(f"  no overall score: {card.withheld}")
    if r.validation:
        print(
            f"  validation: {r.validation.run.errors} error(s), "
            f"{r.validation.run.warnings} warning(s)"
        )


def cmd_analyze(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        report = analyze_company(session, company.id)
        session.commit()
        _print_report(report)
    return 0


def cmd_analyze_all(args) -> int:
    with Session(get_engine()) as session:
        for report in analyze_all(session):
            _print_report(report)
            print()
        session.commit()
    return 0


def _latest_set(session: Session, company: Company) -> PeerSet:
    ps = session.scalar(
        select(PeerSet).where(PeerSet.company_id == company.id).order_by(PeerSet.id.desc()).limit(1)
    )
    if ps is None:
        raise SystemExit("No analysis yet (use `analyze`).")
    return ps


def cmd_peers(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        ps = _latest_set(session, company)
        print(
            f"{company.common_name}: {ps.peer_count} peer(s) of {ps.candidates} candidate(s), "
            f"sector {ps.sector_code}{', THIN' if ps.thin else ''}"
        )
        for peer in session.scalars(
            select(Peer).where(Peer.peer_set_id == ps.id).order_by(Peer.rank)
        ):
            name = session.get(Company, peer.company_id).common_name
            print(f"  {peer.rank}. {name:28} {peer.tier:14} {peer.reason}")
        for item in (ps.summary or {}).get("rejected", []):
            print(f"  - left out: {item['company']}: {item['reason']}")
    return 0


def cmd_scorecard(args) -> int:
    with Session(get_engine()) as session:
        company = find_company(session, args.symbol)
        ps = _latest_set(session, company)
        summary = ps.summary or {}
        print(f"{company.common_name}  (run {ps.id}, basis {ps.criteria.get('basis')})")
        print("  pillar scores:", summary.get("pillar_scores"))
        print(
            "  overall:", summary.get("overall") or f"withheld - {summary.get('overall_withheld')}"
        )
        for adj in session.scalars(
            select(WeightAdjustment).where(WeightAdjustment.peer_set_id == ps.id)
        ):
            if adj.base_weight != adj.adjusted_weight:
                print(
                    f"  weight {adj.pillar}: {adj.base_weight} -> {adj.adjusted_weight:.1f} "
                    f"({adj.reason})"
                )
        print("  comparisons:")
        for r in session.scalars(
            select(EvidenceRecord)
            .where(
                EvidenceRecord.company_id == company.id,
                EvidenceRecord.metric.like("PEERRANK.%"),
                EvidenceRecord.status == EvidenceStatus.ACTIVE,
            )
            .order_by(EvidenceRecord.metric)
        ):
            d = r.details
            print(
                f"    {d['pillar']:14} {d['label'][:34]:34} "
                f"company {float(d['subject']['value']):>10.2f}"
                f"  beats {float(r.value):5.1f}% of {d['n']} peer(s)"
            )
        for gap in summary.get("gaps", []):
            print(f"    gap: {gap['metric']}: {gap['reason']}")
    return 0


def cmd_weights(args) -> int:
    with Session(get_engine()) as session:
        ensure_weight_sets(session)
        session.commit()
        query = select(SectorWeightSet).order_by(SectorWeightSet.sector_code)
        if args.sector:
            query = query.where(SectorWeightSet.sector_code == args.sector)
        for row in session.scalars(query):
            print(f"{row.sector_code} v{row.version} ({row.profile}): {row.pillar_weights}")
            for pillar, why in row.rationale.items():
                print(f"    {pillar:14} {why}")
            print(f"    {row.note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.sector.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("analyze", cmd_analyze), ("peers", cmd_peers), ("scorecard", cmd_scorecard)):
        p = sub.add_parser(name)
        p.add_argument("--symbol", required=True)
        p.set_defaults(func=fn)
    sub.add_parser("analyze-all").set_defaults(func=cmd_analyze_all)
    p = sub.add_parser("weights")
    p.add_argument("--sector")
    p.set_defaults(func=cmd_weights)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
