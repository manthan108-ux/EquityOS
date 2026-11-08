# Financial Calculation Engine (Phase 7)

Turns validated evidence (Phase 6) and official prices into ratios, growth rates, trailing figures,
valuation multiples and technical indicators. Every output is CALCULATION evidence in the evidence
store: it has a formula, named inputs, a quality grade and is recomputed on every validation run.
No LLM is involved.

Code: `backend/app/calc/`. Tables: migration 0009. Read-only API under `/api/companies/{id}/`.
Commands: `python -m app.calc.cli` (from `./backend`).

## 1. What a run does

```
validate evidence -> rebuild price adjustments -> fundamentals -> prices (technicals, valuation)
-> record the run and every GAP -> validate again (recomputes everything just calculated)
```

* Inputs come only from `evidence_resolutions` (the one value per metric/period/basis that passed
  source authority and validation). Blocked, conflicting or missing inputs make a GAP, never a guess.
* Results go through the evidence ledger: exact decimals, units tracked, **one basis and one period
  type per calculation** (consolidated and standalone are calculated separately).
* A re-run changes nothing when inputs are unchanged (same records, same ids).
* `calculation_gaps` keeps, for each run, every metric that could not be calculated and the missing
  input, the precondition that failed, or the validation block that caused it.

## 2. Fundamental metrics (`catalogue.py`)

Declared as data: each metric is a formula over named inputs, with variants tried in order; the
variant used is recorded in `details.definition`, and caveats in `details.notes`.

| Group | Metrics (periods) |
|---|---|
| Building blocks | `BALANCE.owners_equity` (share capital + other equity; capital + reserves; or total equity less non-controlling interests), `INCOME.attributable_profit` (profit to owners; falls back to net profit with a note), `INCOME.net_interest_income` (banks) |
| Growth | total income, revenue (non-financial), net interest income (banks), net profit, PBT, EPS, advances, deposits year on year (annual, quarterly, half-year, nine-month); quarter on quarter; 3-year CAGR of income and profit. Growth from a loss or zero base is refused |
| Margins | net margin and PBT margin (non-financial), net profit / income (banks), other-income share, effective tax rate; banks: cost-to-income, pre-provision margin, provisions / pre-provision profit, advances / deposits |
| Returns (annual) | ROE on closing and on average equity; ROA on closing and on average assets |
| Structure | equity multiplier; debt / equity and current ratio (non-financial); free cash flow and operating cash flow / profit (non-financial; free cash flow requires capex to be stored as an outflow) |
| Trailing twelve months | total income, revenue, net profit, EPS by **two methods**: the last four quarters, and last full year + year-to-date - prior year-to-date. Both are kept; if they disagree beyond tolerance that is a conflict and neither is used |

Profiles: **BANK** (statements with interest earned/expended) and **NONFIN**. Metrics that do not
fit a profile are not attempted. Consolidated bank ratios carry a caveat: subsidiaries' businesses
(for example insurance premiums and claims) are inside "other income" and "operating expenses", which
is why ICICI Bank's consolidated cost-to-income (62.9%) differs from its standalone one (39.8%).

**Deliberately absent:** EBITDA, EBIT, operating margin, interest cover and ROCE. They need
depreciation and finance costs, which the document pipeline does not extract yet. A test enforces
that none of them is in the catalogue.

## 3. Prices, adjustment and technical indicators

**Prices** are the exchanges' official end-of-day values and are unadjusted. `adjust.py` builds
split/bonus/consolidation events (table `price_adjustments`, rebuilt every run):

* **Stored corporate actions** (`corporate_actions`): a bonus "a:b" gives factor (a+b)/b; a split is
  accepted only in words ("from Rs 10 to Re 1"), since a bare "10:1" is ambiguous. Rows that cannot
  be read are listed in the run (`skipped_corporate_actions`).
* **Implied events**: the bhavcopy `prev_close` is the exchange's own reference price. If
  `prior stored close / prev_close` matches a standard ratio within 0.5% it is an event
  (SINGLE_EXCHANGE, or CONFIRMED when the other exchange shows the same). Any other difference
  above 1% is stored as UNEXPLAINED and **not applied**: on 2026-02-02 all five test stocks show
  one, because the Sunday Budget session is not in our data.
* A stored action on the same date overrides the implied event, which is kept as a cross-check
  (agrees / DISAGREES). Cash dividends and rights issues are not adjusted.

**Indicators** (`technical.py`, exact decimals; formulas in the module docstring): SMA 20/50/200,
EMA 12/26, RSI 14 (Wilder), MACD with signal and histogram, Bollinger 20, ATR 14, returns 1m/3m/6m/1y,
volatility 60d/126d, 52-week high/low, distance from 52-week high, 1-year maximum drawdown, 20-day
average volume (adjusted) and delivery %. Not enough history gives a recorded GAP with the reason
(for example "SMA(200) needs 200 prices; 60 stored"), never a shorter window. Each stored indicator
keeps the method, parameters, security, date, number of bars and a **digest of the exact prices and
adjustments** used; validation recomputes it from the database, so changed prices are detected.

## 4. Valuation (`market.py`)

| Metric | Formula | Needs |
|---|---|---|
| `VALUATION.market_cap` | latest close x shares outstanding | `SHAREHOLDING.total.shares` |
| `VALUATION.price_to_book` | market cap / owners' equity (latest) | the two above |
| `VALUATION.pe_ttm` | close / trailing-twelve-month basic EPS | TTM EPS (> 0) |
| `VALUATION.pe_annual` | close / latest annual basic EPS | EPS (> 0) |

Statement values and the price are from different dates, so each result states both dates. If a
split or bonus took effect between a statement date and the price date, the statement value is
brought to today's share structure through an explicit `ADJUSTMENT.cumulative_factor` evidence
record (formula `price / (eps / factor)`), never silently. A loss gives no P/E.

## 5. Quality

A calculation is graded like any evidence (never HIGH) and is **no better than its weakest input**:
an input from a secondary source makes the result LOW, with the reason shown.

## 6. Commands and API

```
python -m app.calc.cli run --symbol ICICIBANK      # validate, adjust, calculate, record gaps
python -m app.calc.cli run-all
python -m app.calc.cli metrics --symbol ICICIBANK --prefix RETURN --basis CONSOLIDATED
python -m app.calc.cli gaps --symbol RELIANCE --metric LEVERAGE
python -m app.calc.cli technicals --symbol ICICIBANK
python -m app.calc.cli adjustments --symbol ICICIBANK
```

`GET /api/companies/{id}/metrics` (filters: prefix, basis, period_type) returns value, unit, formula,
named inputs, definition, notes, quality and whether blocked; also `/metrics/gaps`, `/technicals`,
`/price-adjustments`, `/calculation-run`.

## 7. Real-data results (2026-11-08)

| Company | Profile | Calculated | Gaps | Validation after |
|---|---|---|---|---|
| ICICI Bank | BANK | 220 (fundamentals, 25 indicators, 4 valuation multiples, TTM) | 69 | 0 errors, 0 warnings |
| Reliance Industries | NONFIN | 62 | 73 | 0 errors, 0 warnings |

ICICI Bank, consolidated, FY2026: ROE 15.04% (closing) / 16.13% (average), ROA 1.86%, net profit
growth 6.23%, net interest income growth 9.13%, advances / deposits 89.9%; standalone cost-to-income
39.75%. Market cap about Rs 11.25 lakh crore, P/B 2.90x, P/E 20.7x (annual EPS) and 19.05x
(trailing: Rs 82.46). The bank income identity (NII + other income - operating expenses = pre-provision
profit) holds exactly on both bases. Reliance FY2026: ROE 8.93%, net margin 7.51%, current ratio 1.10x.
Typical gaps are honest ones: prior years not stored (growth, CAGR, averages), no borrowings or
cash-flow statement for Reliance, no share count for Reliance (no market cap, P/B). All 25 technical
indicators could be calculated from the 247 stored sessions (the 1-year return and 200-day average
are close to the limit; a shorter history would produce recorded gaps, not shorter windows).
