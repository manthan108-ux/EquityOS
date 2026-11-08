"""Validation rules over the resolved facts of ONE company. Pure functions, no database.

A rule never edits data. It returns findings: ERROR (the values involved must not be used in a
report), WARNING (usable but a reader should know) or INFO. Each finding names the evidence records
involved, so a person can open them, and the engine can block exactly those values.

The rules deliberately repeat arithmetic the extraction phase already checked per statement. They
run on the *resolved* values, after source authority and conflicts were applied, so they also catch
problems that only appear once several sources, documents or periods are put side by side.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.evidence.core import Fact, Key, Resolved
from app.models.evidence import ResolutionStatus, Severity
from app.normalization import month_end

RULES_VERSION = "6.0"
STALE_ANNUAL_DAYS = 550  # same thresholds as the coverage engine
STALE_QUARTER_DAYS = 200
JUMP_FACTOR = Decimal(5)


@dataclass
class Finding:
    rule: str
    severity: Severity
    message: str
    metric: str | None = None
    period_end: date | None = None
    evidence_ids: list[int] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    # Which of the evidence records may not be used because of this ERROR. None = all involved
    # (an identity failure cannot say which side is wrong); lineage findings block only the
    # calculation or statement that is broken, not the sound facts it was built from.
    blocks: list[int] | None = None

    def blocking_ids(self) -> list[int]:
        return self.evidence_ids if self.blocks is None else self.blocks


@dataclass
class Context:
    resolved: dict[Key, Resolved]
    facts: list[Fact]  # every candidate, including losers
    today: date
    # How many checks each rule actually performed / passed, so 'no findings' can be told apart
    # from 'nothing to check'.
    stats: Counter = field(default_factory=Counter)

    def selected(self) -> list[Fact]:
        return [r.selected for r in self.resolved.values() if r.selected is not None]

    def table(self) -> dict[tuple, Fact]:
        """Selected value by (metric, dimension, period_end, period_type, basis)."""
        return {f.key: f for f in self.selected()}


def _get(
    table: dict[tuple, Fact], metric: str, end: date, ptype: str, basis: str, dimension: str = ""
) -> Fact | None:
    return table.get((metric, dimension, end, ptype, basis))


def _tolerance(*facts: Fact) -> Decimal:
    return sum((f.rounding for f in facts), Decimal(0))


def _ids(*facts: Fact) -> list[int]:
    return [f.id for f in facts]


# ---- sources, provenance and documents ---------------------------------------------------------


def rule_sources(ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    for key, r in ctx.resolved.items():
        metric, dim, end, ptype, basis = key
        label = f"{metric}{f' [{dim}]' if dim else ''} {end} {ptype or 'point'} {basis}".strip()
        involved = [a["evidence_id"] for a in r.alternatives]
        if r.selected is not None:
            involved = [r.selected.id, *involved]
        if r.status is ResolutionStatus.CONFLICT:
            out.append(
                Finding(
                    "CROSS_SOURCE_CONFLICT",
                    Severity.ERROR,
                    f"{label}: {r.note}",
                    metric,
                    end,
                    involved,
                )
            )
        elif r.status is ResolutionStatus.RESOLVED_BY_AUTHORITY:
            out.append(
                Finding(
                    "SOURCE_DISAGREEMENT",
                    Severity.WARNING,
                    f"{label}: {r.note}",
                    metric,
                    end,
                    involved,
                )
            )
        elif r.status is ResolutionStatus.UNUSABLE:
            out.append(
                Finding(
                    "NO_USABLE_SOURCE", Severity.ERROR, f"{label}: {r.note}", metric, end, involved
                )
            )
    for f in ctx.facts:
        if f.problem:
            out.append(
                Finding(
                    "PROVENANCE",
                    Severity.ERROR,
                    f"{f.metric} {f.period_end}: {f.problem}",
                    f.metric,
                    f.period_end,
                    [f.id],
                )
            )
        elif f.provider == "DOCUMENT_EXTRACTION" and (f.document_id is None or f.page is None):
            out.append(
                Finding(
                    "PROVENANCE",
                    Severity.ERROR,
                    f"{f.metric} {f.period_end}: document figure without document or page",
                    f.metric,
                    f.period_end,
                    [f.id],
                )
            )
    return out


def rule_documents(ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    for f in ctx.selected():
        differ = int(f.details.get("other_documents_differ", 0))
        if differ:
            out.append(
                Finding(
                    "DOCUMENT_DISCREPANCY",
                    Severity.WARNING,
                    f"{f.metric} {f.period_end} ({f.basis}): {differ} other document(s) print a "
                    "different figure; the most recent filing is used (restatement or an error "
                    "in one document)",
                    f.metric,
                    f.period_end,
                    [f.id],
                    {"other_documents": f.details.get("other_documents", [])},
                )
            )
        if f.details.get("restated"):
            out.append(
                Finding(
                    "RESTATED",
                    Severity.INFO,
                    f"{f.metric} {f.period_end} ({f.basis}) was restated by a later filing; the "
                    "earlier value is kept in the fact's history",
                    f.metric,
                    f.period_end,
                    [f.id],
                )
            )
        if f.details.get("unit_unstated"):
            out.append(
                Finding(
                    "UNIT_UNSTATED",
                    Severity.WARNING,
                    f"{f.metric} {f.period_end}: the source does not state a unit",
                    f.metric,
                    f.period_end,
                    [f.id],
                )
            )
    return out


# ---- identities ----------------------------------------------------------------------------------


def _identity(
    rule: str,
    left: Fact,
    parts: list[Fact],
    label: str,
    severity: Severity = Severity.ERROR,
) -> Finding | None:
    total = sum((p.value for p in parts), Decimal(0))
    allowed = _tolerance(left, *parts)
    diff = abs(left.value - total)
    if diff <= allowed:
        return None
    return Finding(
        rule,
        severity,
        f"{label}: {left.metric.split('.')[1]} {left.value} but the parts add up to {total} "
        f"(difference {diff}, rounding allowance {allowed})",
        left.metric,
        left.period_end,
        _ids(left, *parts),
        {"difference": str(diff), "allowed": str(allowed)},
    )


def _identity_any(
    rule: str,
    left: Fact,
    variants: list[list[Fact]],
    label: str,
    severity: Severity = Severity.ERROR,
) -> Finding | None:
    """No finding if any way of adding the parts reaches `left`; else report the closest one."""
    findings = [_identity(rule, left, parts, label, severity) for parts in variants]
    if any(f is None for f in findings):
        return None
    return min(findings, key=lambda f: Decimal(f.details["difference"]))


def rule_identities(ctx: Context) -> list[Finding]:
    table = ctx.table()
    out: list[Finding] = []
    seen: set[tuple[date, str, str]] = set()

    def tally(rule: str, finding: Finding | None) -> None:
        ctx.stats[f"{rule}.checked"] += 1
        if finding is None:
            ctx.stats[f"{rule}.passed"] += 1
        else:
            out.append(finding)

    for f in ctx.selected():
        if f.dimension:
            continue
        group = (f.period_end, f.period_type, f.basis)
        if group in seen:
            continue
        seen.add(group)
        end, ptype, basis = group
        label = f"{end} {ptype or 'point-in-time'} {basis}"

        def get(metric: str, end=end, ptype=ptype, basis=basis) -> Fact | None:
            return _get(table, metric, end, ptype, basis)

        # Balance sheet: assets = equity + liabilities (the printed totals)
        assets = get("BALANCE_SHEET.total_assets")
        liabilities = get("BALANCE_SHEET.total_equity_and_liabilities") or get(
            "BALANCE_SHEET.total_capital_and_liabilities"
        )
        if assets and liabilities:
            diff = abs(assets.value - liabilities.value)
            allowed = _tolerance(assets, liabilities)
            tally(
                "BALANCE_SHEET_IDENTITY",
                None
                if diff <= allowed
                else Finding(
                    "BALANCE_SHEET_IDENTITY",
                    Severity.ERROR,
                    f"{label}: total assets {assets.value} differs from total equity and "
                    f"liabilities {liabilities.value} (difference {diff}, allowance {allowed})",
                    assets.metric,
                    end,
                    _ids(assets, liabilities),
                ),
            )
        # Income: total income = revenue (or interest earned) + other income
        total_income = get("INCOME.total_income")
        other = get("INCOME.other_income")
        revenue = get("INCOME.revenue_from_operations") or get("INCOME.interest_earned")
        if total_income and other and revenue:
            tally(
                "INCOME_TOTAL_IDENTITY",
                _identity(
                    "INCOME_TOTAL_IDENTITY",
                    total_income,
                    [revenue, other],
                    f"{label} total income",
                ),
            )
        # Cash flow
        net = get("CASH_FLOW.net_change_in_cash")
        ops = get("CASH_FLOW.net_cash_from_operating_activities")
        inv = get("CASH_FLOW.net_cash_from_investing_activities")
        fin = get("CASH_FLOW.net_cash_from_financing_activities")
        fx = get("CASH_FLOW.fx_effect_on_cash")
        # Statements print the exchange-rate effect either inside the activities or as its own
        # line, so a statement is sound if EITHER form adds up (the extraction phase does the same).
        if net and ops and inv and fin:
            finding = _identity_any(
                "CASH_FLOW_IDENTITY",
                net,
                [[ops, inv, fin], *([[ops, inv, fin, fx]] if fx else [])],
                f"{label} net change in cash",
            )
            tally("CASH_FLOW_IDENTITY", finding)
        opening = get("CASH_FLOW.cash_at_beginning")
        closing = get("CASH_FLOW.cash_at_end")
        if net and opening and closing:
            finding = _identity_any(
                "CASH_ROLL_FORWARD",
                closing,
                [[opening, net], *([[opening, net, fx]] if fx else [])],
                f"{label} closing cash",
                Severity.ERROR if fx else Severity.INFO,
            )
            if finding and not fx:
                finding.message += (
                    " (effect of exchange rates not stored, so this is not conclusive)"
                )
            tally("CASH_ROLL_FORWARD", finding)
    return out


# ---- consistency across periods ------------------------------------------------------------------

NOT_ADDITIVE = {"eps_basic", "eps_diluted", "paid_up_equity_share_capital"}


def _quarter_ends(fy_end: date) -> list[date]:
    year = fy_end.year
    return [month_end(year - 1, 6), month_end(year - 1, 9), month_end(year - 1, 12), fy_end]


def rule_period_sums(ctx: Context) -> list[Finding]:
    """Reported quarters must add up to the reported cumulative / annual figure."""
    table = ctx.table()
    out: list[Finding] = []
    for f in ctx.selected():
        if (
            f.statement != "INCOME"
            or f.dimension
            or f.item in NOT_ADDITIVE
            or f.cls != "FACT"
            or f.period_type not in ("ANNUAL", "HALF_YEAR", "NINE_MONTH")
        ):
            continue
        ends = _quarter_ends(
            f.period_end
            if f.period_end.month == 3
            else month_end(f.period_end.year + (1 if f.period_end.month > 3 else 0), 3)
        )
        wanted = {"ANNUAL": ends, "HALF_YEAR": ends[:2], "NINE_MONTH": ends[:3]}[f.period_type]
        expected_end = wanted[-1]
        if f.period_end != expected_end:
            continue
        parts = [_get(table, f.metric, e, "QUARTERLY", f.basis) for e in wanted]
        if any(p is None or p.cls != "FACT" for p in parts):
            continue
        total = sum((p.value for p in parts), Decimal(0))
        allowed = _tolerance(f, *parts)
        diff = abs(f.value - total)
        ctx.stats["QUARTERS_VS_CUMULATIVE.checked"] += 1
        if diff <= allowed:
            ctx.stats["QUARTERS_VS_CUMULATIVE.passed"] += 1
        if diff > allowed:
            out.append(
                Finding(
                    "QUARTERS_VS_CUMULATIVE",
                    Severity.WARNING,
                    f"{f.metric} {f.basis} {f.period_type.lower()} to {f.period_end}: reported "
                    f"{f.value} but the {len(parts)} reported quarters add up to {total} "
                    f"(difference {diff}, allowance {allowed}); a quarter may have been restated",
                    f.metric,
                    f.period_end,
                    _ids(f, *parts),
                    {"difference": str(diff), "allowed": str(allowed)},
                )
            )
    return out


# ---- plausibility and freshness ------------------------------------------------------------------

_MUST_BE_POSITIVE = {
    "BALANCE_SHEET.total_assets": Severity.ERROR,
    "BALANCE_SHEET.total_equity_and_liabilities": Severity.ERROR,
    "BALANCE_SHEET.total_capital_and_liabilities": Severity.ERROR,
    "BALANCE_SHEET.deposits": Severity.ERROR,
    "INCOME.total_income": Severity.WARNING,
    "INCOME.revenue_from_operations": Severity.WARNING,
    "INCOME.interest_earned": Severity.WARNING,
}
_JUMP_METRICS = ("INCOME.total_income", "BALANCE_SHEET.total_assets")


def rule_plausibility(ctx: Context) -> list[Finding]:
    table = ctx.table()
    out: list[Finding] = []
    for f in ctx.selected():
        severity = _MUST_BE_POSITIVE.get(f.metric)
        if severity and not f.dimension and f.value < 0:
            out.append(
                Finding(
                    "NEGATIVE_VALUE",
                    severity,
                    f"{f.metric} {f.period_end} ({f.basis}) is negative ({f.value}), which this "
                    "item should not be",
                    f.metric,
                    f.period_end,
                    [f.id],
                )
            )
        if f.metric == "INCOME.eps_basic" and not f.dimension:
            profit = _get(table, "INCOME.net_profit", f.period_end, f.period_type, f.basis)
            if (
                profit
                and profit.value != 0
                and f.value != 0
                and (profit.value < 0) != (f.value < 0)
            ):
                out.append(
                    Finding(
                        "EPS_SIGN",
                        Severity.WARNING,
                        f"{f.period_end} {f.basis}: EPS {f.value} and net profit {profit.value} "
                        "have opposite signs",
                        f.metric,
                        f.period_end,
                        _ids(f, profit),
                    )
                )
        if f.metric in _JUMP_METRICS and f.period_type == "ANNUAL" and not f.dimension:
            previous_end = month_end(f.period_end.year - 1, f.period_end.month)
            before = _get(table, f.metric, previous_end, "ANNUAL", f.basis)
            if before and before.value > 0 and f.value > 0:
                ratio = f.value / before.value
                ctx.stats["LARGE_YEAR_CHANGE.checked"] += 1
                if JUMP_FACTOR >= ratio >= 1 / JUMP_FACTOR:
                    ctx.stats["LARGE_YEAR_CHANGE.passed"] += 1
                if ratio > JUMP_FACTOR or ratio < 1 / JUMP_FACTOR:
                    out.append(
                        Finding(
                            "LARGE_YEAR_CHANGE",
                            Severity.WARNING,
                            f"{f.metric} ({f.basis}) changed {ratio:.2f}x between {previous_end} "
                            f"and {f.period_end}: check units, scope (demerger/merger) and "
                            "restatements before relying on growth rates",
                            f.metric,
                            f.period_end,
                            _ids(f, before),
                        )
                    )
    return out


def rule_freshness(ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    for ptype, limit in (("ANNUAL", STALE_ANNUAL_DAYS), ("QUARTERLY", STALE_QUARTER_DAYS)):
        for statement in ("INCOME", "BALANCE_SHEET"):
            dates = [
                f.period_end
                for f in ctx.selected()
                if f.statement == statement and f.period_type == ptype and f.period_end <= ctx.today
            ]
            if not dates:
                continue
            newest = max(dates)
            age = (ctx.today - newest).days
            if age > limit:
                out.append(
                    Finding(
                        "STALE_DATA",
                        Severity.WARNING,
                        f"newest {ptype.lower()} {statement.lower().replace('_', ' ')} is for "
                        f"{newest}, {age} days old (limit {limit})",
                        None,
                        newest,
                    )
                )
    return out


RULES = (
    rule_sources,
    rule_documents,
    rule_identities,
    rule_period_sums,
    rule_plausibility,
    rule_freshness,
)


def run_rules(ctx: Context) -> list[Finding]:
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(ctx))
    return findings


__all__ = [
    "Context",
    "Finding",
    "RULES_VERSION",
    "run_rules",
]
