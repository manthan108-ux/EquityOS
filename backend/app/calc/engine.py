"""Orchestrates one calculation run for a company.

validate evidence -> rebuild price adjustments -> fundamentals -> prices (technical, valuation)
-> record the run and its gaps -> validate again (lineage of everything just calculated)
"""

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.calc.adjust import AdjustmentReport, rebuild_adjustments
from app.calc.market import MarketResult, run_market
from app.calc.metrics import FundamentalsResult, Gap, Inputs, run_fundamentals
from app.evidence.engine import ValidationReport, validate_company
from app.models import CalculationGap, CalculationRun, Company

ENGINE_VERSION = "7.0"


@dataclass
class CalculationReport:
    run: CalculationRun
    fundamentals: FundamentalsResult
    market: MarketResult
    adjustments: AdjustmentReport
    validation: ValidationReport

    @property
    def gaps(self) -> list[Gap]:
        return [*self.fundamentals.gaps, *self.market.gaps]

    @property
    def calculated(self) -> int:
        return len(self.fundamentals.calculated) + len(self.market.calculated)


def calculate_company(
    session: Session,
    company_id: uuid.UUID,
    *,
    today: date | None = None,
    now: datetime | None = None,
    validate_first: bool = True,
) -> CalculationReport:
    now = now or datetime.now(UTC)
    today = today or now.date()
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError(f"no company {company_id}")
    if validate_first:
        validate_company(session, company_id, today=today, now=now)
    adjustments = rebuild_adjustments(session, company_id, now=now)
    fundamentals = run_fundamentals(session, company_id, now=now)
    # The market run needs the derived equity / TTM EPS made just above.
    inputs = Inputs(session, company_id)
    for record in fundamentals.calculated:
        inputs.local[
            (record.metric, record.period_end, record.period_type or "", record.basis or "")
        ] = record
    market = run_market(session, company, inputs, today, now=now)

    gaps = [*fundamentals.gaps, *market.gaps]
    run = CalculationRun(
        company_id=company_id,
        engine_version=ENGINE_VERSION,
        started_at=now,
        finished_at=datetime.now(UTC),
        as_of=today,
        profile=fundamentals.profile,
        calculated=len(fundamentals.calculated) + len(market.calculated),
        gaps=len(gaps),
        summary={
            "profile": fundamentals.profile,
            "price_date": str(market.price_date) if market.price_date else None,
            "security_id": market.security.id if market.security else None,
            "by_category": dict(
                Counter(
                    r.metric.split(".")[0] for r in [*fundamentals.calculated, *market.calculated]
                )
            ),
            "gaps_by_metric": dict(Counter(g.metric for g in gaps)),
            "adjustments_applied": len(adjustments.applied()),
            "unexplained_price_gaps": len(adjustments.unexplained()),
            "skipped_corporate_actions": adjustments.skipped_actions,
        },
    )
    session.add(run)
    session.flush()
    for g in gaps:
        session.add(
            CalculationGap(
                run_id=run.id,
                metric=g.metric,
                period_end=g.period_end,
                period_type=g.period_type,
                basis=g.basis,
                reason=g.reason,
                missing=g.missing or None,
            )
        )
    session.flush()
    validation = validate_company(session, company_id, today=today, now=now, sync=False)
    return CalculationReport(run, fundamentals, market, adjustments, validation)


__all__ = ["CalculationReport", "ENGINE_VERSION", "calculate_company"]
