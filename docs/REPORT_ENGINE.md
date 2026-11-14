# Research report engine

How a stored report becomes an institutional-style PDF. Everything is deterministic: no LLM writes
or edits a number, a finding or a conclusion.

## Pipeline

```
validated evidence ──► report.build_content ──► research_reports.content (JSON, immutable, versioned)
                              │                          │
                              └─ insight.build           └─ pdf_pro.render ─► PDF (rendering only)
                                  analysis / mgmt /
                                  valuation / thesis
```

* `report.build_content` collects the evidence-register sections (as before) and now adds
  `content["analysis"]`. A failure in the analytical layer is logged and recorded
  (`analysis.available = false`, with the reason); it never blocks a report.
* The PDF is rendered from the stored content only. Older reports (no `analysis`) still render, in the
  evidence edition. `GET /api/research/reports/{id}/pdf?style=basic` returns the plain register.
* A changed analysis changes the content hash, so the next *Generate report* creates a new version;
  earlier versions and score history are untouched.

## Modules (`backend/app/research/`)

| Module | Role |
|---|---|
| `sectors.py` | One profile per sector (bank, IT services, FMCG, automobiles, energy, generic): which evidence metrics to trend, compare and monitor, the sector lens, and sector risk templates. Names only metrics that exist in the evidence store. |
| `analysis.py` | Phases 2-3. Annual and quarterly trends (growth, changes, unusual movements, rule-based observations) and peer comparison tables. |
| `mgmt.py` | Phase 4. Extractive management commentary from the stored MD&A pages: verbatim short sentences filed by keyword cues, each with its page. Nothing is paraphrased. |
| `valuation.py` | Phase 5. Historical P/E at year ends, peer P/E and P/B, bank justified-P/B (residual income) with sensitivity and market-implied ROE, relative (peer P/E) scenarios, and the list of models not completed. |
| `industry.py` | Industry overview (sector templates, no statistics) and the growth comparison: company CAGR, peer-set CAGR, growth alpha, relative price return. |
| `profile.py` | Company profile: segments and shares from evidence; verbatim, cited quotes on history, strategic changes and exposure; leaders named beside a title; group entities. |
| `cyclicality.py` | Cyclicality: sector characterisation, observed volatility, downturns and recoveries, sensitivities, indicators, limits. |
| `pdf_sections.py` | PDF sections for the four modules above. |
| `verdict.py` | BUY / SELL / WATCH with target, stop-loss, reward-to-risk and evidence confidence (methodology v1, below). |
| `thesis.py` | Phase 6. Bull and bear points, catalysts, risks, monitoring indicators, trajectory and the nine-question conclusion. |
| `insight.py` | Assembles the above for one company. |
| `pdf_pro.py`, `pdf_analysis.py` | Page layout, charts and tables. `pdf.py` is the older plain renderer, kept. |
| `compat.py` | `python -m app.research.compat [--out DIR]`: read-only check of the 24 pilot companies. |

## Statement types

Every statement carries one label: **VERIFIED FACT** (a stored value), **DERIVED** (computed from stored
values with a stated formula), **INTERPRETATION** (a rule-based reading), **ANALYTICAL HYPOTHESIS**
(goes beyond the data: catalysts, a valuation gap), **MANAGEMENT STATEMENT** (quoted, never a fact),
**ASSUMPTION** (a scenario input), **SECTOR TEMPLATE** (a generic industry risk, not evidence about
the company).

## Rules that protect correctness

* **Basis.** A trend series sits on one accounting basis. If the preferred basis has fewer than two
  annual points and the other has more, the other is shown in its own labelled row. A peer metric is
  compared on one basis for every company (the preferred one unless the other has more comparable
  peers); the row says which. Peers are compared only on the same period end, basis and unit.
* **Ranking.** No median or rank without at least three comparable peers. Within 5% of the median is
  "in line". Valuation multiples are positioned, not called better or worse.
* **Thresholds are presentation rules, printed in the appendix:** stable = under 1% (amounts) or 0.25
  points (ratios); an annual change over 25% is flagged for explanation; a P/E or P/B beyond 10% of the
  peer median is a premium or discount.
* **Missing is never zero.** A metric with no value is listed; a model with missing inputs is listed
  under *Models not completed* with the reason.
* **Valuation assumptions** (cost of equity 11/12/13%, long-run growth 7/6/5%, ROE shifts +2/0/-2
  points) are illustrative EquityOS defaults, printed beside every result as ASSUMPTION. No risk-free
  rate or beta is stored. Results are scenario-implied values, not target prices. Bank intrinsic value
  uses ROE and P/B on the same basis only.
* **Historical P/E** is skipped for a year when a corporate-action adjustment or flagged price gap lies
  after it, because per-share figures would not be comparable.
* **Verdict.** See "Verdict methodology" below. It is a stated rule set, not a forecast or advice.

## Known limits

* Business-model narrative (what the company does, product mix, retail versus corporate, CASA
  composition) needs the investor presentation or Pillar 3 documents, which are not parsed into
  figures.
* Management commentary needs a stored MD&A section (present for 5 of the 24 companies today). Change
  against previous commentary needs a second annual report.
* Peer metric coverage is bounded by extraction: banks other than Axis and ICICI lack consolidated ROE
  and P/B, so some rows compare on the standalone basis; CET1 and Tier 1 are not extracted.
* A DCF is not built for non-banks: there are no sourced forecasts or discount rate.
* The source register shows document id prefixes, not file names or filing dates.
* Sector profiles for FMCG, automobiles and energy use the same non-financial metric set; operating
  metrics (volumes, utilisation, attrition) are used only where captured and are not yet in the
  report.

## Verdict methodology (v1, `verdict.py`)

Five components, each scored -1 / 0 / +1 (valuation counts double):

| Component | +1 | -1 |
|---|---|---|
| Financial score | 60 or more | 40 or less |
| Financial trajectory | improving | deteriorating |
| Peer standing (better minus worse, over rankable metrics, needs 3) | net share at least +1/3 | at most -1/3 |
| Valuation (mean base-case value vs price), weight 2 | upside of 15% or more | 15% or more below |
| Price trend (close vs 200-day average) | more than 5% above | more than 5% below |

* **Verdict:** net points of +3 or more is BUY, -3 or less SELL, otherwise WATCH. BUY and SELL need at
  least four computable components and a reward-to-risk of 1.5 or more, else the verdict is WATCH and the
  reason is printed.
* **Target:** mean of the available base-case (BUY) or bear-case (SELL) scenario values. With one
  valuation method only half of the gap to that value counts. No time horizon is implied.
* **Stop-loss:** 2 x the 14-day average true range from stored prices, kept between 5% and 15% of the
  price. Not computed when a corporate-action adjustment or flagged price gap falls in the window.
* **WATCH** gives no trade levels, only the prices at which the valuation component alone would turn
  positive or negative.
* **Confidence** is evidence confidence, 0-100: coverage 40, agreement 30, research readiness 30, minus 10
  each for a LOW-confidence score, valuation methods that disagree in direction, and a single method.
  HIGH 85+, MEDIUM 55+, else LOW. It is not the probability that a trade works.
* Thresholds are EquityOS judgement and have **not been back-tested**. Review them before relying on
  the output; they are constants at the top of `verdict.py`.

## Report quality changes (2026-11-12)

* **Dates** are shown as DD/MM/YYYY everywhere the PDF prints text (`pdf.dmy`, applied in `clean`); stored
  dates and fiscal-year labels are unchanged. Quoted passages stay verbatim, so a date inside a quotation keeps
  the company's own wording.
* **No inline evidence labels.** The "Verified / Derived / Interpretation" tags beside each fact are gone. A
  reported figure cites its page and evidence number; a calculated figure says "calculated" in its source
  note; hypotheses are written as hypotheses; management statements are in quotation marks with a page;
  industry text and sector risks say they are sector context. Classification lives in Appendix A (Type
  column), the methodology and Appendix E (data quality).
* **Contents page** (two-pass build) and section order: executive summary, industry overview, company and
  management profile, financial performance, growth versus the industry, cyclicality, peers, management
  commentary, valuation, thesis and verdict, scorecard, conclusion, appendices.
* **Industry CAGR** is the *stored peer set's* CAGR (median of peers and combined), because EquityOS holds no
  industry statistics. "Growth alpha" is company CAGR minus that, in points; it is not investment alpha. The
  relative price return is defined separately (price only, peer-median benchmark, not risk-adjusted).
* **Not stored, therefore not shown:** founder and management biographies, board histories, customer
  concentration statistics, geographic revenue splits (unless a quoted passage states them), industry-wide
  market data. History reaches back only as far as the stored annual figures (at most four years), so
  cyclicality is indicated, not established.

## Universe-wide report inputs (2026-11-13)

What a report can now draw on beyond the 24 pilot companies, and how each input is kept honest.

**Where the inputs come from**

| Input | Module | Stored in | Rule |
|---|---|---|---|
| Fundamentals (income, balance sheet, cash flow, ratios, shareholding, actions) | `providers/upstox_refresh.py` | evidence store | Upstox is keyed by ISIN; the profile text must name the company or nothing is stored |
| Industry label + sector market cap | `providers/classification.py` | `company_classification` | the PROVIDER's label, dated, with its source; never described as an exchange classification |
| Official website | `acquisition/website_discovery.py` | `company_website_registry`, `companies.website` | only a verified page is registered; ambiguity is flagged for a person; a verified URL is never replaced |
| Public documents | `acquisition/evidence_fetch.py` (existing) | documents, evidence | runs once a website is registered |
| History, leadership, business quotes | `research/enrich.py` | `company_enrichment` | a model only EXTRACTS; every quote must appear verbatim in the fetched page; leadership needs the name and the title inside the quote |
| User uploads | `research/vault.py` + `docintel/*` | documents, evidence | basis/unit questions are asked, never assumed; private uploads never feed shared reports |

**Sections that are left out.** A section appears only if it has something real to show. Growth,
cyclicality, peers and management commentary are omitted when empty; a mostly empty company profile
collapses to one "Not available for this company: ..." line; the contents page and section numbers are
recalculated, and a conditional page break replaces the hard one after the profile.

**Industry growth, in two tiers** (`research/industry.py`)
1. *Peer set* (existing): the stored same-industry peers closest in size, at least 3 with the same two
   fiscal years on the same basis.
2. *Industry group* (new, `industry_group`): the other companies the provider files under the same sector
   label, restricted to those whose stored annual figures cover the SAME two fiscal years on the SAME basis
   as the company; at least 5 are required. The aggregate is the combined value of exactly those companies
   at both ends (nobody enters one year and not the other). Reported: company CAGR, group median CAGR,
   group combined CAGR, growth alpha vs each (company minus group, percentage points), companies used out of
   the group size, and each member's CAGR with evidence ids. The provider's sector market capitalisation is
   shown for context and used in no calculation. If fewer than 5 qualify, the report prints the reason and
   no number. These figures describe the companies that have stored filings, not the whole industry, and
   say so; coverage rises as `upstox-refresh` proceeds through the universe.

The qualitative sector template is unchanged and still labelled as general context with no statistics.

**Document engine repairs that feed reports** (`docintel/`)
* A statement whose row labels sit BETWEEN two groups of figures (a real Nestle results layout) is read:
  `layout.move_middle_labels` moves the label column to the left edge only when at least six rows and half
  of all figure rows use that layout; figures (and so citation boxes) are never moved.
* A page of figures with no unit line in its text layer ("(Rs. in million)" drawn as outlines) gets one OCR
  read for the unit line only (`pdf.lacks_unit_statement`, `merge_ocr_labels`); no OCR digit is ever used.
  Needs Tesseract (Docker image); without it the uploader is asked.
* Poor scans are re-read at denser settings and the most confident reading wins (`pdf.ocr_best`); a scan's
  own OCR layer with a high share of malformed words is replaced only when our reading is at least 75%
  confident. OCR ruling marks ("|", "[") and cross-references ("(3)+(4)") are stripped from labels.
* A document that does not print its basis or unit raises a question once (buttons in the Documents tab);
  the answer is recorded as "stated by the uploader" and a printed value always wins.

## What feeds a report (updated 2026-11-14)

A report uses only evidence that has been validated: Upstox facts after the `evidence` stage, figures
extracted from documents after the `extract` stage, derived metrics after the `calculate` stage, and
user uploads after processing. The pipeline ([PIPELINE.md](PIPELINE.md)) now runs all of these for every
company it processes, so a freshly fetched filing is read, validated, calculated and reflected in
readiness without a manual step. A document that is merely stored is listed as held and counted in
"document coverage" but contributes no figure, and readiness says "a stored document of the right type
has not been processed yet" only when that is true.

When a company has an existing saved report, the `report` stage regenerates it as a new version if its
readiness inputs changed (the earlier version is kept; unchanged content writes nothing). The pipeline
never starts a report nobody asked for.

Readiness messages: when the chosen basis (default consolidated) stopped reporting and the other basis
is current, the reason names both ("the consolidated figures were last reported for 31 Mar 2018 ...
standalone figures exist for 31 Mar 2026") and the company is ASSISTED, not BLOCKED; a specific stale
reason is never replaced by the generic "document not processed" text.

## Growth and profitability (CP3, 2026-11-14)

`research/profitability.py` builds `analysis["profitability"]` from stored evidence on the one resolved
accounting basis (never mixing consolidated and standalone).

- **Growth** (non-financials: revenue, gross profit, EBITDA, EBIT, net profit; banks: total income, net
  interest income, operating profit before provisions, net profit): annual series, year-on-year growth,
  CAGR over the full span and over exactly three years when four year ends exist. Each figure carries its
  evidence ids. A CAGR needs a positive start and end; otherwise the reason is shown. A missing year gives
  no year-on-year figure.
- **Margins**: gross, EBITDA, EBIT, net (banks: PPOP margin, net profit to income) with the change in
  basis points, EXPANDING / CONTRACTING / STABLE (within 0.5 percentage points over the window is
  STABLE), years up and down, peak and trough, and rule-based observations citing evidence.
- **Margin bridge** (non-financials): per-line change as a share of revenue with an explicit "not
  itemised" remainder; withheld when fewer than three cost lines exist for two years.
- **Peer comparison**: the company's CAGR and latest margin against the peer-group median over the same
  fiscal years and basis, at least three peers. The benchmark is labelled "peer group" everywhere. No
  industry statistics are held, so there is no industry CAGR and no growth alpha; the difference vs the
  peer group is shown in percentage points.
- New calculations: `INCOME.gross_profit`, `MARGIN.gross_margin`, `GROWTH.ebitda_yoy`, `GROWTH.ebit_yoy`,
  `GROWTH.gross_profit_yoy`, 3-year CAGRs for revenue, EBITDA and gross profit.
- The PDF section "Growth and profitability" is omitted when nothing is available. The older section was
  renamed "Growth versus the peer group: CAGR".

Related corrections made while validating: a subsidiary's statements printed in a parent's annual report
(e.g. a "... Cerium Private Limited" page) are no longer attributed to the parent, and sources that differ
by a factor of ten or more are a conflict rather than being settled by authority.

## Cash flow and earnings quality (CP4, 2026-11-14)

`research/cashflow.py` builds `analysis["cashflow"]` for non-financial companies on the one resolved
accounting basis (banks are excluded, with the reason shown: a bank's operating cash flow is dominated by
deposits and advances).

- **Operating cash flow**: annual series, year-on-year growth, CAGR.
- **CFO / PAT** by year and cumulatively over the years where both exist, read as STRONG (>= 1.0),
  ADEQUATE (>= 0.8), WEAK (>= 0.5) or POOR; **cash conversion** = CFO / EBITDA; CFO margin.
- **Free cash flow** = CFO + capital expenditure (stored as an outflow); calculated only where the cash
  flow statement prints a capex line. FCF margin and FCF / PAT. No estimate is made where capex is not
  printed; the reason is listed instead.
- **Capital expenditure**: spend, spend / revenue, / CFO and / depreciation (above 1x grows the asset base).
- **Accruals** = PAT - CFO; accrual ratio = accruals / average total assets (closing assets when no opening
  balance sheet exists): cash-backed (<= 0), normal (< 5%), elevated (< 10%), high.
- **Divergences**: years where profit was positive with zero/negative cash, cash under half of profit,
  profit rising while cash fell (and the reverse), each citing its evidence; plus the working-capital and
  non-cash lines the cash flow statement itemises for the latest year, labelled as not necessarily summing
  to the gap.
- **Peer comparison**: CFO/PAT, CFO margin, FCF margin and capex/revenue against the peer-group median
  (>= 3 peers, same fiscal year and basis).
- New calculations: `CASH.cfo_margin`, `CASH.cfo_to_ebitda`, `CASH.fcf_margin`, `CASH.fcf_to_net_profit`,
  `CASH.capex_to_revenue`, `CASH.capex_to_cfo`, `CASH.capex_to_depreciation`, `ACCRUAL.accruals`,
  `ACCRUAL.accrual_ratio`.
- The PDF section "Cash flow and earnings quality" is omitted when nothing is available.

Document reading: capital-expenditure lines were being withheld because the cash-flow statement had no
arithmetic check that could verify the column. A check was added (operating cash flow = cash generated
from operations - income taxes paid) and cash-balance label variants were added to the taxonomy.

## Working capital and financial health (CP5, 2026-11-14)

`research/health.py` builds `analysis["health"]` on the one resolved accounting basis.

- **Working capital** (non-financials): receivable days (closing trade receivables / revenue x 365),
  inventory and payable days (on cost of goods = revenue - gross profit), the operating cycle
  (inventory + receivable days) and the cash conversion cycle (operating cycle - payable days), with the
  change in days and a rule-based reading (within 5 days is stable).
- **Leverage and liquidity**: debt-to-equity, net debt (borrowings - cash - other bank balances - current
  investments; negative = net cash), net debt/EBITDA, borrowings/EBITDA, interest cover (EBIT and EBITDA over
  finance costs), current and quick ratios, with stated-threshold readings.
- **Repayment**: years of operating cash flow needed to repay borrowings, cash against current borrowings,
  operating cash flow against borrowings.
- **Returns and DuPont** (banks too): ROE, ROCE (non-banks), ROA; ROE = net margin x asset turnover x
  equity multiplier on average balances, product-checked against the stored ROE, the leading driver of the
  change by log decomposition, and a five-step form where EBIT is stored.
- Peer-group comparison (>= 3 peers, same year and basis). Banks get returns and DuPont only.
- New calculations: `WORKING_CAPITAL.*` days and cycles, `LIQUIDITY.quick_ratio`,
  `LIQUIDITY.cash_to_short_term_debt`, `LIQUIDITY.cfo_to_debt`, `LEVERAGE.total_borrowings` (one line, or
  non-current + current), `LEVERAGE.net_debt`, `net_debt_to_equity`, `net_debt_to_ebitda`, `debt_to_ebitda`,
  `RATIO.ebitda_interest_cover`.
- **Materiality gate**: gross profit (and so inventory days, payable days and the cycles) needs cost of goods
  of at least 10% of revenue. A services company's small stock in trade (Wipro: 0.7%) produced absurd
  figures (a cash conversion cycle of -6,600 days) before this gate.
- **Withdrawal rule**: when a metric's condition stops being met for a period, the earlier engine-made value
  is withdrawn with the reason, so a tightened definition cannot leave stale figures live. The calculate
  fingerprint now covers the whole catalogue definition.

## Sector-specific indicators (CP6, 2026-11-14)

`research/sector_kpis.py` builds `analysis["sector_kpis"]` from the company's sector (BANK, IT_SERVICES, FMCG,
AUTOMOBILES, ENERGY; other sectors show the general sections only). Each indicator is a series from validated
evidence marked REPORTED (stated by the company or filing, with its period; no accounting basis) or
CALCULATED (calculation engine, on the report's basis), with latest value, change and evidence ids; an
indicator nothing supports lists its reason.

- Banking: NIM, GNPA/NNPA ratio and amount, provisions/PPOP, CASA, credit and deposit growth, credit-deposit
  ratio, capital adequacy, cost-to-income.
- IT: revenue growth, EBIT/EBITDA margin, employee cost/revenue (new `MARGIN.employee_cost_ratio`),
  attrition and utilisation (voluntary/total and with/without trainees kept apart), receivable days.
- FMCG: revenue and volume growth, gross and EBITDA margin, receivable and inventory days (distribution).
- Automobiles: stated sales volume, derived volume growth and revenue per vehicle (formula shown), segment
  mix, margins. Energy: revenue growth, EBITDA margin, inventory days, net debt/EBITDA, production,
  realisation, throughput and GRM where stated, segment mix.
- Peer comparison uses the most recent period (of the latest four) that at least three peers also state.
The PDF section "Sector-specific indicators" is omitted when empty.

## Company and business analysis (CP7, 2026-11-14)

- **Profile topics** (`research/profile.py`) now include business model, revenue sources, product exposure,
  competitive advantages, competitive positioning and dependencies/structural risks, besides history, strategic
  changes and exposure. Each is a sentence quoted verbatim from the latest stored annual report with its
  page, filed by keyword cues; per-topic exclusions drop forward-looking disclaimers, personal rankings,
  hedging notes and statements that deny a risk, and company-subject topics need the company as subject.
- **Segment profit contribution**: each segment's reported result as a share of the segments' combined result,
  result growth, result margin and revenue share, with the largest segment's share.
- **`research/business.py`** (`analysis["business"]`): revenue sources from figures (operating revenue or a bank's
  net interest income against other income, by year), competitive positioning from the peer comparison
  (where the company is better, worse or in line, with evidence ids) plus stated positioning, and business
  strengths and structural weaknesses as two sides: EVIDENCE findings by stated rules (peer-relative growth,
  margin direction, cash conversion, accruals, ROE, leverage, interest cover, current ratio, segment
  concentration) citing evidence ids, and DOCUMENT_QUOTE statements labelled as the company's own claims.
- The PDF adds "Business strengths, weaknesses and positioning" after the profile; the segment table gains a
  profit-share column. Sections with nothing to show are omitted.

## Management, governance and capital allocation (CP8, 2026-11-14)

`research/governance.py` builds `analysis["governance"]`. Support is labelled: EVIDENCE (validated figures,
exchange corporate-action feed), DOCUMENT_QUOTE (sentence from the latest stored annual report, with its page)
and PARSED (a number read from one such sentence by a stated pattern, shown with it).

- **Management**: leaders named beside a title (from the profile), background and tenure quotes, and the company's
  own financial track record (revenue and profit CAGR, ROE) over the stored years; no biography is added.
- **Promoter holding**: quarterly promoter percentage series, change and direction; no promoter reported is stated as
  widely held. Pledges are not stored.
- **Board**: size and independent directors parsed only from one sentence that states both; committees checked
  against the report text; board quotes.
- **Governance assessment**: a checklist, not a score. Tests: independent directors (at least one third), woman
  director, the four statutory committees described, promoter holding (a fall of 5 points is flagged), promoter pledge,
  auditor's report flags, related-party arm's-length statement (a denial is not a pass), and dividends plus buybacks
  against free cash flow. Each is PASS, FLAG or UNKNOWN with its rule and source.
- **Related-party transactions**: quoted statements; amounts are not extracted.
- **Capital allocation**: uses of operating cash flow by year (capex, dividends, buybacks, acquisitions as printed,
  as shares of CFO), payout ratio, dividend announcements from the exchange feed (limited window) and policy quotes.
  New cash-flow taxonomy items: `buyback_of_shares`, `acquisition_of_subsidiaries_and_businesses`.
- **Acquisitions and capex assessment**: capex and capex/depreciation, acquisitions cash, goodwill growth and its
  share of equity, ROCE shown beside capex with no causal claim, and acquisition quotes.
The PDF section "Management, governance and capital allocation" is omitted when empty; management commentary stays
in the management-discussion section.

## Industry and peer analysis (CP9, 2026-11-14)

- **Peer selection by business similarity** (`sector/core.py`, `sector/store.py`): the gates are unchanged (same
  statement profile, sector and industry); within them peers are ordered by half business similarity and half size.
  Similarity uses only business traits, never performance: reported segment words (Jaccard), the provider's industry
  label, goods-versus-services, employee cost share and capital intensity, over the traits both companies have. A
  candidate is excluded as "too different" only when at least three traits compare and similarity is below 0.30. The
  reason and the traits compared are stored with each peer. With no traits the order is by size, as before.
- **Misleading ranks prevented** (`research/analysis.py`): a median needs 3 comparable peers; a rank or better/worse
  verdict needs 4 comparable peers and at least half of the selected peers. Otherwise the position is "not ranked"
  with the reason, and each comparison carries its coverage (comparable of selected) and reliability
  (HIGH, MEDIUM, LOW or NONE). Peers on another period, basis or unit are listed but never compared.
- **`research/industry_view.py`** (`analysis["industry_view"]`): three benchmarks kept apart: PEER_GROUP CAGR,
  SECTOR_LABEL_GROUP CAGR (stored companies under the provider's label) and INDUSTRY (verified official statistics:
  none held, so no industry size, no industry CAGR, no growth alpha). Structure and competitive intensity are
  concentration (largest, top three, HHI) among stored companies of the label group, read only when they cover at
  least 60% of it. Regulation, macroeconomic sensitivity, leading indicators, opportunities and threats are sector
  context; each opportunity or threat is checked against the company's own stored figure (favourable or unfavourable
  direction). A peer-quality block summarises reliability and lists the comparisons withheld.
- The PDF section "Industry and peer analysis" is omitted when empty. Run `python -m app.sector.cli analyze-all` after
  a selection change so stored peer sets and ranks use the new rules.

## Valuation assumption infrastructure (CP10, 2026-11-14)

`research/assumptions.py` is the assumption ledger on top of `evidence.ledger.register_assumption` (which now also
stores provenance in `extra`). Every valuation input is one of four kinds, kept apart in the report:

- **Historical actuals**: validated figures (price, P/E, P/B, ROE) with evidence ids.
- **Sourced inputs**: a named, dated source (risk-free rate, equity risk premium, beta), with source, optional URL,
  as-of date, author and rationale.
- **User assumptions**: a person's judgement (cost of equity, long-run growth, ROE shift per scenario) with author,
  date and rationale; labelled as judgement.
- **Derived outputs**: calculations registered with links to every input: the CAPM cost of equity
  (`VALUATION.capm_cost_of_equity` = risk-free rate + beta x premium) and each scenario's justified P/B and value.

Rules: **nothing market-related is invented**: there is no built-in risk-free rate, premium, beta, cost of equity or
growth; a valuation lacking an input is NOT COMPLETED and names what is missing. The old built-in bear/base/bull
defaults were removed. Inputs are range-checked (a fraction entered for a percent is refused), a sourced input older than
its limit (risk-free 45 days, premium and beta 400 days) is flagged STALE, and a market input without a source is
flagged as not source-backed. **Editing never overwrites**: a new value supersedes the old record (kept, with editor and
reason), the derived CAPM figure is withdrawn and re-derived, and the history is queryable. Identical re-entry is a no-op.

Entering and editing: `python -m app.research.assumption_cli list|set|history|withdraw` and
`GET/PUT /api/companies/{id}/valuation-inputs`, `GET .../{name}/history`, `POST /api/valuation-inputs/{id}/withdraw`
(API key required; the recorded author is the authenticated client). The PDF valuation section gains "Valuation inputs
and their provenance". The sensitivity and market-implied tables are grids of hypotheticals and are produced only when a
base long-run growth has been entered.
