# Milestone 3 handoff: institutional-quality research (written 2026-11-14, before implementation)

Status: **not started.** Milestone 3 begins only after the Milestone 1 soak-test results are reviewed. This
file is the brief for Step 1 and the standing rules for the whole milestone.

## Roadmap in force

* **Priorities:** Milestone 1 (pipeline reliability, complete and locally committed as `3b1fbff`, not pushed)
  and Milestone 3 (institutional-quality equity research).
* **Frozen, do not start or expand:** Milestone 2 (universe coverage: large website, Upstox or document
  campaigns, universe expansion, coverage maximisation) and Milestone 4 (AI research copilot: conversational
  agents, natural-language screening, idea generation, alerts, continuous thesis monitoring, new copilot UI).
  Existing integrations and data stay as they are.
* **Scope:** the 24-company pilot universe (`app/seeds/sample_universe.py`, `SAMPLE`): banking, IT, FMCG,
  automobiles, energy. Build sector capabilities that can later serve other companies, without expanding the
  universe now. No large universe run.
* **Rules:** preserve the architecture, evidence lineage, historical records and scoring methodology unless a
  specific defect needs a documented correction; keep the light-theme frontend; modular and testable code;
  no commit, push or deploy without approval; never leave a blank section in a report.

## Decisions already made (apply to every Milestone 3 step)

1. **DCF** starts as an assumption-driven scenario engine: explicit revenue growth, operating margin, tax,
   reinvestment and working-capital assumptions, FCF, discount rate, terminal growth, terminal value,
   enterprise and equity value, sensitivity analysis. Every assumption visible, editable and labelled as one of
   (1) historical actual, (2) sourced external input, (3) user-defined assumption, (4) model-derived output.
   Never presented as a forecast. **No invented risk-free rate, equity risk premium or beta.** Banks and other
   sectors get their own methods; no FCFF DCF forced on banks.
2. **Industry statistics** from verified, legally accessible sources where they exist (official publications,
   regulators, industry associations, public research). Otherwise the existing peer-group CAGR, **labelled
   "peer-group CAGR", never "industry CAGR"**, and **no growth alpha against a wrongly labelled benchmark**.
   Company and benchmark CAGR periods must match.
3. **Accounting basis:** consolidated by default. If consolidated is unavailable and valid standalone exists,
   fall back to standalone automatically, disclose the basis in the report, show a warning that consolidated
   data was unavailable, use one basis across all comparisons, never combine bases silently, keep both bases.
4. **Reports:** every material financial conclusion rests on validated data; every external qualitative claim
   has a traceable source; no fabricated information; no generic filler; no blank or empty sections.

## Step 1: Financial Statement Depth (the next step)

Goal: for every pilot company, a complete, evidence-backed analysis of the three statements and the ratios
that follow from them, on one stated basis.

### What exists (reuse, do not rebuild)

| Need | Where | State |
|---|---|---|
| Validated statement evidence (income, balance sheet, cash flow; annual and quarterly; basis, unit, period, source, page) | `evidence/` (`sync.py`, `engine.py`, `ledger.py`), `models/evidence.py` | working; read through `research/readiness.py` `_Store` |
| Derived metrics as data (formula, variants, gaps listed, never estimated) | `calc/catalogue.py`, `calc/engine.py` | has growth (revenue, net profit, NII, advances, deposits), EBITDA/EBIT/net/PBT margins, interest cover, ROCE, ROE/ROA (closing and average), asset turnover, equity multiplier, debt to equity, current ratio, free cash flow, CFO to net profit, effective tax rate, cost to income, TTM |
| Annual and quarterly trend series, change rules, observations, basis discipline | `research/analysis.py` (`series_for`, `trends`, `quarterly_rows`) | working; thresholds are presentation rules |
| Sector profiles: which metrics to trend, compare, monitor | `research/sectors.py` | six profiles; FMCG, autos and energy share one non-financial set |
| Report assembly, sections omitted when empty, PDF layout and charts | `research/insight.py`, `research/report.py`, `research/pdf_pro.py`, `pdf_analysis.py`, `pdf_sections.py` | working; content is stored, versioned, hashed |
| Integrity and monitoring | `evidence/integrity.py`, `pipeline/metrics.py` | run the audit before and after |

### Gaps to close in Step 1

* **Growth:** revenue CAGR exists; **EBITDA CAGR and PAT CAGR are missing.** Define CAGR over matching fiscal
  years, same basis, with the periods printed; refuse (with the reason) when fewer than two comparable annual
  points exist.
* **Profitability and efficiency:** present margins, ROE/ROCE/ROA and asset turnover as multi-year tables, with
  a DuPont-style decomposition where the inputs exist.
* **Liquidity and leverage:** current ratio exists; add quick ratio, net debt, net debt to EBITDA, interest
  cover trend, debt maturity only if a source states it. Banks use their own set (capital, asset quality,
  funding) and must not be given industrial ratios.
* **Working capital:** receivable, inventory and payable days and the cash conversion cycle, where the balance
  sheet and income statement lines exist; otherwise state what is missing.
* **Earnings quality:** CFO to PAT trend, accruals, other income share, exceptional items, tax rate
  stability; flag, do not judge, with the evidence ids.
* **Capital allocation (financial side):** capex, dividends, buybacks, acquisitions from the cash flow
  statement; payout ratio; reinvestment rate. (Management commentary on allocation belongs to Step 4.)
* **Basis fallback:** implement Decision 3 centrally (one resolver used by every Step 1 table), with the
  warning text in the report and a test that mixed bases are impossible.
* **Missing inputs:** the catalogue says depreciation and finance costs are only available when the filing
  prints them. Where a metric cannot be computed, the report states why; it never leaves a gap and never
  estimates.

### Suggested shape (keep it modular)

* New derived metrics go into `calc/catalogue.py` as data (with variants and gap reasons), not into report code.
* One new module, `research/financials.py`, assembles the statement-depth analysis (multi-year tables, CAGRs,
  working capital, earnings quality, leverage, liquidity) from evidence, via the existing `_Store` and basis
  rules. `insight.py` calls it; `pdf_sections.py` renders it; empty sub-sections are omitted with a one-line
  "not available because ..." note, as the profile section does.
* Sector variants (bank versus non-financial first; then IT, FMCG, autos, energy) are selected from
  `research/sectors.py`.

### Acceptance criteria

1. Every pilot company's report shows the statements section on a stated basis, with the fallback warning
   where it applies, and no blank or unexplained gap.
2. Every number cites its evidence id and period; every derived number states its formula; no value is
   estimated.
3. CAGRs use matching years and basis; an unavailable CAGR says why.
4. Banks and non-financials get different metric sets.
5. `python -m app.research.compat` shows no regression across the 24 companies; the integrity audit gains no
   new ERROR.
6. New tests for: CAGR edge cases, basis fallback and refusal to mix, each new metric (including its gap
   reasons), section omission, and one report per sector rendering to PDF. Existing tests unchanged.

### Order of work inside Step 1

1. Basis resolver and its tests. 2. New catalogue metrics (CAGRs, working capital, net debt, quick ratio,
earnings quality) with tests. 3. `research/financials.py` and sector selection. 4. Report and PDF rendering.
5. Run the pilot companies, read the reports, fix gaps. 6. Documentation and a report for review.

## What the soak test settles before this starts

The soak result (recorded in `PROJECT_STATUS.md`) decides whether regenerated reports can be trusted to
update on their own while Step 1 changes report content: a content change in the analysis layer changes the
report hash, so the next regeneration creates a new version; older versions are kept.

## Known facts to keep in mind

* Many pilot companies have both bases; some small companies have standalone only. Readiness defaults to
  consolidated.
* History reaches back four annual points at most for Upstox-sourced companies, longer where annual reports
  are extracted, so multi-year tables and CAGRs will often cover three to four years.
* Peer-rank and peer-median calculations can go stale when peers refresh (26 rows, 3 companies at the last
  audit); refresh them by re-running the sector PEERS and SCORING stages, which Step 2 will touch anyway.
