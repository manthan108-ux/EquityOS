"""Writing provider data to the normalized tables, with the raw response archived alongside."""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    Basis,
    CorporateAction,
    FinancialFact,
    PeriodType,
    RatioSnapshot,
    RawPayload,
    ShareholdingFact,
    Statement,
)


@dataclass(frozen=True)
class FactRow:
    statement: Statement
    line_item: str
    period_end: date
    period_type: PeriodType
    basis: Basis
    original_value: Decimal
    original_unit: str
    period_label: str
    value_inr: Decimal | None  # None when the unit is not convertible (never assumed)


@dataclass(frozen=True)
class HoldingRow:
    category: str
    period_end: date
    period_label: str
    percent: Decimal


@dataclass(frozen=True)
class ActionRow:
    action_type: str
    effective_date: date | None
    amount: Decimal | None
    ratio_text: str
    details: list | None


@dataclass(frozen=True)
class RatioRow:
    name: str
    company_value_raw: str
    sector_value_raw: str | None
    company_value: Decimal | None
    sector_value: Decimal | None
    is_percent: bool


def archive_payload(
    session: Session,
    *,
    company_id: uuid.UUID | None,
    provider_code: str,
    dataset: str,
    request_url: str,
    payload: dict | list,
    retrieved_at: datetime,
) -> RawPayload:
    """Store the response verbatim. An identical response is not stored twice."""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    existing = session.scalar(
        select(RawPayload).where(
            RawPayload.company_id == company_id,
            RawPayload.provider_code == provider_code,
            RawPayload.dataset == dataset,
            RawPayload.request_url == request_url,
            RawPayload.sha256 == digest,
        )
    )
    if existing is not None:
        return existing
    row = RawPayload(
        company_id=company_id,
        provider_code=provider_code,
        dataset=dataset,
        request_url=request_url,
        payload=payload,
        sha256=digest,
        retrieved_at=retrieved_at,
    )
    session.add(row)
    session.flush()
    return row


def store_financial_facts(
    session: Session,
    company_id: uuid.UUID,
    provider_code: str,
    rows: list[FactRow],
    *,
    source_url: str,
    retrieved_at: datetime,
    payload_id: int | None,
) -> int:
    """Upsert facts. A re-fetch refreshes values (providers restate); the earlier raw payloads
    stay archived in `raw_payloads`, so prior values remain recoverable."""
    if not rows:
        return 0
    values = [
        {
            "company_id": company_id,
            "provider_code": provider_code,
            "statement": r.statement,
            "line_item": r.line_item,
            "period_end": r.period_end,
            "period_type": r.period_type,
            "basis": r.basis,
            "original_value": r.original_value,
            "original_unit": r.original_unit,
            "period_label": r.period_label,
            "value_inr": r.value_inr,
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "payload_id": payload_id,
        }
        for r in rows
    ]
    stmt = insert(FinancialFact).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            "company_id",
            "provider_code",
            "statement",
            "line_item",
            "period_end",
            "period_type",
            "basis",
        ],
        set_={
            "original_value": stmt.excluded.original_value,
            "original_unit": stmt.excluded.original_unit,
            "period_label": stmt.excluded.period_label,
            "value_inr": stmt.excluded.value_inr,
            "source_url": stmt.excluded.source_url,
            "retrieved_at": stmt.excluded.retrieved_at,
            "payload_id": stmt.excluded.payload_id,
        },
    )
    session.execute(stmt)
    return len(values)


def store_shareholding(
    session: Session,
    company_id: uuid.UUID,
    provider_code: str,
    rows: list[HoldingRow],
    *,
    source_url: str,
    retrieved_at: datetime,
    payload_id: int | None,
) -> int:
    if not rows:
        return 0
    values = [
        {
            "company_id": company_id,
            "provider_code": provider_code,
            "category": r.category,
            "period_end": r.period_end,
            "period_label": r.period_label,
            "percent": r.percent,
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "payload_id": payload_id,
        }
        for r in rows
    ]
    stmt = insert(ShareholdingFact).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["company_id", "provider_code", "category", "period_end"],
        set_={
            "percent": stmt.excluded.percent,
            "period_label": stmt.excluded.period_label,
            "source_url": stmt.excluded.source_url,
            "retrieved_at": stmt.excluded.retrieved_at,
            "payload_id": stmt.excluded.payload_id,
        },
    )
    session.execute(stmt)
    return len(values)


def store_corporate_actions(
    session: Session,
    company_id: uuid.UUID,
    provider_code: str,
    rows: list[ActionRow],
    *,
    source_url: str,
    retrieved_at: datetime,
    payload_id: int | None,
) -> int:
    if not rows:
        return 0
    values = [
        {
            "company_id": company_id,
            "provider_code": provider_code,
            "action_type": r.action_type,
            "effective_date": r.effective_date,
            "amount": r.amount,
            "ratio_text": r.ratio_text,
            "details": r.details,
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "payload_id": payload_id,
        }
        for r in rows
    ]
    stmt = insert(CorporateAction).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            "company_id",
            "provider_code",
            "action_type",
            "effective_date",
            "ratio_text",
        ],
        set_={
            "amount": stmt.excluded.amount,
            "details": stmt.excluded.details,
            "source_url": stmt.excluded.source_url,
            "retrieved_at": stmt.excluded.retrieved_at,
            "payload_id": stmt.excluded.payload_id,
        },
    )
    session.execute(stmt)
    return len(values)


def store_ratio_snapshot(
    session: Session,
    company_id: uuid.UUID,
    provider_code: str,
    rows: list[RatioRow],
    *,
    source_url: str,
    retrieved_at: datetime,
) -> int:
    for r in rows:
        session.add(
            RatioSnapshot(
                company_id=company_id,
                provider_code=provider_code,
                name=r.name,
                company_value_raw=r.company_value_raw,
                sector_value_raw=r.sector_value_raw,
                company_value=r.company_value,
                sector_value=r.sector_value,
                is_percent=r.is_percent,
                source_url=source_url,
                retrieved_at=retrieved_at,
            )
        )
    session.flush()
    return len(rows)
