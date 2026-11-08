# Sector and Peer Engine (Phase 8)

Answers: *who is this company compared with, what matters most in its sector, and where does it
stand?* No LLM; every number is CALCULATION evidence linked to the evidence it came from, recomputed
by validation. Code: `backend/app/sector/`. Tables: migration 0010. Commands:
`python -m app.sector.cli`. Read-only API under `/api/companies/{id}/` and `/api/sector-weights`.

## 1. What the numbers mean (read this first)

* A **rank** is the share of peers the company beats on one metric (a tie counts half). It is a
  *relative standing among the chosen peers*, not a measure of quality and not a forecast. With 3
  peers it can only be 0, 16.7, 33.3 ... 100; with 2 peers, 0, 25, 50, 75 or 100. Such groups are
  marked **thin** and every result built on them is graded LOW.
* A **pillar score** is the plain average of the ranks of that pillar's metrics that could be
  compared. The **overall score** is the weighted average of pillar scores using the sector's curated
  weights, renormalised over the pillars that have data.
* **No overall score is published unless the pillars with data carry at least 60% of the sector's
  weight.** Below that the pillar scores are shown and the overall is withheld with the reason. A
  number summarising two of six pillars would mislead.
* VALUATION is scored as *cheaper than peers*, which is not the same as better.

## 2. Sector profiles and weights (`profiles.py`)

Six scored pillars: GROWTH, PROFITABILITY, RETURNS, BALANCE_SHEET, VALUATION, MOMENTUM. Each sector
has a weight set summing to 100 with a written reason for every weight:

| Sector | Growth | Profit | Returns | Balance sheet | Valuation | Momentum |
|---|---|---|---|---|---|---|
| banking | 15 | 20 | 25 | 15 | 15 | 10 |
| it_services | 20 | 25 | 20 | 5 | 20 | 10 |
| fmcg | 15 | 25 | 25 | 10 | 15 | 10 |
| automobiles | 20 | 20 | 15 | 15 | 20 | 10 |
| oil_and_gas | 10 | 20 | 20 | 25 | 15 | 10 |
| any other sector | 15 | 20 | 20 | 15 | 20 | 10 (generic, separate for banks) |

**These weights are the project's judgement.** They are not derived from data, not predictions and
not produced by an LLM (the specification forbids invented sector weights). They are stored in
`sector_weight_sets` with their rationale and a version; changing one means a new version, and old
versions stay so past scores remain explainable. A sector's weights are used only if its statement
profile fits (a bank in a non-bank sector, or vice versa, gets the generic set). Run
`python -m app.sector.cli weights` to read every reason.

Scored metrics come from the calculation engine. Banks: net interest income, profit, advances and
deposits growth; pre-provision margin; cost-to-income; ROE/ROA; equity multiplier; provisions /
pre-provision profit; P/B and P/E; momentum. Non-financials: revenue, profit and EPS growth; net
and pre-tax margin; ROE/ROA; debt/equity, current ratio, equity multiplier; P/E and P/B; momentum.
Growth, margins and balance-sheet ratios may be compared on a common quarter when too few peers
share a year; returns are annual only (nothing here annualises a quarter).

## 3. Peers (`core.select_peers`)

A peer must have the **same statement profile** (a bank is never compared with a manufacturer) and
the **same sector**. Same-industry peers come first, then same-sector ones; within a tier the
**closest in size** (annual total income, log distance); at most 8. Every candidate left out is
recorded with its reason (different profile, different sector, no sector, beyond the 8 closest). A
company with no sector assigned has no peers.

Comparisons use **one basis for the whole company** (the basis with the most scorable metrics,
consolidated on ties) and, per metric, the period that the most peers can be compared at: the
company's newest period with at least 3 comparable peers, else the period with the most. Peers lacking
the same period and basis are excluded for that metric, and a metric no peer can be compared on is a
recorded gap.

## 4. Evidence and checking

For each compared metric the engine records two CALCULATION records linked (INPUT) to the company's
own value and every peer's value: `PEERRANK.<metric>` and `PEERMEDIAN.<metric>`. Pillar scores
(`SCORE.<PILLAR>`) link to their ranks and `SCORE.overall` to the pillar scores, with the weights
stored. Validation recomputes each from its links: if a peer's input was superseded or withdrawn it
reports `CALC_INPUT_NOT_CURRENT`; if a value no longer follows from its inputs,
`CALC_RECOMPUTE_MISMATCH`, and everything above it (pillar score, overall) is blocked. A run also
stores the peer set, each peer's reason and every weight adjustment (a pillar without usable data is
dropped and the others scaled up, and that is recorded). Engine-made calculations whose inputs
vanished are retired automatically at the start of a run; nobody else's claims are touched.

## 5. Commands and API

```
python -m app.sector.cli analyze --symbol TCS     # record peers, comparisons and scores
python -m app.sector.cli analyze-all
python -m app.sector.cli peers --symbol TCS
python -m app.sector.cli scorecard --symbol TCS
python -m app.sector.cli weights --sector banking
```

`GET /api/companies/{id}/peers` (peers, reasons, those left out), `/scorecard` (pillar scores,
overall or why withheld, weights and adjustments, every comparison with the peers' values, gaps, and
a plain statement of what the scores mean), `GET /api/sector-weights?sector=`.

## 6. Real-data results (2026-11-08) - an honest reading

| Group | Peers found | Result |
|---|---|---|
| Banks (ICICI, HDFC, Axis) | 2 each (thin) | 6-9 metrics compared; pillar scores for growth, profitability, momentum; **no overall score** (30-45% of weight covered) |
| IT (TCS, Infosys, Wipro) | 2 each (thin) | 6-7 metrics; pillar scores for growth, balance sheet, momentum; **no overall score** (15-35%) |
| M&M, Reliance | none | no peer in the sample has data; nothing compared |

The engine works end to end and withholds what the data cannot support. What limits it is data, not
design: returns (ROE/ROA) need a common financial year but peers hold different years; valuation needs
share counts and four quarters; Kotak, SBI, HCL, Tech Mahindra, ITC, HUL, Maruti have no values yet.
Each company added with data raises coverage with no code change.
