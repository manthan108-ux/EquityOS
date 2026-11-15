# ruff: noqa: E501
"""Activates the valuation workflow without inventing anything.

Two sources of valuation inputs, both entered through the assumption ledger (assumptions.set_input) so they
carry source, date, author and rationale, and can be edited (the old record is superseded):

* MARKET inputs (market_assumptions.json): the risk-free rate, the equity risk premium, an industry beta and the
  statutory tax rate (non-financials),
  each from a NAMED, DATED source. They are SOURCED_INPUT records. A beta is the Indian industry average of
  Damodaran's January 2026 table, used only where the industry has at least `min_firms` firms; otherwise the
  all-market beta (without financials) is used and the rationale says so.

* HISTORY RULES (company inputs): mechanical statistics over the company's OWN stored annual statements,
  never an analyst's view and never labelled as one. For a ratio observed in the last (up to) five years:
  base = mean of the last three, bear = the less favourable extreme of the five, bull = the more favourable.
  Revenue growth uses the year-on-year changes of stored revenue. They are entered as USER_ASSUMPTION records
  by the author "EquityOS history rule v1", with the evidence ids in the rationale. An input is produced only
  when at least three yearly observations exist; otherwise it is reported as not computable and stays unset.

Judgements that cannot be derived from history (long-run growth, shift in return on equity) are NOT produced
here: they are entered by the user (assumption_cli) and nothing is assumed in their place.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.research import analysis as an
from app.research import assumptions as asm
from app.research import profitability as pf
from app.research import readiness as rd
from app.research.sectors import AMOUNT, Metric

DATA = Path(__file__).with_name("market_assumptions.json")
AUTHOR_MARKET = "EquityOS market inputs"
AUTHOR_RULE = "EquityOS history rule v1"
Q = Decimal("0.01")
MIN_OBS = 3

DEP = Metric("dep", "Depreciation and amortisation", ("INCOME.depreciation_and_amortisation",), AMOUNT, ptype="ANNUAL")  # fmt: skip
CAPEX = Metric("capex", "Capital expenditure", ("CASH_FLOW.capital_expenditure",), AMOUNT, ptype="ANNUAL")  # fmt: skip
INVENTORY = Metric("inv", "Inventories", ("BALANCE_SHEET.inventories",), AMOUNT, ptype="ANNUAL")
RECEIVABLES = Metric("rec", "Trade receivables", ("BALANCE_SHEET.current_trade_receivables",), AMOUNT, ptype="ANNUAL")  # fmt: skip
PAYABLES = Metric("pay", "Trade payables", ("BALANCE_SHEET.current_trade_payables",), AMOUNT, ptype="ANNUAL")  # fmt: skip
PAY_MSME = Metric("paym", "Trade payables (MSME)", ("BALANCE_SHEET.trade_payables_msme",), AMOUNT, ptype="ANNUAL")  # fmt: skip
PAY_OTHER = Metric("payo", "Trade payables (others)", ("BALANCE_SHEET.trade_payables_others",), AMOUNT, ptype="ANNUAL")  # fmt: skip
EBIT = pf.EBIT
REVENUE = pf.REVENUE
ALL = (REVENUE, EBIT, DEP, CAPEX, INVENTORY, RECEIVABLES, PAYABLES, PAY_MSME, PAY_OTHER)


def load() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- market inputs


def beta_for(symbol: str, data: dict | None = None) -> tuple[Decimal, str, str]:
    """(beta, industry used, rationale) for a symbol under the minimum-sample rule."""
    b = (data or load())["beta"]
    wanted = b["mapping"].get(symbol)
    if wanted is None:
        raise asm.AssumptionError(f"{symbol} has no industry mapped for the beta; none is assumed")
    row = b["industries"][wanted]
    if row["firms"] >= b["min_firms"]:
        used, why = wanted, f"Indian industry average for {wanted} ({row['firms']} firms)."
    else:
        used = b["fallback"]
        row = b["industries"][used]
        why = (
            f"{wanted} has only {b['industries'][wanted]['firms']} Indian firms (fewer than {b['min_firms']}), so "
            f"the all-market average beta ({used}, {row['firms']} firms) is used instead."
        )
    return (
        Decimal(row["beta"]),
        used,
        why + " Industry average, not this company's own regression beta.",
    )


def apply_market(session: Session, company, symbol: str, *, today: date | None = None) -> list[str]:
    """Enter the dated market inputs for one company. Returns what was entered."""
    data, today, out = load(), today or date.today(), []
    for name in ("risk_free_rate", "equity_risk_premium"):
        s = data[name]
        asm.set_input(
            session, company.id, name, s["value"], author=AUTHOR_MARKET, rationale=s["rationale"],
            as_of=date.fromisoformat(s["as_of"]), source=s["source"], source_url=s["source_url"], today=today,
        )  # fmt: skip
        out.append(f"{name} {s['value']}")
    beta, industry, why = beta_for(symbol, data)
    b = data["beta"]
    asm.set_input(
        session, company.id, "beta", beta, author=AUTHOR_MARKET, rationale=why,
        as_of=date.fromisoformat(b["as_of"]), source=f"{b['source']}; industry: {industry}",
        source_url=b["source_url"], today=today,
    )  # fmt: skip
    out.append(f"beta {beta} ({industry})")
    if not industry.startswith(
        "Bank"
    ):  # a lender's profit is after interest; the free-cash-flow tax input does not apply
        t = data["tax_rate"]
        for case in asm.SCENARIOS:
            asm.set_input(
                session, company.id, "tax_rate", t["value"], author=AUTHOR_MARKET, rationale=t["rationale"],
                as_of=date.fromisoformat(t["as_of"]), scenario=case, source=t["source"], source_url=t["source_url"], today=today,
            )  # fmt: skip
        out.append(f"tax_rate {t['value']} (statutory)")
    return out


# ---------------------------------------------------------------- history rules


def _by_year(store, m: Metric, basis: str) -> dict[date, object]:
    return {
        d: r
        for d, r in an._records(store, m.names, "ANNUAL", basis).items()
        if r.basis in (basis, None)
    }


def _ratio(
    num: dict, den: dict, *, absolute: bool = False, pct: bool = True
) -> list[tuple[date, Decimal, list[int]]]:
    out = []
    for d in sorted(set(num) & set(den)):
        n, dn = num[d].value, den[d].value
        if dn in (None, 0) or n is None:
            continue
        v = (abs(n) if absolute else n) / dn
        out.append((d, v * 100 if pct else v, [num[d].id, den[d].id]))
    return out


def _quant(x: Decimal) -> Decimal:
    return x.quantize(Q, rounding=ROUND_HALF_UP)


def _scenarios(
    obs: list[tuple[date, Decimal, list[int]]], *, higher_is_better: bool
) -> dict | None:
    """base = mean of the last three; bear/bull = the less/more favourable extreme of the last five."""
    obs = obs[-5:]
    if len(obs) < MIN_OBS:
        return None
    vals = [v for _d, v, _i in obs]
    base = sum(vals[-3:]) / Decimal(3)
    lo, hi = min(vals), max(vals)
    bear, bull = (lo, hi) if higher_is_better else (hi, lo)
    ids = sorted({i for _d, _v, ids in obs for i in ids})
    years = f"{obs[0][0].year}-{obs[-1][0].year}"
    return {
        "bear": _quant(bear),
        "base": _quant(base),
        "bull": _quant(bull),
        "ids": ids,
        "years": years,
        "n": len(obs),
    }


def history_inputs(session: Session, company_id) -> tuple[dict[str, dict], dict[str, str]]:
    """({input name: scenarios}, {input name: why it could not be computed}) from the stored annual statements."""
    store = rd._Store(session, company_id, {n for m in ALL for n in m.names})
    basis = an.resolve_basis(store)["used"]
    rev, ebit, dep, capex = (_by_year(store, m, basis) for m in (REVENUE, EBIT, DEP, CAPEX))
    inv, rec, pay, paym, payo = (
        _by_year(store, m, basis) for m in (INVENTORY, RECEIVABLES, PAYABLES, PAY_MSME, PAY_OTHER)
    )
    found: dict[str, dict] = {}
    why: dict[str, str] = {}

    growth = []
    ys = sorted(rev)
    for a, b in zip(ys, ys[1:], strict=False):
        if rev[a].value and rev[a].value > 0 and (b - a).days < 400:
            growth.append((b, (rev[b].value / rev[a].value - 1) * 100, [rev[a].id, rev[b].id]))
    spec = {
        "revenue_growth": (growth, True, "year-on-year change in stored annual revenue"),
        "operating_margin": (_ratio(ebit, rev), True, "stored EBIT / revenue"),
        "capex_to_revenue": (
            _ratio(capex, rev, absolute=True),
            False,
            "stored capital expenditure / revenue",
        ),
        "depreciation_to_revenue": (
            _ratio(dep, rev, absolute=True),
            False,
            "stored depreciation and amortisation / revenue",
        ),
    }
    # operating working capital = inventories + trade receivables - trade payables (payables as one line, or the MSME and other lines)
    wc_obs = []
    for d in sorted(set(inv) & set(rec) & set(rev)):
        p = pay.get(d)
        pv, pids = (p.value, [p.id]) if p is not None else (None, [])
        if p is None and d in paym and d in payo:
            pv, pids = paym[d].value + payo[d].value, [paym[d].id, payo[d].id]
        if pv is None or not rev[d].value:
            continue
        wc_obs.append(
            (
                d,
                (inv[d].value + rec[d].value - pv) / rev[d].value * 100,
                [inv[d].id, rec[d].id, rev[d].id, *pids],
            )
        )
    spec["working_capital_to_revenue"] = (
        wc_obs,
        False,
        "stored inventories + trade receivables - trade payables, over revenue",
    )
    for name, (obs, better, what) in spec.items():
        sc = _scenarios(obs, higher_is_better=better)
        if sc is None:
            why[name] = f"fewer than {MIN_OBS} yearly observations of {what} are stored"
        else:
            sc["what"] = what
            found[name] = sc
    return found, why


def apply_history(
    session: Session, company, *, today: date | None = None
) -> tuple[list[str], dict[str, str]]:
    today = today or date.today()
    found, why = history_inputs(session, company.id)
    done = []
    for name, sc in found.items():
        for case in asm.SCENARIOS:
            val = sc[case]
            spec = asm.SPECS[name]
            if not (spec.low <= val <= spec.high):
                why[name] = (
                    f"the computed {case} value {val} is outside the plausible range {spec.low} to {spec.high}"
                )
                break
            rationale = (
                f"Rule-derived, not an analyst view: {sc['what']} over {sc['n']} stored years ({sc['years']}); "
                f"base = mean of the last three, bear/bull = the less/more favourable extreme. "
                f"Evidence ids {', '.join(map(str, sc['ids'][:12]))}{'...' if len(sc['ids']) > 12 else ''}."
            )
            latest_end = max(d for d in _years(session, company.id))
            asm.set_input(
                session, company.id, name, val, author=AUTHOR_RULE, rationale=rationale,
                as_of=min(latest_end, today), scenario=case, today=today,
            )  # fmt: skip
        else:
            done.append(name)
    return done, why


def _years(session: Session, company_id) -> list[date]:
    store = rd._Store(session, company_id, set(REVENUE.names))
    basis = an.resolve_basis(store)["used"]
    return list(_by_year(store, REVENUE, basis)) or [date.today()]
