"""Technical indicators as CALCULATION evidence, reproducible from the stored prices.

An indicator is computed from hundreds of price rows, so it is not linked to each of them.
Instead the
record stores the method, the indicator, the security, the date and a digest of the exact adjusted
prices used. `recompute` repeats the calculation from the database; if the prices or the adjustment
events changed since, the digest or the value no longer match and validation flags the record.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calc.adjust import Bar, adjusted_bars, stored_events
from app.calc.technical import INDICATORS, InsufficientHistory, compute
from app.evidence.core import canon
from app.evidence.ledger import ENGINE_PROVIDER
from app.models import (
    EvidenceClass,
    EvidenceRecord,
    EvidenceStatus,
    Exchange,
    Security,
)

METHOD = "technical.v1"
TOLERANCE = Decimal("0.000001")
STORED_INDICATORS = [name for name in INDICATORS if name != "close"]


def price_digest(bars: list[Bar]) -> str:
    """Fingerprint of the exact prices (raw close and adjustment factor per day) behind a value."""
    payload = [(str(b.trade_date), str(b.raw_close), canon(b.cumulative_factor)) for b in bars]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def dimension_of(session: Session, security: Security) -> str:
    exchange = session.get(Exchange, security.exchange_id)
    return f"{exchange.code if exchange else security.exchange_id}:{security.symbol}"


@dataclass
class Skipped:
    indicator: str
    reason: str


def register_technical(
    session: Session,
    company_id: uuid.UUID,
    security: Security,
    indicator: str,
    as_of: date,
    *,
    created_by: str = "calc-engine",
    now: datetime | None = None,
) -> EvidenceRecord | Skipped:
    bars = adjusted_bars(session, security.id, upto=as_of)
    try:
        reading = compute(indicator, bars, as_of)
    except InsufficientHistory as exc:
        return Skipped(indicator, str(exc))
    used = [b for b in bars if b.trade_date <= as_of]
    events = [
        {
            "ex_date": str(e.ex_date),
            "factor": canon(e.factor),
            "origin": e.origin.value,
            "confidence": e.confidence,
        }
        for e in stored_events(session, security.id)
        if e.applied and used and e.ex_date > used[0].trade_date
    ]
    details = {
        "method": METHOD,
        "indicator": indicator,
        "security_id": security.id,
        "as_of": str(as_of),
        "last_bar": str(used[-1].trade_date),
        "first_bar": str(used[0].trade_date),
        "bars": len(used),
        "price_digest": price_digest(used),
        "adjustments_applied": events,
        "reading": {k: str(v) for k, v in reading.note.items()},
        "prices": "official end-of-day, adjusted for splits and bonuses only",
    }
    dimension = dimension_of(session, security)
    metric = f"TECH.{indicator}"
    digest = hashlib.sha256(
        json.dumps(
            [
                str(company_id),
                metric,
                dimension,
                str(as_of),
                canon(reading.value),
                details["price_digest"],
            ]
        ).encode()
    ).hexdigest()
    existing = session.scalar(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.evidence_class == EvidenceClass.CALCULATION,
            EvidenceRecord.content_hash == digest,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
        )
    )
    if existing is not None:
        return existing
    stamp = now or datetime.now(UTC)
    record = EvidenceRecord(
        company_id=company_id,
        evidence_class=EvidenceClass.CALCULATION,
        kind="TECHNICAL",
        metric=metric,
        dimension=dimension,
        period_end=as_of,
        value=reading.value,
        unit=reading.unit,
        expression=f"{indicator}(adjusted official end-of-day prices)",
        provider_code=ENGINE_PROVIDER,
        authority_tier=1,
        source_url=f"calculation:{metric}",
        retrieved_at=stamp,
        details=details,
        content_hash=digest,
        created_by=created_by,
        status_changed_at=stamp,
    )
    session.add(record)
    session.flush()
    for old in session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.company_id == company_id,
            EvidenceRecord.metric == metric,
            EvidenceRecord.dimension == dimension,
            EvidenceRecord.period_end == as_of,
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.id != record.id,
        )
    ):
        old.status = EvidenceStatus.SUPERSEDED
        old.status_reason = f"recalculated as evidence {record.id}"
        old.status_changed_at = stamp
    session.flush()
    return record


def recompute(session: Session, record: EvidenceRecord) -> tuple[Decimal, str]:
    """(value, price digest) recomputed from the database for a stored technical record."""
    d = record.details or {}
    as_of = date.fromisoformat(d["as_of"])
    bars = adjusted_bars(session, int(d["security_id"]), upto=as_of)
    used = [b for b in bars if b.trade_date <= as_of]
    return compute(d["indicator"], bars, as_of).value, price_digest(used)


__all__ = [
    "METHOD",
    "STORED_INDICATORS",
    "Skipped",
    "dimension_of",
    "price_digest",
    "recompute",
    "register_technical",
]
