"""Industry overview and growth comparison (CAGR, peer-set CAGR, growth alpha, relative price return).

Two kinds of content live here and are kept apart in the report:

* INDUSTRY CONTEXT: a qualitative sector template (how the business model earns money, structure, demand
  drivers, challenges, regulation). It is general background written for each sector profile. It contains no
  statistics, is not drawn from company filings, and is labelled as industry-level context, never as a
  fact about the company.
* GROWTH COMPARISON: arithmetic on stored evidence only. EquityOS stores no industry-wide statistics, so the
  "industry" benchmark is the company's stored PEER SET (same-industry listed companies closest in size).
  That is said wherever the benchmark appears. If fewer than three peers have the endpoints on the same basis
  the benchmark is reported as unavailable. Nothing is estimated.

"Growth alpha" here is the difference between the company's CAGR and the peer-set CAGR over the same fiscal
years (percentage points). It is a measure of relative operating growth, not investment alpha. The relative
price return is defined separately: price return (no dividends) over a stated period minus the median price
return of the same peer set; it is not risk-adjusted and not a market-index alpha.
"""

from __future__ import annotations

from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, CompanyClassification
from app.research import readiness as rd
from app.research.analysis import _records, _s
from app.research.sectors import AMOUNT, Metric

VERSION = "1.0"
MIN_PEERS = 3
MIN_YEARS = 2

CONTEXT_NOTE = (
    "Industry-level context. It is a general sector template written by EquityOS, not taken from the "
    "company's filings, and it contains no statistics. Company-specific facts are in the other sections."
)

TEMPLATES: dict[str, dict] = {
    "BANK": {
        "background": (
            "Banks take deposits and other borrowings and lend or invest them. They are the main channel "
            "through which savings reach households, businesses and the government, and they are among the "
            "most heavily regulated businesses."
        ),
        "how_it_earns": [
            "Net interest income: interest earned on loans and investments minus interest paid on deposits and borrowings. For most banks it is the largest source of profit.",
            "Fee and other income: payment, card, wealth, trade and transaction fees, treasury gains and bancassurance commissions.",
            "Costs and credit cost: staff and branch costs, technology, and provisions for loans that go bad. Profit is what remains.",
        ],
        "revenue_drivers": [
            "Loan growth and the mix of loans (retail, small business, corporate).",
            "Net interest margin: the gap between what loans earn and deposits cost.",
            "Low-cost deposits (current and savings accounts), which keep funding cost down.",
            "Asset quality: slippages and provisions decide how much of income is kept.",
        ],
        "structure": (
            "The market combines large public-sector banks, private-sector banks, small finance banks and "
            "non-bank lenders. Large banks compete on deposit franchise, branch and digital reach and cost of "
            "funds; mid-sized and private banks often compete on service, product breadth and speed."
        ),
        "demand": [
            "Economic growth and credit demand from households and firms.",
            "Rising financial savings and formalisation of the economy.",
            "Digital payments and credit distribution reaching new customers.",
        ],
        "opportunities": [
            "Retail and small-business lending, wealth and fee products, and digital channels.",
        ],
        "challenges": [
            "Credit cycles, where a downturn raises bad loans and provisions.",
            "Competition for low-cost deposits and pressure on net interest margin when rates fall.",
            "Technology, cyber-security and conduct risk.",
        ],
        "regulation": (
            "Regulated by the Reserve Bank of India: capital adequacy (Basel III), provisioning and "
            "asset-classification norms, liquidity ratios, priority-sector lending, and limits on ownership "
            "and on the products a bank may offer. Changes in these rules directly change returns and growth capacity."
        ),
        "sensitivity": [
            (
                "Interest rates",
                "High",
                "Policy-rate moves change both loan yields and deposit costs; the timing of the two decides margin.",
            ),
            (
                "Credit cycle",
                "High",
                "Slippages and provisions rise in downturns and fall in recoveries.",
            ),
            (
                "Economic growth and demand",
                "High",
                "Loan demand and repayment capacity follow the economy.",
            ),
            (
                "Inflation",
                "Medium",
                "Works mainly through the interest-rate path and household budgets.",
            ),
            ("Commodity prices", "Low", "Indirect, through borrowers in commodity-linked sectors."),
        ],
        "cyclical_prior": (
            "cyclical",
            "Bank earnings move with the credit cycle and the interest-rate cycle.",
        ),
        "leading_indicators": [
            "Credit growth versus deposit growth, and the credit-deposit ratio.",
            "Policy-rate direction and the repricing of loans and deposits.",
            "New slippages, gross and net NPA trend, and provisioning.",
            "Growth in low-cost deposits.",
            "System liquidity and corporate and household leverage.",
        ],
    },
    "IT_SERVICES": {
        "background": (
            "IT services firms build, run and modernise software and technology systems for other "
            "businesses. Most Indian firms sell mainly to clients in North America and Europe and deliver "
            "work from India and from client locations."
        ),
        "how_it_earns": [
            "Billing for people's time and skills (time-and-material) and for fixed-price projects and managed services.",
            "Long-term outsourcing contracts, which give recurring revenue.",
            "Consulting, cloud, data and AI work, and a smaller amount of product and platform revenue.",
        ],
        "revenue_drivers": [
            "Client technology budgets and the number of large, multi-year deals won.",
            "Headcount, utilisation and billing rates.",
            "Mix of work: digital and cloud work versus legacy support.",
            "Currency, since most revenue is earned in foreign currency.",
        ],
        "structure": (
            "A few very large firms with global delivery scale compete with mid-sized specialists and "
            "global consultancies. Scale, client relationships, talent supply and domain depth decide who wins large deals."
        ),
        "demand": [
            "Digital transformation, cloud migration, data and AI programmes.",
            "Cost take-out through automation and outsourcing.",
            "Regulatory and security spending.",
        ],
        "opportunities": [
            "Generative-AI services, cloud and data modernisation, and large vendor-consolidation deals."
        ],
        "challenges": [
            "Slower client spending in economic downturns.",
            "Wage inflation and attrition that raise costs.",
            "Pricing pressure and the risk that automation reduces billable effort.",
            "Visa and trade-policy changes in client countries.",
        ],
        "regulation": (
            "Not licensed like a bank, but exposed to data-protection, cross-border and immigration rules in "
            "client countries, and to Indian tax and special-economic-zone policy."
        ),
        "sensitivity": [
            (
                "Economic growth and demand",
                "High",
                "Client discretionary technology spending follows corporate confidence.",
            ),
            (
                "Currency",
                "High",
                "Revenue is mostly foreign currency while many costs are in rupees.",
            ),
            (
                "Interest rates",
                "Medium",
                "Higher rates cool client budgets in rate-sensitive sectors such as financial services.",
            ),
            ("Inflation", "Medium", "Works mainly through wage costs."),
            ("Commodity prices", "Low", "Indirect only."),
        ],
        "cyclical_prior": (
            "moderately cyclical",
            "Contracts give some visibility, but discretionary projects are cut when clients are cautious.",
        ),
        "leading_indicators": [
            "Large-deal wins and the order book.",
            "Client budget commentary in North America and Europe.",
            "Attrition, utilisation and headcount trend.",
            "Operating margin and pricing commentary.",
            "Rupee exchange rate.",
        ],
    },
    "FMCG": {
        "background": (
            "Fast-moving consumer goods companies sell everyday products such as food, beverages, home and "
            "personal care through large retail and distribution networks. Demand is steady because the products are bought often."
        ),
        "how_it_earns": [
            "Selling branded products to distributors, retailers and modern-trade and online channels.",
            "Margin is the gap between price and the cost of raw materials, packaging, manufacturing, distribution and marketing.",
        ],
        "revenue_drivers": [
            "Volume growth, price increases and the mix of premium products.",
            "Distribution reach, especially in rural and small-town markets.",
            "Brand strength, which supports pricing power.",
            "Input costs (agricultural commodities, packaging, energy).",
        ],
        "structure": (
            "A few large multinational-linked and Indian groups with national brands and distribution lead "
            "most categories, alongside regional brands and newer direct-to-consumer players. Distribution "
            "depth, brand equity and cost efficiency are the main advantages."
        ),
        "demand": [
            "Population, household income growth and urbanisation.",
            "Premiumisation and the shift from unbranded to branded products.",
            "Growth of organised retail and quick commerce.",
        ],
        "opportunities": ["Premium and adjacent categories, rural expansion and digital channels."],
        "challenges": [
            "Raw-material and packaging cost swings before price increases follow.",
            "Competition from regional and new brands.",
            "Weak rural or urban demand periods.",
        ],
        "regulation": (
            "Food-safety and labelling rules, pricing and indirect-tax changes, and restrictions on "
            "advertising and on some ingredients can change economics."
        ),
        "sensitivity": [
            (
                "Commodity prices",
                "High",
                "Input cost swings move gross margin until prices are adjusted.",
            ),
            ("Inflation", "High", "Raises costs and can reduce volumes if consumers trade down."),
            (
                "Economic growth and demand",
                "Medium",
                "Products are staples, so demand is steadier than for durables.",
            ),
            ("Interest rates", "Low", "Little direct effect; businesses carry low debt."),
        ],
        "cyclical_prior": (
            "defensive",
            "Demand for staples is relatively stable through the cycle; margins follow input costs.",
        ),
        "leading_indicators": [
            "Volume growth versus price growth.",
            "Commodity and packaging cost trend.",
            "Rural and urban demand commentary.",
            "Gross and operating margin trend.",
        ],
    },
    "AUTOMOBILES": {
        "background": (
            "Automobile makers design, build and sell cars, utility vehicles, two-wheelers and commercial "
            "vehicles, usually through dealer networks and with financing offered by banks and finance companies."
        ),
        "how_it_earns": [
            "Selling vehicles to dealers, who sell on to customers; revenue is volume multiplied by realisation per vehicle.",
            "Spare parts, accessories and services, and in some groups financing, exports and other businesses.",
        ],
        "revenue_drivers": [
            "Volumes, which depend on economic activity, interest rates, fuel prices and affordability.",
            "Product mix and pricing, with utility vehicles and premium models earning more per unit.",
            "Commodity and component costs, which set margin.",
            "Exports and new launches.",
        ],
        "structure": (
            "A small number of large manufacturers hold most of each segment, with intense competition on "
            "launches, features and price. Scale, supplier relationships and the dealer network matter; "
            "electric vehicles are bringing new entrants."
        ),
        "demand": [
            "Household income growth, financing availability and urbanisation.",
            "Replacement demand and rising vehicle ownership.",
            "Government incentives and infrastructure.",
        ],
        "opportunities": [
            "Electric vehicles, exports, premium models and connected-vehicle services."
        ],
        "challenges": [
            "Cyclical demand and inventory build-up at dealers.",
            "Steel, aluminium and semiconductor supply and cost swings.",
            "Heavy investment needed for electrification.",
            "Emission and safety rules that raise costs.",
        ],
        "regulation": (
            "Emission norms, safety standards, fuel and indirect-tax policy, and incentives for electric "
            "vehicles all directly change product cost and demand."
        ),
        "sensitivity": [
            (
                "Interest rates",
                "High",
                "Vehicle purchases are mostly financed, so rate changes move affordability.",
            ),
            ("Economic growth and demand", "High", "Volumes follow income and confidence."),
            ("Commodity prices", "High", "Steel, aluminium and other inputs set margin."),
            ("Inflation", "Medium", "Raises input costs and can weaken demand."),
            ("Currency", "Medium", "Affects imported components and export realisations."),
        ],
        "cyclical_prior": (
            "cyclical",
            "Vehicle demand moves with income, financing conditions and fuel prices.",
        ),
        "leading_indicators": [
            "Monthly domestic and export volumes and dealer inventory.",
            "Interest-rate direction and vehicle-finance availability.",
            "Commodity and component cost trend.",
            "Order books and launch pipeline.",
        ],
    },
    "ENERGY": {
        "background": (
            "Energy companies find and produce crude oil and gas, refine them into fuels and chemicals, "
            "and market them; some also run pipelines, retail outlets or power and new-energy businesses."
        ),
        "how_it_earns": [
            "Producers earn from selling oil and gas at market-linked or administered prices.",
            "Refiners and marketers earn the gap between the products they sell and the crude they buy (refining margin) and from retail margins.",
        ],
        "revenue_drivers": [
            "Crude oil and gas prices and exchange rates.",
            "Production volumes, refinery throughput and utilisation.",
            "Refining and marketing margins, which can be set partly by government pricing policy.",
            "Capital spending and project delivery.",
        ],
        "structure": (
            "State-owned companies dominate production, refining and fuel marketing, alongside large "
            "private groups and international firms. Scale, access to crude and government policy shape results."
        ),
        "demand": [
            "Economic growth and transport, industry and petrochemical demand.",
            "Population and rising energy use per person.",
        ],
        "opportunities": ["Petrochemicals, gas, renewable energy and fuel retail diversification."],
        "challenges": [
            "Volatile commodity prices and margins.",
            "Administered pricing and subsidy-sharing that can cap earnings.",
            "Large capital programmes and debt.",
            "The long-run shift away from fossil fuels.",
        ],
        "regulation": (
            "Government pricing and taxation of fuels, subsidy and windfall-tax decisions, licensing for "
            "exploration and refining, and environmental rules are central to profitability."
        ),
        "sensitivity": [
            ("Commodity prices", "High", "Crude and product prices drive revenue and margin."),
            (
                "Currency",
                "High",
                "Crude is bought in dollars; realisations and costs are in rupees.",
            ),
            (
                "Economic growth and demand",
                "Medium",
                "Fuel demand follows activity, though it is fairly price-inelastic.",
            ),
            ("Interest rates", "Medium", "Large capital programmes are debt-funded."),
            (
                "Inflation",
                "Medium",
                "Government may hold fuel prices, shifting the burden to the companies.",
            ),
        ],
        "cyclical_prior": (
            "highly cyclical",
            "Earnings follow commodity prices and refining margins, and policy can cap them.",
        ),
        "leading_indicators": [
            "Crude oil price and refining margin trend.",
            "Rupee exchange rate.",
            "Government pricing and subsidy-sharing announcements.",
            "Refinery utilisation and production volumes.",
        ],
    },
    "GENERIC_NONFIN": {
        "background": (
            "A non-financial company earns revenue by selling goods or services and keeps what is left "
            "after the costs of making and delivering them, financing and tax."
        ),
        "how_it_earns": [
            "Revenue from its products and services, less operating costs, financing costs and tax."
        ],
        "revenue_drivers": ["Demand, pricing power, cost control and capital intensity."],
        "structure": "Competitive structure depends on the industry; the peer set below shows comparable listed companies.",
        "demand": ["Economic growth and demand in the company's end markets."],
        "opportunities": ["Not assessed in this edition."],
        "challenges": ["Competition, input costs and leverage."],
        "regulation": "Industry-specific regulation is not summarised for this sector.",
        "sensitivity": [
            ("Economic growth and demand", "Medium", "Demand in end markets follows the economy."),
            ("Interest rates", "Medium", "Matters most where borrowing is large."),
            ("Commodity prices", "Medium", "Matters where inputs are commodity-linked."),
        ],
        "cyclical_prior": ("not classified", "No sector profile is defined for this company."),
        "leading_indicators": ["Revenue growth, margin trend and leverage."],
    },
}
TEMPLATES["CONGLOMERATE"] = {
    **TEMPLATES["GENERIC_NONFIN"],
    "background": (
        "A diversified group earns from several unrelated businesses at once (for example energy, "
        "consumer and technology); results are a blend of each business's own cycle."
    ),
    "structure": "Each business faces its own competitors; there is usually no single industry peer for the whole group.",
}
INDUSTRY_NOTES = {
    "private_sector_bank": "Private-sector banks usually grow faster than the system on retail lending and fees, and carry more market-driven funding costs than public-sector banks.",
    "public_sector_bank": "Public-sector banks have the largest deposit bases and branch networks and carry government ownership and policy-lending roles.",
}


def template(code: str) -> dict:
    return TEMPLATES.get(code, TEMPLATES["GENERIC_NONFIN"])


def overview(sector_code: str, industry_code: str | None, company: str, peers: list[str]) -> dict:
    t = template(sector_code)
    return {
        "note": CONTEXT_NOTE,
        "background": t["background"],
        "how_it_earns": t["how_it_earns"],
        "revenue_drivers": t["revenue_drivers"],
        "structure": t["structure"],
        "demand": t["demand"],
        "opportunities": t["opportunities"],
        "challenges": t["challenges"],
        "regulation": t["regulation"],
        "industry_note": INDUSTRY_NOTES.get(industry_code or ""),
        "fit": {
            "company": company,
            "peers": peers,
            "text": (
                f"{company} is analysed in EquityOS with the {sector_code.replace('_', ' ').lower()} "
                "sector profile. Its stored peer group (same-industry listed companies closest in size) "
                + (
                    f"is: {', '.join(peers)}."
                    if peers
                    else "is empty, so no listed peers are named."
                )
                + " Market shares are not stored, so no share or ranking by size within the whole industry is claimed."
            ),
        },
    }


# ---------------------------------------------------------------- growth comparison

REV = Metric(
    "revenue",
    "Revenue",
    ("INCOME.revenue_from_operations", "INCOME.total_income"),
    AMOUNT,
    ptype="ANNUAL",
)
PAT = Metric(
    "net_profit",
    "Net profit",
    ("INCOME.net_profit", "INCOME.attributable_profit", "INCOME.profit_after_tax"),
    AMOUNT,
    ptype="ANNUAL",
)
BANK_REV = Metric(
    "revenue",
    "Total income",
    ("INCOME.total_income", "INCOME.revenue_from_operations"),
    AMOUNT,
    ptype="ANNUAL",
)


def _cagr(first: Decimal, last: Decimal, years: int) -> Decimal | None:
    if first is None or last is None or first <= 0 or last <= 0 or years < 1:
        return None
    return ((last / first) ** (Decimal(1) / Decimal(years)) - 1) * 100


def _pow_ok(first, last) -> bool:
    return first is not None and last is not None and first > 0 and last > 0


def _endpoints(store, m: Metric, basis: str):
    recs = {d: r for d, r in _records(store, m.names, "ANNUAL", basis).items() if r.basis == basis}
    return recs


def _cagr_for(recs: dict, start, end):
    a, b = recs.get(start), recs.get(end)
    if a is None or b is None:
        return None
    years = round((end - start).days / 365.25)
    c = _cagr(a.value, b.value, years)
    if c is None:
        return None
    return {"cagr": c, "first": a, "last": b, "years": years}


def _common_span(company_recs: dict, peer_recs: list[dict], min_peers: int = MIN_PEERS):
    """The longest span (at least MIN_YEARS) whose end points the company and enough peers all have."""
    ends = sorted(company_recs)
    best = None
    for i, start in enumerate(ends):
        for end in ends[i + 1 :]:
            years = round((end - start).days / 365.25)
            if years < MIN_YEARS:
                continue
            ok = [
                r
                for r in peer_recs
                if start in r and end in r and _pow_ok(r[start].value, r[end].value)
            ]
            if len(ok) >= min_peers and (best is None or years > best[2]):
                best = (start, end, years)
    return best


def _metric_block(own_store, peer_stores, m: Metric, basis_order: list[str]):
    chosen = None
    for basis in basis_order:
        own = _endpoints(own_store, m, basis)
        if len(own) < 2:
            continue
        peers = [(c.common_name, _endpoints(s, m, basis)) for c, s in peer_stores]
        span = _common_span(own, [p for _, p in peers])
        if span:
            chosen = (basis, own, peers, span)
            break
    if chosen is None:
        own = _endpoints(own_store, m, basis_order[0])
        if len(own) < 2:
            for basis in basis_order[1:]:
                own = _endpoints(own_store, m, basis)
                if len(own) >= 2:
                    break
        ends = sorted(own)
        if len(ends) < 2:
            return {
                "available": False,
                "reason": "fewer than two annual values are stored for the company",
            }
        c = _cagr_for(own, ends[0], ends[-1])
        reason = (
            f"fewer than {MIN_PEERS} peers have the same fiscal-year end points on the same accounting basis"
            if peer_stores
            else "the company has no stored peer group"
        )
        out = {"available": True, "peer_benchmark": False, "peer_reason": reason}
        if c:
            out["company"] = _company_block(c, ends[0], ends[-1], own[ends[0]].basis)
        else:
            out["available"] = False
            out["reason"] = "the CAGR is undefined (a start or end value is zero or negative)"
        return out
    basis, own, peers, (start, end, years) = chosen
    c = _cagr_for(own, start, end)
    if c is None:
        return {
            "available": False,
            "reason": "the company CAGR is undefined (a value is zero or negative)",
        }
    rows, firsts, lasts = [], Decimal(0), Decimal(0)
    for name, recs in peers:
        pc = _cagr_for(recs, start, end) if start in recs and end in recs else None
        if pc:
            rows.append(
                {
                    "name": name,
                    "cagr": _s(pc["cagr"].quantize(Decimal("0.1"))),
                    "evidence_ids": [pc["first"].id, pc["last"].id],
                }
            )
            firsts += pc["first"].value
            lasts += pc["last"].value
    med = Decimal(str(median([Decimal(r["cagr"]) for r in rows])))
    agg = _cagr(firsts, lasts, years)
    return {
        "available": True,
        "peer_benchmark": True,
        "company": _company_block(c, start, end, basis),
        "peers": rows,
        "peer_median_cagr": _s(med.quantize(Decimal("0.1"))),
        "peer_aggregate_cagr": _s(agg.quantize(Decimal("0.1"))) if agg is not None else None,
        "difference_vs_median_pp": _s((c["cagr"] - med).quantize(Decimal("0.1"))),
        "difference_vs_aggregate_pp": _s((c["cagr"] - agg).quantize(Decimal("0.1")))
        if agg is not None
        else None,
        "peer_count": len(rows),
    }


def _company_block(c, start, end, basis) -> dict:
    return {
        "cagr": _s(c["cagr"].quantize(Decimal("0.1"))),
        "years": c["years"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_value": _s(c["first"].value),
        "end_value": _s(c["last"].value),
        "basis": basis,
        "unit": c["first"].unit,
        "evidence_ids": [c["first"].id, c["last"].id],
    }


def _price_return(own_store, key: str) -> dict | None:
    def latest(name):
        recs = [r for r in own_store.by_metric.get(name, []) if r.value is not None]
        return max(recs, key=lambda r: r.period_end or r.retrieved_at) if recs else None

    own, med = latest(f"TECH.{key}"), latest(f"PEERMEDIAN.TECH.{key}")
    if own is None or med is None:
        return None
    return {
        "period": key.replace("return_", "").upper(),
        "company": _s(own.value),
        "peer_median": _s(med.value),
        "difference_pp": _s((own.value - med.value).quantize(Decimal("0.1"))),
        "as_of": own.period_end.isoformat() if own.period_end else None,
        "evidence_ids": [own.id, med.id],
        "unit": own.unit,
    }


def growth(own_store, peer_stores, sector_code: str, prefer: str) -> dict:
    alt = "STANDALONE" if prefer == "CONSOLIDATED" else "CONSOLIDATED"
    order = [prefer, alt]
    rev_m = BANK_REV if sector_code == "BANK" else REV
    rev = _metric_block(own_store, peer_stores, rev_m, order)
    pat = _metric_block(own_store, peer_stores, PAT, order)
    price = [
        p
        for p in (_price_return(own_store, "return_1y"), _price_return(own_store, "return_6m"))
        if p
    ]
    return {
        "version": VERSION,
        "revenue_label": rev_m.label,
        "revenue": rev,
        "profit": pat,
        "price_return": price,
        "definitions": [
            "CAGR = (ending value / starting value) ^ (1 / years) - 1, using annual figures on one accounting basis between the same two fiscal year ends.",
            "Peer-group CAGR (not an industry CAGR): the median of the peers' own CAGRs, and the CAGR of the peers' combined values, over the same fiscal years. Peers without both end points on that basis are excluded.",
            "Difference vs the peer group = company CAGR minus peer-group CAGR, in percentage points. It measures relative operating growth only; it is not investment alpha, and no alpha against an industry benchmark is calculated because no verified industry statistics are held.",
            "Relative price return = the company's price return minus the median price return of the same peer set over the same period. Price return only (no dividends), not risk-adjusted, and not an index alpha.",
        ],
        "limitations": [
            "EquityOS holds no official industry statistics. The peer set (same-industry listed companies closest in size) is small and size-selected; the industry-group figures cover only the other companies the data provider files under the same sector label AND whose stored filings span the same years, so they describe those companies, not the whole industry.",
            "Only the fiscal years stored are used, so spans are short; one unusual year can move a CAGR a lot.",
            "A company with an acquisition, demerger or restatement in the span is not adjusted for it.",
        ],
    }


# ---------------------------------------------------------------- industry group (provider label)

MIN_GROUP = 5  # other companies in the group that must share the same fiscal-year end points
MAX_GROUP = 120  # members read per metric (name order); the rest are counted, not used
SCHEME = "UPSTOX_SECTOR"


# Labels the provider uses that do not describe a line of business: a DVR is a class of share, and
# "Miscellaneous" is a bucket. Companies under them are not comparable, so no group statistic is built.
NOT_AN_INDUSTRY = frozenset({"dvr", "miscellaneous", "others", "other", "na", "n/a"})


def industry_group(
    session: Session, company: Company, own_store, prefer: str, sector_code: str
) -> dict:
    """Industry-wide growth from the filings of every stored company sharing the provider's label.

    The group is "the other listed companies the data provider files under the same sector label",
    restricted to those whose stored annual figures cover the SAME two fiscal years on the SAME
    accounting basis as the company. The aggregate is the combined value of exactly those companies
    at both ends, so no company enters one year and not the other. Nothing is extrapolated to the
    members that lack data, and the coverage is always stated.
    """
    row = session.get(CompanyClassification, (company.id, SCHEME))
    if row is None:
        return {
            "available": False,
            "reason": "no industry label has been collected for this company yet",
        }
    if row.label.strip().lower() in NOT_AN_INDUSTRY:
        return {
            "available": False,
            "label": row.label,
            "reason": f"the provider's label '{row.label}' is not an industry (it is a share class "
            "or a catch-all), so it cannot define a group of comparable companies",
        }
    members = list(
        session.scalars(
            select(Company)
            .join(CompanyClassification, CompanyClassification.company_id == Company.id)
            .where(
                CompanyClassification.scheme == SCHEME,
                CompanyClassification.label == row.label,
                Company.id != company.id,
            )
            .order_by(Company.common_name)
        )
    )
    out = {
        "available": False,
        "label": row.label,
        "classification_source": row.retrieved_at.date().isoformat()
        + " - Upstox Fundamentals profile (sector field); the provider's label, not an exchange one",
        "group_companies": len(members) + 1,
        "group_market_cap_crore": _s(row.group_market_cap_crore)
        if row.group_market_cap_crore is not None
        else None,
        "metrics": {},
    }
    alt = "STANDALONE" if prefer == "CONSOLIDATED" else "CONSOLIDATED"
    rev_m = BANK_REV if sector_code == "BANK" else REV
    stores = [
        (member, rd.load_store(session, member.id, set(rev_m.names) | set(PAT.names)))
        for member in members[:MAX_GROUP]
    ]
    for metric in (rev_m, PAT):
        out["metrics"][metric.key] = _group_metric(
            own_store, stores, metric, [prefer, alt], len(members) + 1
        )
    out["available"] = any(b.get("available") for b in out["metrics"].values())
    if not out["available"]:
        out["reason"] = next(
            (b.get("reason") for b in out["metrics"].values() if b.get("reason")),
            "not enough stored data",
        )
    return out


def _group_metric(own_store, stores, metric: Metric, basis_order: list[str], group_size: int):
    for basis in basis_order:
        own = _endpoints(own_store, metric, basis)
        if len(own) < 2:
            continue
        peers = [(c.common_name, _endpoints(s, metric, basis)) for c, s in stores]
        span = _common_span(own, [p for _, p in peers], MIN_GROUP)
        if not span:
            continue
        start, end, years = span
        c = _cagr_for(own, start, end)
        if c is None:
            continue
        rows, firsts, lasts = [], Decimal(0), Decimal(0)
        for name, recs in peers:
            pc = _cagr_for(recs, start, end) if start in recs and end in recs else None
            if pc:
                rows.append(
                    {
                        "name": name,
                        "cagr": _s(pc["cagr"].quantize(Decimal("0.1"))),
                        "evidence_ids": [pc["first"].id, pc["last"].id],
                    }
                )
                firsts += pc["first"].value
                lasts += pc["last"].value
        agg = _cagr(firsts, lasts, years)
        med = Decimal(str(median([Decimal(r["cagr"]) for r in rows])))
        return {
            "available": True,
            "label": metric.label,
            "company": _company_block(c, start, end, basis),
            "members": rows,
            "members_used": len(rows),
            "group_size": group_size,
            "aggregate_cagr": _s(agg.quantize(Decimal("0.1"))) if agg is not None else None,
            "median_cagr": _s(med.quantize(Decimal("0.1"))),
            "alpha_vs_aggregate_pp": _s((c["cagr"] - agg).quantize(Decimal("0.1")))
            if agg is not None
            else None,
            "alpha_vs_median_pp": _s((c["cagr"] - med).quantize(Decimal("0.1"))),
        }
    return {
        "available": False,
        "reason": (
            f"fewer than {MIN_GROUP} other companies with the same label have stored annual "
            f"{metric.label.lower()} for the same fiscal years on the company's accounting basis "
            f"(the group has {group_size} companies; most have not been collected yet)"
        ),
    }


def build(
    session: Session,
    company: Company,
    own_store: rd._Store,
    peer_stores: list[tuple[Company, rd._Store]],
    sector_code: str,
    prefer: str,
    peer_names: list[str],
) -> dict:
    g = growth(own_store, peer_stores, sector_code, prefer)
    ig = g["industry_group"] = industry_group(session, company, own_store, prefer, sector_code)
    ov = overview(
        sector_code,
        company.industry.code if company.industry else None,
        company.common_name,
        peer_names,
    )
    if ig.get("label"):  # what the data says about the industry, next to the general context
        cap = ig.get("group_market_cap_crore")
        ov["fit"]["text"] += (
            f" The data provider files {company.common_name} under the sector label "
            f"'{ig['label']}', which covers {ig['group_companies']} listed companies"
            + (
                f" with a combined market capitalisation of {Decimal(cap):,.0f} crore"
                if cap
                else ""
            )
            + f" (read {ig['classification_source'].split(' - ')[0]}). Industry-wide growth "
            "from the filings of that group is in the growth section."
        )
    return {"version": VERSION, "overview": ov, "growth": g}
