"""Price-based calculations: technical indicators and valuation (market cap, P/E, P/B).

Prices are the exchanges' official end-of-day closes (tier 1). A valuation multiplies or divides a
price by statement values from a different date, so every such calculation spans periods on purpose
and says which dates it used. If a split or bonus happened between an earnings or share-count date
and the price date, the statement value is brought to today's share structure with an explicit
adjustment-factor evidence record; the factor is never applied silently.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calc.adjust import factor_between, price_rows, stored_events
from app.calc.metrics import Gap, Inputs
from app.calc.technical_store import STORED_INDICATORS, Skipped, dimension_of, register_technical
from app.evidence.core import canon
from app.evidence.ledger import LedgerError, price_evidence, register_calculation
from app.models import (
    Company,
    EvidenceClass,
    EvidenceRecord,
    EvidenceStatus,
    Exchange,
    Security,
)

CREATED_BY = "calc-engine"


@dataclass
class MarketResult:
    calculated: list[EvidenceRecord] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    security: Security | None = None
    price_date: date | None = None


def choose_security(
    session: Session, company_id: uuid.UUID, today: date
) -> tuple[Security, date] | None:
    """The listing to price the company with: NSE first, the one with the latest stored close."""
    best: tuple[tuple, Security, date] | None = None
    for sec in session.scalars(select(Security).where(Security.company_id == company_id)):
        rows = [r for r in price_rows(session, sec.id) if r.trade_date <= today]
        if not rows:
            continue
        exchange = session.get(Exchange, sec.exchange_id)
        rank = (rows[-1].trade_date, 1 if exchange and exchange.code == "NSE" else 0, len(rows))
        if best is None or rank > best[0]:
            best = (rank, sec, rows[-1].trade_date)
    return (best[1], best[2]) if best else None


def adjustment_evidence(
    session: Session,
    company_id: uuid.UUID,
    security: Security,
    after: date,
    upto: date,
    factor: Decimal,
    now: datetime,
) -> EvidenceRecord:
    """FACT: the cumulative split/bonus factor between two dates, with the events behind it."""
    events = [
        {
            "ex_date": str(e.ex_date),
            "factor": canon(e.factor),
            "origin": e.origin.value,
            "confidence": e.confidence,
            "detail": e.detail,
        }
        for e in stored_events(session, security.id)
        if e.applied and after < e.ex_date <= upto
    ]
    source_id = f"{security.id}|{after}|{upto}"
    digest = hashlib.sha256(json.dumps([source_id, canon(factor), events]).encode()).hexdigest()
    existing = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.source_table == "price_adjustments",
            EvidenceRecord.source_id == source_id,
            EvidenceRecord.content_hash == digest,
        )
    )
    if existing is not None:
        return existing
    record = EvidenceRecord(
        company_id=company_id,
        evidence_class=EvidenceClass.FACT,
        kind="ADJUSTMENT",
        metric="ADJUSTMENT.cumulative_factor",
        dimension=dimension_of(session, security),
        period_end=upto,
        value=factor,
        unit="ratio",
        original_value=factor,
        original_unit="ratio",
        provider_code="MARKET_DATA",
        authority_tier=1,
        source_table="price_adjustments",
        source_id=source_id,
        source_url="derived:price_adjustments",
        retrieved_at=now,
        details={"events": events, "after": str(after), "upto": str(upto)},
        content_hash=digest,
        created_by="calc-engine",
        status_changed_at=now,
    )
    session.add(record)
    session.flush()
    for old in session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.source_table == "price_adjustments",
            EvidenceRecord.source_id == source_id,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.id != record.id,
        )
    ):
        old.status = EvidenceStatus.SUPERSEDED
        old.status_reason = "the adjustment events changed"
        old.status_changed_at = now
    session.flush()
    return record


def _latest(inputs: Inputs, metric: str, ptypes: tuple[str, ...], not_after: date | None = None):
    """Newest usable record of `metric`, consolidated preferred over standalone."""
    keys = [
        k
        for k in (*inputs.usable, *inputs.local)
        if k[0] == metric and k[2] in ptypes and (not_after is None or k[1] <= not_after)
    ]
    if not keys:
        return None
    best = max(keys, key=lambda k: (k[1], k[3] == "CONSOLIDATED"))
    return inputs.record(best), best


def run_market(
    session: Session,
    company: Company,
    inputs: Inputs,
    today: date,
    *,
    now: datetime | None = None,
) -> MarketResult:
    now = now or datetime.now(UTC)
    result = MarketResult()
    chosen = choose_security(session, company.id, today)
    if chosen is None:
        result.gaps.append(
            Gap("TECH.*", None, None, None, "no official prices stored for any listing")
        )
        result.gaps.append(Gap("VALUATION.*", None, None, None, "no official prices stored"))
        return result
    security, price_date = chosen
    result.security, result.price_date = security, price_date

    for indicator in STORED_INDICATORS:
        outcome = register_technical(session, company.id, security, indicator, price_date, now=now)
        if isinstance(outcome, Skipped):
            result.gaps.append(Gap(f"TECH.{indicator}", price_date, None, None, outcome.reason))
        else:
            result.calculated.append(outcome)

    try:
        price = price_evidence(session, company.id, security.id, price_date)
    except LedgerError as exc:
        result.gaps.append(Gap("VALUATION.*", price_date, None, None, str(exc)))
        return result
    events = stored_events(session, security.id)

    def calc(code, expression, ins, unit, notes, basis_gap=None):
        try:
            record = register_calculation(
                session,
                company.id,
                code,
                expression,
                ins,
                created_by=CREATED_BY,
                result_unit=unit,
                spans_periods=True,
                notes=notes,
                definition=code,
                now=now,
            )
        except LedgerError as exc:
            result.gaps.append(
                Gap(code, price_date, None, basis_gap, f"refused by the ledger: {exc}")
            )
            return None
        result.calculated.append(record)
        return record

    def bring_forward(record, label):
        """Evidence and note that bring a statement value to today's share structure, if needed."""
        factor = factor_between(events, record.period_end, price_date)
        if factor == 1:
            return None
        evidence = adjustment_evidence(
            session, company.id, security, record.period_end, price_date, factor, now
        )
        return (
            evidence,
            f"{label} adjusted by factor {canon(factor)} for splits/bonuses after "
            f"{record.period_end}",
        )

    # market capitalisation = price x shares outstanding
    found = _latest(inputs, "SHAREHOLDING.total.shares", ("",), price_date) or _latest(
        inputs, "BALANCE.shares_outstanding", ("ANNUAL", "QUARTERLY"), price_date
    )
    market_cap = None
    if found is None:
        result.gaps.append(
            Gap(
                "VALUATION.market_cap",
                price_date,
                None,
                None,
                "missing input: SHAREHOLDING.total.shares (shares outstanding) is not stored and "
                "BALANCE.shares_outstanding could not be derived from paid-up capital and the "
                "face value",
            )
        )
    else:
        shares, _key = found
        ins = {"price": price.id, "shares": shares.id}
        expression = "price * shares"
        notes = [f"shares outstanding as at {shares.period_end}; price of {price_date}"]
        adj = bring_forward(shares, "share count")
        if adj:
            ins["factor"] = adj[0].id
            expression = "price * (shares * factor)"
            notes.append(adj[1])
        market_cap = calc("VALUATION.market_cap", expression, ins, "inr", notes)

    # price to book
    equity = _latest(inputs, "BALANCE.owners_equity", ("ANNUAL", "QUARTERLY"))
    if market_cap is None:
        result.gaps.append(
            Gap(
                "VALUATION.price_to_book",
                price_date,
                None,
                None,
                "needs market capitalisation, which could not be calculated",
            )
        )
    elif equity is None:
        result.gaps.append(
            Gap(
                "VALUATION.price_to_book",
                price_date,
                None,
                None,
                "missing input: BALANCE.owners_equity",
            )
        )
    else:
        record, key = equity
        calc(
            "VALUATION.price_to_book",
            "cap / equity",
            {"cap": market_cap.id, "equity": record.id},
            "x",
            [f"owners' equity as at {key[1]} ({key[3]}); price of {price_date}"],
            key[3],
        )

    # price to earnings: trailing four quarters, and the latest financial year
    for code, metric, ptypes in (
        ("VALUATION.pe_ttm", "TTM.eps_basic", ("TTM",)),
        ("VALUATION.pe_annual", "INCOME.eps_basic", ("ANNUAL",)),
    ):
        eps = _latest(inputs, metric, ptypes)
        if eps is None:
            result.gaps.append(Gap(code, price_date, None, None, f"missing input: {metric}"))
            continue
        record, key = eps
        if record.value is None or record.value <= 0:
            result.gaps.append(
                Gap(
                    code,
                    key[1],
                    key[2],
                    key[3],
                    f"EPS is {record.value:f}: a price-to-earnings ratio is not meaningful when "
                    "earnings are zero or negative",
                )
            )
            continue
        ins = {"price": price.id, "eps": record.id}
        expression = "price / eps"
        notes = [f"EPS for the period ending {record.period_end} ({key[3]}); price of {price_date}"]
        adj = bring_forward(record, "EPS")
        if adj:
            ins["factor"] = adj[0].id
            expression = "price / (eps / factor)"
            notes.append(adj[1])
        calc(code, expression, ins, "x", notes, key[3])
    return result


__all__ = ["MarketResult", "adjustment_evidence", "choose_security", "run_market"]
