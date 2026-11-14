# ruff: noqa: E501
"""Working capital, financial health, repayment capacity, returns and DuPont analysis.

Built only from validated evidence on one accounting basis; nothing is estimated. Output
(`analysis["health"]`):

* working_capital (non-financials): receivable, inventory and payable days, the operating cycle
  (inventory + receivable days) and the cash conversion cycle (operating cycle - payable days), by year,
  with the change in days and a rule-based reading;
* leverage_liquidity (non-financials): debt-to-equity, net debt (negative = net cash), net debt / EBITDA,
  interest cover, current and quick ratios, with readings;
* repayment: borrowings against operating cash flow (years of cash flow to repay), cash against
  short-term debt, and cash flow against borrowings;
* returns: ROE, ROCE, ROA by year;
* dupont: ROE = net margin x asset turnover x equity multiplier on average balances, with the product
  checked against the stored ROE, the factor that moved ROE most between the first and last year, and a
  five-step form (tax burden x interest burden x EBIT margin x turnover x multiplier) where EBIT is stored;
* peers: the company's latest figure against the peer-group median (>= 3 peers, same year and basis).

Banks get returns and the DuPont decomposition only: working-capital days, net debt and interest cover do
not describe a bank, and its leverage and liquidity are tested in the capital and asset-quality sections.
"""

from decimal import Decimal
from math import log

from app.research import analysis as an
from app.research import cashflow as cf
from app.research import profitability as pf
from app.research import readiness as rd
from app.research.sectors import AMOUNT, MULT, PCT, Metric

VERSION = "1.0"
DAYS = "days"
CYCLE_STEP = Decimal("5")  # a change within +/- 5 days is "stable"
INTEREST_STRETCHED, INTEREST_COMFORTABLE = Decimal("1.5"), Decimal("3")
CURRENT_TIGHT, CURRENT_COMFORTABLE = Decimal("1"), Decimal("1.5")
LEVERAGE_LOW, LEVERAGE_HIGH = Decimal("1"), Decimal("3")
REPAY_COMFORTABLE, REPAY_STRETCHED = Decimal("3"), Decimal("5")
PRODUCT_TOLERANCE = Decimal("0.05")  # percentage points between the DuPont product and stored ROE


def _m(key, label, name, kind) -> Metric:
    return Metric(key, label, (name,), kind, ptype="ANNUAL")


DAY_METRICS = (
    _m("receivable_days", "Receivable days", "WORKING_CAPITAL.receivable_days", DAYS),
    _m("inventory_days", "Inventory days", "WORKING_CAPITAL.inventory_days", DAYS),
    _m("payable_days", "Payable days", "WORKING_CAPITAL.payable_days", DAYS),
    _m("operating_cycle_days", "Working capital (operating) cycle", "WORKING_CAPITAL.operating_cycle_days", DAYS),
    _m("cash_conversion_cycle_days", "Cash conversion cycle", "WORKING_CAPITAL.cash_conversion_cycle_days", DAYS),
)  # fmt: skip
LEVERAGE_METRICS = (
    _m("debt_to_equity", "Debt to equity", "LEVERAGE.debt_to_equity", MULT),
    _m("net_debt_to_equity", "Net debt to equity", "LEVERAGE.net_debt_to_equity", MULT),
    _m("net_debt_to_ebitda", "Net debt / EBITDA", "LEVERAGE.net_debt_to_ebitda", MULT),
    _m("debt_to_ebitda", "Borrowings / EBITDA", "LEVERAGE.debt_to_ebitda", MULT),
    _m("interest_cover", "Interest cover (EBIT / finance costs)", "RATIO.interest_cover", MULT),
    _m("ebitda_interest_cover", "EBITDA / finance costs", "RATIO.ebitda_interest_cover", MULT),
    _m("current_ratio", "Current ratio", "LIQUIDITY.current_ratio", MULT),
    _m("quick_ratio", "Quick ratio", "LIQUIDITY.quick_ratio", MULT),
    _m("cash_to_short_term_debt", "Cash and liquid investments / current borrowings", "LIQUIDITY.cash_to_short_term_debt", MULT),
    _m("cfo_to_debt", "Operating cash flow / borrowings", "LIQUIDITY.cfo_to_debt", MULT),
)  # fmt: skip
RETURN_METRICS = (
    _m("roe", "Return on equity (average equity)", "RETURN.roe_average", PCT),
    _m("roe_closing", "Return on equity (closing equity)", "RETURN.roe_closing", PCT),
    _m("roce", "Return on capital employed", "RETURN.roce", PCT),
    _m("roa", "Return on assets (average assets)", "RETURN.roa_average", PCT),
    _m("roa_closing", "Return on assets (closing assets)", "RETURN.roa_closing", PCT),
)  # fmt: skip
NET_DEBT = Metric("net_debt", "Net debt", ("LEVERAGE.net_debt",), AMOUNT, ptype="ANNUAL")
BORROWINGS = Metric(
    "borrowings", "Borrowings", ("LEVERAGE.total_borrowings",), AMOUNT, ptype="ANNUAL"
)
BY_KEY = {m.key: m for m in (*DAY_METRICS, *LEVERAGE_METRICS, *RETURN_METRICS)}
PEER_COMPARED_NONBANK = ("cash_conversion_cycle_days", "receivable_days", "net_debt_to_equity", "interest_cover", "current_ratio", "roe", "roce")  # fmt: skip
PEER_COMPARED_BANK = ("roe", "roa")

REV = pf.REVENUE
TOT = pf.TOTAL_INCOME
ATTR = Metric("net_profit", "Net profit attributable to owners", ("INCOME.attributable_profit",), AMOUNT, ptype="ANNUAL")  # fmt: skip
PBT = Metric("pbt", "Profit before tax", ("INCOME.profit_before_tax",), AMOUNT, ptype="ANNUAL")
EBIT = pf.EBIT
ASSETS = Metric("assets", "Total assets", ("BALANCE_SHEET.total_assets",), AMOUNT, ptype="ANNUAL")
EQUITY = Metric("equity", "Owners' equity", ("BALANCE.owners_equity",), AMOUNT, ptype="ANNUAL")
CFO = cf.CFO

NAMES = frozenset(
    {
        n
        for m in (*DAY_METRICS, *LEVERAGE_METRICS, *RETURN_METRICS, NET_DEBT, BORROWINGS, ATTR, PBT, EBIT, ASSETS, EQUITY, REV, TOT, CFO)
        for n in m.names
    }
)  # fmt: skip


# ---------------------------------------------------------------- readings (rules, stated)


def read_interest_cover(v: Decimal) -> str:
    return (
        "COMFORTABLE"
        if v >= INTEREST_COMFORTABLE
        else "ADEQUATE"
        if v >= INTEREST_STRETCHED
        else "STRETCHED"
    )


def read_current_ratio(v: Decimal) -> str:
    return (
        "COMFORTABLE" if v >= CURRENT_COMFORTABLE else "ADEQUATE" if v >= CURRENT_TIGHT else "TIGHT"
    )


def read_net_debt_ebitda(v: Decimal) -> str:
    if v < 0:
        return "NET_CASH"
    return "LOW" if v < LEVERAGE_LOW else "MODERATE" if v < LEVERAGE_HIGH else "HIGH"


def read_repayment_years(v: Decimal) -> str:
    return (
        "COMFORTABLE"
        if v < REPAY_COMFORTABLE
        else "MODERATE"
        if v < REPAY_STRETCHED
        else "STRETCHED"
    )


def read_cycle_change(days: Decimal) -> str:
    return (
        "LENGTHENING" if days >= CYCLE_STEP else "SHORTENING" if days <= -CYCLE_STEP else "STABLE"
    )


READERS = {
    "interest_cover": (read_interest_cover, f"below {INTEREST_STRETCHED}x stretched; below {INTEREST_COMFORTABLE}x adequate; otherwise comfortable"),
    "current_ratio": (read_current_ratio, f"below {CURRENT_TIGHT}x tight; below {CURRENT_COMFORTABLE}x adequate; otherwise comfortable"),
    "net_debt_to_ebitda": (read_net_debt_ebitda, f"negative: net cash; below {LEVERAGE_LOW}x low; below {LEVERAGE_HIGH}x moderate; otherwise high"),
}  # fmt: skip


def _with_reading(m: dict, key: str) -> dict:
    if key in READERS and m.get("available"):
        fn, rule = READERS[key]
        for row in m["series"]:
            row["reading"] = fn(Decimal(row["value"]))
        m["rules"] = rule
    return m


# ---------------------------------------------------------------- sections


def working_capital(store, basis: str) -> dict:
    days = {m.key: cf._ratio(store, m, basis) for m in DAY_METRICS}
    ccc = days["cash_conversion_cycle_days"]
    if not any(d.get("available") for d in days.values()):
        return {
            "available": False,
            "days": days,
            "reason": "no receivable, inventory or payable balances are stored on this basis with the "
            "revenue and cost of goods they are measured against (a services company has no cost of "
            "goods, so only receivable days can be measured)",
        }
    out = {"available": True, "basis": basis, "days": days, "convention": "closing balances, 365-day year; cost of goods = revenue from operations - gross profit"}  # fmt: skip
    for key in ("receivable_days", "inventory_days", "payable_days", "cash_conversion_cycle_days"):
        d = days[key]
        if d.get("available") and len(d["series"]) >= 2:
            first, last = d["series"][0], d["series"][-1]
            change = (Decimal(last["value"]) - Decimal(first["value"])).quantize(Decimal("0.1"))
            d["change_days"] = an._s(change)
            d["from_fy"], d["to_fy"] = first["fy"], last["fy"]
            d["direction"] = read_cycle_change(change)
            d["change_evidence_ids"] = [first["evidence_id"], last["evidence_id"]]
    out["rules"] = (
        f"a change within {CYCLE_STEP} days over the window is stable; a longer cycle ties up more cash"
    )
    out["observations"] = []
    if ccc.get("direction"):
        word = {
            "LENGTHENING": "lengthened",
            "SHORTENING": "shortened",
            "STABLE": "was broadly unchanged",
        }[ccc["direction"]]
        tail = (
            f" by {abs(Decimal(ccc['change_days']))} days" if ccc["direction"] != "STABLE" else ""
        )
        out["observations"].append(
            {
                "key": "cash_conversion_cycle",
                "text": f"The cash conversion cycle {word}{tail} between {ccc['from_fy']} and {ccc['to_fy']} "
                f"({Decimal(ccc['series'][0]['value']).quantize(Decimal('1'))} to {Decimal(ccc['series'][-1]['value']).quantize(Decimal('1'))} days).",
                "kind": "INTERPRETATION",
                "rule": out["rules"],
                "evidence_ids": ccc["change_evidence_ids"],
            }
        )
    return out


def leverage_liquidity(store, basis: str) -> dict:
    ratios = {m.key: _with_reading(cf._ratio(store, m, basis), m.key) for m in LEVERAGE_METRICS}
    net_debt = cf._flow(store, NET_DEBT, basis)
    debt = cf._flow(store, BORROWINGS, basis)
    if net_debt.get("available"):
        for row in net_debt["series"]:
            row["position"] = "NET_CASH" if Decimal(row["value"]) < 0 else "NET_DEBT"
            row.pop("yoy_pct", None)
    if (
        not any(r.get("available") for r in ratios.values())
        and not net_debt.get("available")
        and not debt.get("available")
    ):
        return {"available": False, "reason": f"no borrowings, liquidity or coverage figures are stored on the {basis.lower()} basis"}  # fmt: skip
    obs = []
    nd = ratios["net_debt_to_ebitda"]
    if nd.get("available"):
        last = nd["series"][-1]
        obs.append(_obs("net_debt_to_ebitda", f"Net debt / EBITDA was {Decimal(last['value'])}x in {last['fy']} ({last['reading'].lower().replace('_', ' ')}).", nd["rules"], [last["evidence_id"]]))  # fmt: skip
    elif net_debt.get("available"):
        last = net_debt["series"][-1]
        word = "net cash" if last["position"] == "NET_CASH" else "net debt"
        obs.append(_obs("net_debt", f"The company held {word} of INR {abs(Decimal(last['value'])) / Decimal(10_000_000):,.0f} crore in {last['fy']}.", "net debt = borrowings - cash and liquid investments", [last["evidence_id"]]))  # fmt: skip
    for key, label in (
        ("interest_cover", "Interest cover"),
        ("current_ratio", "The current ratio"),
    ):
        r = ratios[key]
        if r.get("available"):
            last = r["series"][-1]
            obs.append(_obs(key, f"{label} was {Decimal(last['value'])}x in {last['fy']} ({last['reading'].lower()}).", r["rules"], [last["evidence_id"]]))  # fmt: skip
    return {
        "available": True,
        "basis": basis,
        "ratios": ratios,
        "net_debt": net_debt,
        "borrowings": debt,
        "observations": obs,
    }


def _obs(key, text, rule, ids) -> dict:
    return {"key": key, "text": text, "kind": "INTERPRETATION", "rule": rule, "evidence_ids": ids}


def repayment(store, basis: str, lev: dict) -> dict:
    """Can operating cash flow repay the borrowings, and does liquid cash cover what is due soon?"""
    cfo = pf._on_basis(store, CFO.names, basis)
    debt = pf._on_basis(store, BORROWINGS.names, basis)
    years = sorted(set(cfo) & set(debt))
    rows = []
    for end in years[-pf.MAX_YEARS :]:
        d, c = debt[end], cfo[end]
        if d.value <= 0:
            rows.append(
                {"fy": rd.fiscal_year(end), "status": "NO_BORROWINGS", "evidence_ids": [d.id]}
            )
        elif c.value <= 0:
            rows.append({"fy": rd.fiscal_year(end), "status": "NO_OPERATING_CASH", "years_to_repay": None, "evidence_ids": [d.id, c.id]})  # fmt: skip
        else:
            years_to = (d.value / c.value).quantize(Decimal("0.1"))
            rows.append({"fy": rd.fiscal_year(end), "status": "OK", "years_to_repay": an._s(years_to), "reading": read_repayment_years(years_to), "evidence_ids": [d.id, c.id]})  # fmt: skip
    short = (lev.get("ratios") or {}).get("cash_to_short_term_debt", {})
    if not rows and not short.get("available"):
        return {"available": False, "reason": "borrowings with operating cash flow, or cash with current borrowings, are not stored for any year on this basis"}  # fmt: skip
    out = {
        "available": True,
        "years": rows,
        "rules": f"years of operating cash flow needed to repay all borrowings: below {REPAY_COMFORTABLE} comfortable; below {REPAY_STRETCHED} moderate; otherwise stretched",
        "short_term_cover": short if short.get("available") else None,
        "observations": [],
    }
    last = next((r for r in reversed(rows) if r["status"] == "OK"), None)
    if rows and rows[-1]["status"] == "NO_BORROWINGS":
        out["observations"].append(_obs("repayment", f"The company reported no borrowings in {rows[-1]['fy']}.", "borrowings of zero", rows[-1]["evidence_ids"]))  # fmt: skip
    elif rows and rows[-1]["status"] == "NO_OPERATING_CASH":
        out["observations"].append(_obs("repayment", f"Operating cash flow was zero or negative in {rows[-1]['fy']}, so borrowings cannot be repaid from it.", out["rules"], rows[-1]["evidence_ids"]))  # fmt: skip
    elif last:
        out["observations"].append(_obs("repayment", f"Borrowings equal {Decimal(last['years_to_repay'])} years of {last['fy']} operating cash flow ({last['reading'].lower()}).", out["rules"], last["evidence_ids"]))  # fmt: skip
    if short.get("available"):
        s = short["series"][-1]
        out["observations"].append(_obs("short_term_cover", f"Cash and liquid investments covered current borrowings {Decimal(s['value'])}x in {s['fy']}.", "cash and current investments / current borrowings", [s["evidence_id"]]))  # fmt: skip
    return out


def returns(store, basis: str) -> dict:
    out = {m.key: cf._ratio(store, m, basis) for m in RETURN_METRICS}
    return {"available": any(r.get("available") for r in out.values()), "metrics": out}


# ---------------------------------------------------------------- DuPont


def _pair_average(recs: dict, end):
    """(average, evidence ids) of the balance at `end` and one year earlier, or None."""
    prev = next((d for d in recs if pf._consecutive(d, end)), None)
    if end not in recs or prev is None:
        return None
    return (recs[end].value + recs[prev].value) / 2, [recs[prev].id, recs[end].id]


def dupont(store, basis: str, bank: bool) -> dict:
    sales_metric = TOT if bank else REV
    attr = pf._on_basis(store, ATTR.names, basis)
    sales = pf._on_basis(store, sales_metric.names, basis)
    assets = pf._on_basis(store, ASSETS.names, basis)
    equity = pf._on_basis(store, EQUITY.names, basis)
    pbt = pf._on_basis(store, PBT.names, basis)
    ebit = pf._on_basis(store, EBIT.names, basis)
    years = sorted(set(attr) & set(sales))
    rows = []
    for end in years:
        a, e = _pair_average(assets, end), _pair_average(equity, end)
        if not a or not e or a[0] <= 0 or e[0] <= 0 or sales[end].value <= 0:
            continue
        net_margin = attr[end].value / sales[end].value * 100
        turnover = sales[end].value / a[0]
        multiplier = a[0] / e[0]
        roe = attr[end].value / e[0] * 100
        row = {
            "fy": rd.fiscal_year(end),
            "net_margin": an._s(net_margin.quantize(Decimal("0.01"))),
            "asset_turnover": an._s(turnover.quantize(Decimal("0.001"))),
            "equity_multiplier": an._s(multiplier.quantize(Decimal("0.001"))),
            "roe": an._s(roe.quantize(Decimal("0.01"))),
            "product_check": an._s(
                (net_margin / 100 * turnover * multiplier * 100 - roe).quantize(Decimal("0.0001"))
            ),
            "evidence_ids": [attr[end].id, sales[end].id, *a[1], *e[1]],
            "_f": (net_margin, turnover, multiplier, roe),
        }
        if (
            end in pbt
            and end in ebit
            and pbt[end].value > 0
            and ebit[end].value > 0
            and attr[end].value > 0
        ):
            tax_burden = attr[end].value / pbt[end].value
            interest_burden = pbt[end].value / ebit[end].value
            ebit_margin = ebit[end].value / sales[end].value * 100
            row["five_step"] = {
                "tax_burden": an._s(tax_burden.quantize(Decimal("0.001"))),
                "interest_burden": an._s(interest_burden.quantize(Decimal("0.001"))),
                "ebit_margin": an._s(ebit_margin.quantize(Decimal("0.01"))),
                "asset_turnover": row["asset_turnover"],
                "equity_multiplier": row["equity_multiplier"],
                "evidence_ids": [pbt[end].id, ebit[end].id],
            }
        rows.append(row)
    if not rows:
        return {
            "available": False,
            "reason": "the decomposition needs net profit, revenue, and total assets and equity for two consecutive year ends on this basis",
        }
    out = {
        "available": True,
        "basis": basis,
        "sales_label": "total income" if bank else "revenue from operations",
        "years": [{k: v for k, v in r.items() if k != "_f"} for r in rows],
        "convention": "ROE = net profit attributable / average equity = net margin x asset turnover x equity multiplier; averages of opening and closing balances",
        "driver": None,
    }
    if len(rows) >= 2:
        f0, f1 = rows[0]["_f"], rows[-1]["_f"]
        if all(x > 0 for x in (*f0, *f1)) and f0[3] != f1[3]:
            total = log(float(f1[3] / f0[3]))
            names = ("Net margin", "Asset turnover", "Equity multiplier")
            shares = [log(float(f1[i] / f0[i])) / total * 100 for i in range(3)]
            lead = max(range(3), key=lambda i: abs(shares[i]))
            out["driver"] = {
                "from_fy": rows[0]["fy"],
                "to_fy": rows[-1]["fy"],
                "roe_change_pp": an._s((f1[3] - f0[3]).quantize(Decimal("0.01"))),
                "shares_pct": {n: round(s) for n, s in zip(names, shares, strict=True)},
                "leading": names[lead],
                "method": "log decomposition: each factor's share of the proportional change in ROE",
                "evidence_ids": rows[0]["evidence_ids"] + rows[-1]["evidence_ids"],
            }
        elif f0[3] == f1[3]:
            out["driver"] = None
    return out


# ---------------------------------------------------------------- peers


def peers_block(own_store, peer_stores, keys, basis: str, own_ratios: dict) -> dict:
    out = {}
    for key in keys:
        block = pf.peer_margin(own_store, peer_stores, BY_KEY[key], basis, own_ratios[key])
        if block.get("available"):
            block["kind"] = BY_KEY[key].kind
        out[key] = block
    return out


def build(own_store, peer_stores, sector_code: str, basis: str) -> dict:
    bank = sector_code == "BANK"
    ret = returns(own_store, basis)
    dp = dupont(own_store, basis, bank)
    base = {
        "version": VERSION,
        "kind": "BANK" if bank else "NON_FINANCIAL",
        "basis": basis,
        "returns": ret,
        "dupont": dp,
        "benchmark": {
            "kind": "PEER_GROUP",
            "label": "peer group",
            "industry": None,
            "description": "the same-industry listed companies closest in size that have stored filings on the same accounting basis and fiscal years",
        },
    }
    own = {
        m.key: cf._ratio(own_store, m, basis)
        for m in (*DAY_METRICS, *LEVERAGE_METRICS, *RETURN_METRICS)
    }
    if bank:
        base["available"] = bool(ret["available"] or dp["available"])
        base["peers"] = peers_block(own_store, peer_stores, PEER_COMPARED_BANK, basis, own)
        base["note"] = (
            "working-capital days, net debt and interest cover do not describe a bank; its leverage, "
            "liquidity and asset quality are tested in the capital and asset-quality sections"
        )
        base["definitions"] = definitions(bank=True)
        return base
    wc = working_capital(own_store, basis)
    lev = leverage_liquidity(own_store, basis)
    rep = repayment(own_store, basis, lev)
    base.update(
        {
            "available": bool(
                wc["available"]
                or lev["available"]
                or rep["available"]
                or ret["available"]
                or dp["available"]
            ),
            "working_capital": wc,
            "leverage_liquidity": lev,
            "repayment": rep,
            "peers": peers_block(own_store, peer_stores, PEER_COMPARED_NONBANK, basis, own),
            "definitions": definitions(bank=False),
        }
    )
    return base


def definitions(*, bank: bool) -> list[str]:
    common = [
        "ROE = net profit attributable to owners / average owners' equity; ROA = the same profit / average total assets; averages use the opening and closing balance sheets.",
        "DuPont: ROE = net margin x asset turnover x equity multiplier, where turnover = sales / average assets and the multiplier = average assets / average equity; the product is checked against the stored ROE.",
        "The five-step form splits the net margin into tax burden (profit attributable / profit before tax), interest burden (profit before tax / EBIT) and EBIT margin, and is shown only where EBIT is stored.",
        "Readings are rules of thumb with stated thresholds; they describe the evidence and are not conclusions about the company.",
    ]
    if bank:
        return ["For a bank, sales are total income.", *common]
    return [
        "Receivable days = closing current trade receivables / revenue from operations x 365; inventory and payable days use cost of goods = revenue from operations - gross profit, as printed.",
        "Operating (working capital) cycle = inventory days + receivable days; cash conversion cycle = operating cycle - payable days.",
        "Net debt = borrowings - cash and cash equivalents - other bank balances - current investments (those printed); a negative figure is net cash.",
        "Interest cover = EBIT / finance costs; quick ratio = (current assets - inventories) / current liabilities; ROCE = EBIT / (total assets - current liabilities).",
        "Years to repay = borrowings / operating cash flow.",
        *common,
    ]
