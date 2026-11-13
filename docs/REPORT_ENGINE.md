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
