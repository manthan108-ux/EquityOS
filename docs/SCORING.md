# Scoring: relative, absolute and hybrid (Phase 10)

Guiding rule: every company is evaluated fairly, but no company receives a score the available
evidence cannot justify. A missing value is never a zero, and a score that cannot be justified is
shown as "unavailable" with the reason.

## 1. Architecture audit (before the change)

* **Pillars (Phase 8):** growth, profitability, returns, balance sheet, valuation, momentum (six).
  The new request names five (growth, profitability, financial health, valuation, operational
  efficiency). The relative engine keeps its six untouched; the absolute engine uses the five.
* **Peers:** `sector/core.py::select_peers`. Until now a peer needed the same statement profile and
  the same *sector*; industry only ranked them. Now a peer must share an *industry* (banks, private
  and public, are one group).
* **Relative score:** peer percentile per metric -> pillar mean -> weighted overall with a 60%
  weight-coverage minimum and (new) a 3-peer minimum for the overall. Stored as evidence records
  and a `peer_sets` row. **Unchanged.**
* **Reusable:** every ratio in the calculation catalogue; the evidence resolution index (selected,
  unblocked values); `MetricIndex`.
* **Missing:** EBITDA and interest cover for companies without filings (Upstox has no D&A or finance
  costs), borrowings, capex, net debt, inventory/receivables, segment data, volumes, NPA/NIM/CAR.
* **Baseline:** 754 tests at the start of Phase 9 close-out, 760 at the start of this phase.

## 2. Three methodologies

| Methodology | When | What |
|---|---|---|
| RELATIVE | >= 3 *eligible* peers and the peer engine's own 60% coverage rule passes | The Phase 8 score, unchanged (read, never recomputed) |
| ABSOLUTE | 0-2 eligible peers (2 are shown as context only), or relative withheld | Metrics read against benchmarks |
| HYBRID | never, for now | Needs a justified calibration; refused with the reason |
| UNAVAILABLE | evidence cannot justify a score | Reason stored and shown |

An **eligible peer** shares the industry and statement profile, and has >= 4 comparable metrics
across >= 2 pillars at the company's own latest annual period. Being in the database is not enough.

Absolute and relative scores are different scales and are never ranked together (the compare API
refuses and says why).

### Absolute eligibility (independent of the 60% relative rule)

* >= 6 metrics scored;
* every *critical pillar* of the profile has >= 1 scored metric;
* **computable coverage >= 60%**: of the weight that has a data source at all, at least 60% was
  scored. (Metrics that no source can supply yet are listed and never scored, but they do not by
  themselves block a score; they lower the *profile coverage*, which sets the confidence.)
* newest financial period within 548 days;
* otherwise: UNAVAILABLE, with every failed rule in the reason.

Pillar weights are renormalised over pillars that have at least one scored metric; metrics inside a
pillar are weighted by their stated weights.

### Confidence

HIGH: profile coverage >= 75% and >= half of the scored weight read against empirical benchmarks.
MEDIUM: coverage >= 60% and >= a quarter empirical. Otherwise LOW.

## 3. Metric transformation (`scoring/transform.py`)

* **PERCENTILE** against the stored benchmark distribution (anchors min/p25/p50/p75/p90/max ->
  0/25/50/75/90/100, linear in between), flipped for lower-is-better. Used where a representative
  sample exists.
* **THRESHOLD** piecewise-linear curve for safety/risk measures (debt/equity, interest cover, cash
  conversion, cost-to-income...) and as a fallback for growth and returns.
* **RANGE** trapezoid for metrics with a sweet spot (current ratio, credit-deposit ratio).
* Scores are clamped to 0-100 (an outlier cannot score more than 100); a degenerate distribution is
  "not scorable"; P/E or P/B on negative earnings/book and debt/equity on negative equity are
  *not meaningful*, not poor. The original value is stored beside the score.

**Every threshold curve is a documented project CONVENTION, not a validated industry benchmark.**
Each carries its rationale (cost of capital, conventional credit-safety levels, nominal GDP
growth), is labelled CONVENTION in the API and the dashboard, and lowers the confidence.

## 4. Benchmark intelligence (`scoring/benchmarks.py`, tables `benchmark_*`)

* Observations: one per company and fiscal year (consolidated preferred), validated and unblocked,
  last 5 fiscal years, scope = the company's **industry** and its **sector**. Price-date metrics use
  the newest value per company.
* Values before a company's restructuring cut-off are excluded (see identity below).
* Outliers: modified z-score > 5 (median absolute deviation), only for samples >= 8; excluded from
  the statistics but stored with the reason.
* Statistics: n, min, max, mean, median, std, p25, p50, p75, p90.
* **Sufficiency:** no distribution unless >= 4 companies and >= 12 observations (MEDIUM); HIGH needs
  >= 8 companies and >= 30 observations. Below that: INSUFFICIENT, no statistics stored.
* Each distribution stores source, method, period, observation and company counts, exclusions,
  timestamp and confidence. A build is an immutable **version**; identical inputs return the
  existing version (reproducible), new data creates a new one, and old versions stay intact.
  Every score run records its benchmark version, configuration version and an input hash.
* Not yet: inflation adjustment and accounting-standard break handling (a documented limitation);
  with at most four fiscal years per company there is no business-cycle depth.

## 5. Profiles (`scoring/registry.py`, copied to `metric_definitions` / `scoring_configs`)

FMCG, AUTOMOBILES, CONGLOMERATE (Reliance), BANK, IT_SERVICES and GENERIC_NONFIN (energy and other
companies). Each metric records: identifier, display name, pillar, applicability, formula, unit,
direction, scoring kind, curve and its source class and note, weight, effective date, version, and,
for metrics with no source in the system, the reason (volume growth, inventory turnover, NPA ratios,
segment data, constant-currency growth, ...). Banks never receive industrial metrics (debt/equity,
EBITDA margin, interest cover). Reported IT growth is labelled as reported, not constant-currency.

**SOTP:** no segment data and no reliable segment valuation assumptions are stored, so
`VALUATION.sotp` and the segment metrics are listed as unavailable. Nothing is manufactured.

## 6. Hybrid

`HYBRID_ENABLED = False`. `calibration()` enables it only if, per profile group, >= 8 companies have
both an absolute and a relative score and their rank correlation is >= 0.6. With the current
sample that cannot be shown (a group has at most one company with both), so hybrid is refused with
the reason rather than produced by averaging unlike scales.

## 7. Company identity and restructuring (`seeds/identity.py`)

* **TMPV** is the original Tata Motors Ltd (same ISIN) renamed after the commercial-vehicle
  demerger (appointed date 1 Jul 2025, effective 1 Oct 2025). The CV company lists as **TMCV** (a
  different legal entity, deliberately not in the sample). Growth metrics whose comparison base is
  before 1 Jul 2025 and price history across the demerger are excluded for TMPV.
* **Nestle India** changed its financial year end from December to March; growth against the longer
  transition period is excluded.
* **Hyundai Motor India** listed 22 Oct 2024: short filing history.
* Identity (symbol, ISIN, legal name) is checked against the stored securities
  (`verify_identity`).

## 8. Database (migration `0012`, reversible)

`benchmark_versions`, `benchmark_observations`, `benchmark_stats`, `metric_definitions`,
`scoring_configs`, `score_runs`, `score_metric_results`.

## 9. API

`GET /api/companies/{id}/score?method=`, `.../score/metrics?pillar=`, `.../score/methodology`,
`.../score/history`, `GET /api/scores/compare?ids=`, `GET /api/scoring/definitions`,
`GET /api/benchmarks`, `GET /api/benchmarks/{id}/observations`; `POST /api/benchmarks/build` and
`POST /api/companies/{id}/score/recalculate` (disabled unless `SCORING_ADMIN_ENABLED=true`; there is
no authentication layer yet).

## 10. Commands

```
python -m app.scoring.cli seed
python -m app.scoring.cli benchmarks [--show]
python -m app.scoring.cli score --symbol M\&M [--method ABSOLUTE]
python -m app.scoring.cli score-all
python -m app.scoring.cli calibrate
```

## 11. Results on 2026-11-10 (24 companies)

* RELATIVE (unchanged from Phase 8): Axis 22.8, HDFC 48.8, ICICI 72.5, Kotak 40.7, SBI 43.3, TCS 60.2,
  Infosys 63.5, HCL 40.4, Wipro 43.8, Tech Mahindra 40.2; and, newly eligible with three same-industry
  peers, Tata Motors Passenger Vehicles 0.0 (LOW confidence, see limitations).
* ABSOLUTE: Mahindra & Mahindra 77.0 (LOW confidence: 37% of the intended profile, no valuation or
  efficiency evidence beyond asset turnover).
* UNAVAILABLE (reason stored): HUL, ITC, Maruti, Reliance, Nestle, Britannia, Godrej, Hyundai, TVS,
  ONGC, IOC, BPCL. The binding constraints are missing EBITDA/EBIT, borrowings, shares outstanding
  and capex for companies whose only source is Upstox, and too-small samples for valuation
  benchmarks.
