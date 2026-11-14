"""Industry classification from the provider's own profile, stored with where and when it was read.

Upstox's profile response carries `sector` (for example "Refineries") and the combined market
capitalisation of that sector. Nothing here is guessed: a label is stored only when the profile
text names the company (the same identity check the fundamentals refresh uses) and the value is a
plain, bounded string. The label is the PROVIDER's; reports call it that.

`backfill` reads the profile endpoint only (one request per company), so a classification can be
collected for the whole universe much faster than a full fundamentals refresh, in resumable batches.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.docintel.entity import company_names
from app.models import Company, CompanyClassification, Security
from app.providers.upstox import SOURCE_CODE
from app.providers.upstox_refresh import BREAKER, company_ref, identity_matches

log = logging.getLogger("equityos.classification")
SCHEME = "UPSTOX_SECTOR"
SOURCE = "Upstox Fundamentals API, company profile (field: sector)"
MAX_LABEL = 120
MAX_DESCRIPTION = 700


def parse_profile(body) -> tuple[str, Decimal | None] | None:
    """(sector label, sector market cap in crore) from a profile body, or None if absent/odd."""
    data = (body or {}).get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return None
    label = data.get("sector")
    if not isinstance(label, str):
        return None
    label = " ".join(label.split())
    if not label or len(label) > MAX_LABEL or not any(c.isalpha() for c in label):
        return None
    cap = data.get("sector_market_cap_inr")
    crore = None
    if isinstance(cap, dict) and str(cap.get("unit", "")).lower() == "crore":
        try:
            crore = Decimal(str(cap.get("value")))
        except (InvalidOperation, ValueError):
            crore = None
        if crore is not None and crore <= 0:
            crore = None
    return label, crore


def store(
    session: Session, company: Company, body, retrieved_at: datetime | None = None
) -> CompanyClassification | None:
    """Upsert the classification from a profile body the caller has already identity-checked."""
    parsed = parse_profile(body)
    if parsed is None:
        return None
    label, crore = parsed
    now = retrieved_at or datetime.now(UTC)
    text = ((body.get("data") or {}).get("company_profile")) if isinstance(body, dict) else None
    description = None
    if isinstance(text, str) and text:
        description = " ".join(text.split())[:MAX_DESCRIPTION]
    # one atomic statement: two runs reading the same company at once cannot collide
    stmt = pg_insert(CompanyClassification).values(
        company_id=company.id,
        scheme=SCHEME,
        label=label,
        group_market_cap_crore=crore,
        retrieved_at=now,
        source=SOURCE,
        description=description,
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["company_id", "scheme"],
            set_={
                "label": stmt.excluded.label,
                "group_market_cap_crore": stmt.excluded.group_market_cap_crore,
                "retrieved_at": stmt.excluded.retrieved_at,
                "source": stmt.excluded.source,
                "description": stmt.excluded.description,
            },
        )
    )
    session.flush()
    session.expire_all()
    return session.get(CompanyClassification, (company.id, SCHEME))


@dataclass
class BackfillRun:
    stored: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    stopped: str | None = None
    names: list[str] = field(default_factory=list)


def missing(session: Session, limit: int) -> list[Company]:
    """Companies with an ISIN and no stored classification, NSE-listed first, in name order."""
    have = select(CompanyClassification.company_id).where(CompanyClassification.scheme == SCHEME)
    q = (
        select(Company)
        .join(Security, Security.company_id == Company.id)
        .where(Security.isin.is_not(None), Company.id.not_in(have))
        .group_by(Company.id)
        .order_by(func.bool_or(Security.security_code.is_(None)).desc(), Company.common_name)
        .limit(limit)
    )
    return list(session.scalars(q))


def backfill(session: Session, provider, companies: list[Company], on_company=None) -> BackfillRun:
    run = BackfillRun()
    streak = 0

    def skip(reason: str) -> None:
        run.skipped[reason] = run.skipped.get(reason, 0) + 1

    for company in companies:
        ref = company_ref(session, company)
        if not ref.isin:
            skip("no ISIN")
            continue
        try:
            res, body, _ = provider._get(ref.isin, "profile")
        except Exception as exc:  # noqa: BLE001 - one company must not stop the batch
            log.exception("classification failed for %s", company.common_name)
            skip(f"error {type(exc).__name__}")
            continue
        if not res.ok or not isinstance(body, dict):
            status = getattr(res.status, "value", str(res.status))
            skip(f"profile not available ({status})")
            streak = streak + 1 if status in ("RATE_LIMITED", "ACCESS_DENIED") else 0
            if streak >= BREAKER:
                run.stopped = (
                    f"{BREAKER} requests in a row were refused (rate limit / access); stopped to "
                    "protect the quota. Resume later - progress is saved."
                )
                break
            continue
        streak = 0
        text = ((body.get("data") or {}).get("company_profile")) or ""
        if not text or not identity_matches(text, company_names(session, company.id)):
            skip("profile does not name this company")
            continue
        try:
            if store(session, company, body) is None:
                skip("no usable sector in the profile")
                continue
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one company must not stop the batch
            session.rollback()
            log.exception("could not store the classification of %s", company.common_name)
            skip(f"error {type(exc).__name__}")
            continue
        run.stored += 1
        run.names.append(company.common_name)
        if on_company:
            on_company(company)
    return run


def summary(session: Session) -> dict:
    rows = session.execute(
        select(CompanyClassification.label, func.count())
        .where(CompanyClassification.scheme == SCHEME)
        .group_by(CompanyClassification.label)
        .order_by(func.count().desc())
    ).all()
    total = session.scalar(
        select(func.count(func.distinct(Security.company_id))).where(Security.isin.is_not(None))
    )
    return {
        "classified": sum(n for _, n in rows),
        "eligible_companies": total or 0,
        "distinct_labels": len(rows),
        "largest_groups": [{"label": label, "companies": n} for label, n in rows[:10]],
        "source": SOURCE,
        "provider": SOURCE_CODE,
    }
