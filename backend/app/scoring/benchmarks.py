"""Building the benchmark database from validated evidence.

For every metric some profile reads against a benchmark, and for every scope (an industry, and the
sector around it), the observations are the company-year values of the validated annual metrics of
the companies in that scope. Rules:

* one observation per company and fiscal year (consolidated preferred, standalone otherwise), so no
  company counts twice;
* only values the validation engine selected and did not block;
* values that fall before a company's restructuring cut-off (`seeds/identity.py`) are excluded;
* the last `YEARS` fiscal years only;
* statistical outliers (median-absolute-deviation rule) are kept in the table, marked excluded with
  the reason, and left out of the statistics;
* a scope with too few companies or observations gets NO distribution (confidence INSUFFICIENT):
  better no benchmark than one that looks authoritative and is not.

A build is a new, immutable `benchmark_versions` row, so any score can name the exact benchmark it
used and a later rebuild never changes it.
"""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BenchmarkObservation,
    BenchmarkStat,
    BenchmarkVersion,
    Company,
    EvidenceRecord,
    EvidenceResolution,
    EvidenceStatus,
)
from app.scoring import stats as st
from app.scoring.registry import benchmarked_metrics
from app.sector.store import comparable_from_for, starts_before

YEARS = 5
METHOD = (
    f"One observation per company and fiscal year (consolidated preferred); validated, unblocked "
    f"annual values only; last {YEARS} fiscal years; modified-z-score outliers (>{st.MAD_CUTOFF}) "
    f"excluded and recorded; percentiles by linear interpolation; no distribution below "
    f"{st.MIN_COMPANIES} companies and {st.MIN_OBSERVATIONS} observations."
)
SOURCE = (
    "Validated annual metrics calculated by the EquityOS calculation engine from filings and "
    "Upstox fundamentals already stored in the evidence store (sample of listed Indian companies)."
)


def _scopes(company: Company) -> list[tuple[str, str]]:
    out = []
    if company.industry:
        out.append(("INDUSTRY", company.industry.code))
    if company.sector:
        out.append(("SECTOR", company.sector.code))
    return out


def _company_annual_values(
    session: Session, company: Company, metrics: set[str], today: date
) -> list[tuple[str, EvidenceRecord, str | None]]:
    """(metric, record, exclusion reason) for the company's one-per-year annual values."""
    cutoff = comparable_from_for(session, company.id)
    rows = session.execute(
        select(EvidenceResolution.metric, EvidenceRecord)
        .join(EvidenceRecord, EvidenceRecord.id == EvidenceResolution.selected_evidence_id)
        .where(
            EvidenceResolution.company_id == company.id,
            EvidenceResolution.blocked.is_(False),
            EvidenceResolution.period_type.in_(("ANNUAL", "", "TTM")),
            EvidenceResolution.metric.in_(metrics),
            EvidenceRecord.status == EvidenceStatus.ACTIVE,
            EvidenceRecord.value.is_not(None),
        )
    ).all()
    best: dict[tuple[str, date], EvidenceRecord] = {}
    latest_point: dict[str, EvidenceRecord] = {}  # price-date metrics: only the newest counts
    for metric, record in rows:
        if not record.period_type or record.period_type == "TTM":
            held = latest_point.get(metric)
            if held is None or (record.period_end, record.basis == "CONSOLIDATED") > (
                held.period_end,
                held.basis == "CONSOLIDATED",
            ):
                latest_point[metric] = record
            continue
        key = (metric, record.period_end)
        current = best.get(key)
        if current is None or (record.basis == "CONSOLIDATED" and current.basis != "CONSOLIDATED"):
            best[key] = record
    horizon = date(today.year - YEARS, today.month, 1)
    out = [(m, r, None) for m, r in sorted(latest_point.items())]
    for (metric, period_end), record in sorted(best.items()):
        if period_end < horizon:
            continue
        reason = None
        if cutoff and starts_before(metric, period_end, cutoff):
            reason = "base period before the company's restructuring (not comparable)"
        out.append((metric, record, reason))
    return out


def build(
    session: Session, *, today: date | None = None, notes: str | None = None
) -> BenchmarkVersion:
    """Compute and store a new benchmark version from the current validated evidence."""
    today = today or datetime.now(UTC).date()
    metrics = benchmarked_metrics()
    gathered: dict[tuple[str, str, str], list[tuple[Company, EvidenceRecord, str | None]]] = {}
    companies = list(session.scalars(select(Company).order_by(Company.common_name)))
    for company in companies:
        if company.sector is None:
            continue
        values = _company_annual_values(session, company, metrics, today)
        if not values:
            continue
        for scope_type, scope_code in _scopes(company):
            for metric, record, reason in values:
                gathered.setdefault((scope_type, scope_code, metric), []).append(
                    (company, record, reason)
                )
    digest = hashlib.sha256(
        json.dumps(
            sorted(
                (k, str(c.id), r.id, str(r.value), reason)
                for k, rows in gathered.items()
                for c, r, reason in rows
            ),
            default=str,
        ).encode()
    ).hexdigest()
    same = session.scalar(select(BenchmarkVersion).where(BenchmarkVersion.input_hash == digest))
    if same is not None:
        return same  # identical inputs: the existing version is the reproducible answer
    count = len(list(session.scalars(select(BenchmarkVersion.id)))) + 1
    version = BenchmarkVersion(
        code=f"bench.{today.isoformat()}.{count}",
        input_hash=digest,
        methodology={
            "method": METHOD,
            "years": YEARS,
            "min_companies": st.MIN_COMPANIES,
            "min_observations": st.MIN_OBSERVATIONS,
            "outlier_rule": (
                f"modified z-score > {st.MAD_CUTOFF}, only for samples >= {st.MAD_MIN_SAMPLE}"
            ),
        },
        notes=notes,
    )
    session.add(version)
    session.flush()
    now = datetime.now(UTC)
    for (scope_type, scope_code, metric), rows in sorted(gathered.items()):
        eligible = [(c, r) for c, r, reason in rows if reason is None]
        values = [Decimal(r.value) for _, r in eligible]
        kept_idx, dropped_idx = st.split_outliers(values) if values else ([], [])
        drop = set(dropped_idx)
        position = 0
        for c, r, reason in rows:
            why = reason
            if reason is None:
                if position in drop:
                    why = "statistical outlier (modified z-score)"
                position += 1
            session.add(
                BenchmarkObservation(
                    version_id=version.id,
                    scope_type=scope_type,
                    scope_code=scope_code,
                    metric_code=metric,
                    company_id=c.id,
                    period_end=r.period_end,
                    basis=r.basis or "",
                    value=Decimal(r.value),
                    evidence_id=r.id,
                    included=why is None,
                    exclusion_reason=why,
                )
            )
        used = [(c, r) for i, (c, r) in enumerate(eligible) if i in set(kept_idx)]
        companies_used = {c.id for c, _ in used}
        confidence = st.classify(len(used), len(companies_used))
        stats = st.summarize([Decimal(r.value) for _, r in used]) if used else None
        if confidence == "INSUFFICIENT":
            stats = None
        session.add(
            BenchmarkStat(
                version_id=version.id,
                scope_type=scope_type,
                scope_code=scope_code,
                metric_code=metric,
                n_observations=len(used),
                n_companies=len(companies_used),
                n_excluded=len(rows) - len(used),
                period_from=min((r.period_end for _, r in used), default=None),
                period_to=max((r.period_end for _, r in used), default=None),
                stats=stats,
                confidence=confidence,
                method=METHOD,
                source=SOURCE,
                computed_at=now,
            )
        )
    session.flush()
    return version


def latest_version(session: Session) -> BenchmarkVersion | None:
    return session.scalar(select(BenchmarkVersion).order_by(BenchmarkVersion.id.desc()).limit(1))


def stat_for(
    session: Session, version_id: int, metric: str, industry: str | None, sector: str | None
) -> BenchmarkStat | None:
    """The most specific usable distribution: the industry's, then the sector's."""
    for scope_type, scope_code in (("INDUSTRY", industry), ("SECTOR", sector)):
        if not scope_code:
            continue
        found = session.scalar(
            select(BenchmarkStat).where(
                BenchmarkStat.version_id == version_id,
                BenchmarkStat.scope_type == scope_type,
                BenchmarkStat.scope_code == scope_code,
                BenchmarkStat.metric_code == metric,
            )
        )
        if found is not None and found.stats is not None:
            return found
    return None
