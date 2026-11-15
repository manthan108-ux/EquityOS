# Known limitations

Honest list, as of 2026-11-08 (Phase 6 added at the end). Each item says what happens instead of a wrong answer.

## Data coverage
* Only ICICI Bank and Reliance have stored documents with extracted statements. TCS and Infosys
  block automated access (HTTP 403, not bypassed). HDFC Bank's documents are listed but every PDF
  download from its site timed out. Use `ingest-inbox`. Coverage says why a document is missing and
  never marks a company complete because a site was down.
* Upstox fundamentals are still unconfigured (no token yet).
* Exchange announcements, XBRL and shareholding patterns of the Reg. 31 form have no permitted
  automated source. Shareholding is read only from annual-report tables of the "category / number
  of shares / % holding" form (verified on ICICI Bank).
* Corporate actions are not acquired; prices are unadjusted.

## Extraction
* Layouts verified: ICICI Bank (bank format, born-digital) and Reliance (Ind AS, scanned). Other
  layouts may leave labels unmatched or fail checks; the effect is withheld values, not wrong ones.
* April-March fiscal years are assumed (half-year = 6 months to September, nine-month = to
  December). Other fiscal years: such columns are staged, not stored as H1/9M.
* Schedule tables that are roll-forwards (fixed assets) fail their total check by design and are
  withheld. Schedule 9 (advances) has one repeated total that does not reconcile.
* Related-party transaction amounts, contingent-liability narrative tables, and notes other than
  tables with a total are not extracted. The group roster is read from "name / nature of
  relationship" tables only.
* Segment, ratio and schedule values count as validated only when their arithmetic holds and the
  column layout matches an already-verified primary statement of the same document.
* Auditor-report flags are explicit phrases ("Key Audit Matters", "Basis for Qualified Opinion");
  they are not an assessment of audit quality.
* Standalone quarters derived from cumulative figures carry the rounding uncertainty of their two
  inputs (stored in `derived_from`).

## OCR
* Windows has no Tesseract (needs an administrator install); Docker has Tesseract 5.5.
* Dense multi-column scanned statements: numbers are often read right, but column dates are often
  garbled, so those columns are refused. Reliance's ratios and cash-flow tables stay unusable.
* The second-reading check withholds correct values when Tesseract itself errs (it did for 27
  values on the Reliance filing). They are flagged `needs_review` and nothing reviews them yet
  (no review UI, only `needs_review` in the database).
* Only English text; no deskewing or table-line detection.

## Engineering
* PyMuPDF is AGPL-3.0. Decision (see [LICENCES.md](LICENCES.md)): keep it while EquityOS is private;
  replace it (pdfminer.six + pypdfium2, tested) or publish under AGPL before any public use. Gate for
  Phase 12.
* The API has no authentication and no upload endpoint by design.
* Superseded extraction runs are kept (audit); `document_pages` text of old runs uses disk space
  until you approve pruning.
* CI does not build the Docker images.
* The frontend was never exercised in a real browser.

## Sector and peer engine (Phase 8)
* Only the 8 companies with values can be compared; the largest groups (3 banks, 3 IT firms) give 2
  peers each, so every comparison is thin (LOW quality). No company has an overall score yet.
* ROE/ROA and other annual-only metrics need a common financial year: HDFC and Infosys hold different
  years from ICICI and TCS, so they often cannot be compared. Valuation multiples need share counts
  (only ICICI has one) and four quarters.
* Asset quality (GNPA/NNPA), net interest margin, CASA, EBITDA margin and interest cover are not
  scored because they are not extracted; the banking and IT pillars are weaker for it.
* Sector weights are curated judgement with stated reasons, not empirical or optimised, and the
  sector/industry of the 10 newer sample companies is a curated starting point.
* A rank among 2-3 peers is coarse (0, 25, 50, 75, 100 with two peers). Size ordering uses annual
  total income, which for a consolidated bank includes subsidiaries' businesses.
* Quarterly figures stand in for annual ones only for period-length-independent ratios; the metric is
  then labelled with its period.
* Extraction still withholds Kotak, SBI, HCL, Tech Mahindra, ITC, HUL and Maruti (scans, image-only
  covers, mirrored table layouts, press releases instead of statements).

## Document typing
* Documents are typed from content first, then file name, else stored as `other`. Content rules are
  keyword based (statement words, "press release", safe-harbour text) and can mis-type: HUL's
  result PDFs contain safe-harbour text, so content alone would call them presentations. Both types
  go through extraction, so nothing is lost, but the label can be off.
* Press releases and `other` documents are stored and issuer-checked but not extracted.

## Research sample (2026-11-08)
* Of the 15 sample companies, 4 have values that pass all checks (ICICI Bank, Reliance, M&M, Wipro
  partially). Eight need manual filings. Kotak, Tech Mahindra and ITC have documents stored but
  nothing promoted yet (scanned bank results, image-only annual-report cover, press releases).
* ZIP result kits (Kotak, M&M) are stored but not opened.
* A document can only be linked to a company if its text names the company; annual reports whose
  cover is an image are withheld rather than assumed.
* Sector and industry on the 10 new companies are curated starting points, not an exchange
  classification; the Phase 8 sector engine assigns the real ones.

## Evidence engine (Phase 6)
* Only ICICI Bank (1,605 values) and Reliance (220) have evidence; TCS, Infosys and HDFC Bank have no
  stored documents. Cross-SOURCE conflict and authority rules are tested on synthetic data only:
  no second provider (Upstox) has supplied fundamentals yet, so none has run on real data.
* Identity and sum rules need the relevant line items to be present. Where a statement lacks one the
  rule does not run (the run reports how many checks were actually performed).
* A claim's figures are matched by value and rounding, not by meaning: "profit rose 12%" passes if a
  cited value is 12% even when the cited 12% is something else. Direction words ("rose", "fell") and
  number words ("three segments") are not checked. Agent output still needs the Phase 9 review.
* Calculations carry the rounding of their inputs but do not propagate it numerically.
* Prices are not bulk-mirrored; `price_evidence` creates them on demand and they are UNADJUSTED
  until Phase 7 applies corporate actions.
* Plausibility thresholds (5x year change, 550/200-day staleness) are heuristics and only warn.
* Evidence ids change when a source value, location or the evidence hash scheme changes (the
  1,605 ICICI records were re-created once during development). Calculations built on replaced
  inputs are flagged, by design.
* `evidence_resolutions` is rebuilt from scratch on each validation run; it holds no original data.
* The API is read-only; there is no review screen for findings.

## Calculation engine (Phase 7)
* Only ICICI Bank and Reliance can be calculated; TCS, Infosys and HDFC Bank have no stored statements.
* No EBITDA, EBIT, operating margin, interest cover or ROCE: depreciation and finance costs are not
  extracted from documents yet. Free cash flow and debt / equity need cash-flow and borrowings items
  that Reliance's stored statements do not contain, so they are gaps.
* Growth, CAGR and average-balance metrics need earlier periods; only two financial years and a few
  quarters are stored, so most of these are gaps for older periods.
* Market cap and P/B need a share count; only ICICI's annual-report shareholding table supplies one
  (as at 31 March 2026, used with the November price and adjusted for later splits/bonuses only if a
  corporate action or a price-implied event is known).
* No corporate actions are acquired. Price adjustment therefore relies on the exchange's reference
  price; a split on a day whose prior session is missing from our data could be misread as an
  unexplained gap (it would be recorded, not applied). Dividends and rights are not adjusted, so
  returns are price returns, not total returns.
* Consolidated bank ratios mix in subsidiaries' businesses (insurance); the stored caveat says so,
  but the engine does not split them out.
* "Operating cash flow + capital expenditure" assumes capital expenditure is stored as an outflow
  (negative); a positive value is refused.
* EPS-based trailing figures are sums of reported EPS; share-count changes make them approximate.
* Technical indicators are only as long as the stored history (247 sessions); no intraday data.
* `price_adjustments` and `calculation_gaps` are rebuilt or appended per run; they are derived data.

## Added at the end of Phase 8 (2026-11-08)

* No overall score yet for any company: peer groups are small (2-4 per sector) and several sample
  companies have no statements at all. Scores appear only when 60% of the sector weight has data.
* Companies with no stored values, with the recorded reason: **SBI** (results PDFs are scans whose
  embedded text has corrupted digits), **HCL Tech** (scanned, garbled headers and units), **ITC** and
  **Maruti Suzuki** (only press releases supplied; full results documents are needed),
  **HUL** (mirrored table layout), **Kotak** consolidated (values vertically centred between
  wrapped labels; standalone works).
* The share count is derived from rounded paid-up capital; for a bank whose capital line is not in
  the stored statements there is no market cap.
* Full-text search is lexical (English stemming): it finds words, not meanings, and quoted scanned
  pages are only as good as the OCR.
* The review queue lists withheld scanned values and open findings but accepting a value is not yet
  possible; it must be checked against the page by a person.
* The Docker CI job runs on GitHub; its result has not been seen from this machine (no `gh`).

## Added in Phase 9 (2026-11-08)

* **SBI, Maruti**: no document-extracted values (press releases, scanned results). Both are served by Upstox
  only (annual/quarterly summaries, balance sheet, cash flow). Bank-specific lines for SBI (interest earned,
  deposits, advances) are missing, so its bank pillars are partly gaps and its overall rests on 65% of the weight.
* **Upstox gaps**: no competitors endpoint (client error), standalone and consolidated statements have identical
  value counts but are separate series; where a filing and Upstox define a line differently the filing wins.
* **Small sectors**: FMCG (ITC, HUL), automobiles (M&M, Maruti) and oil & gas (Reliance) have fewer than 3 peers in
  the sample, so no overall score; add more companies in those sectors to enable one.
* **Basis**: several companies are scored on standalone figures because that basis had more compared metrics;
  the choice is shown per report and never mixed within a comparison.
* **HUL**: values come from the LLM table reader and heading reader and pass the identities and the
  quarters-vs-half-year check; the annual-report statements and the Excel-style one-page PDF are still not read.
* **HCL**: one document's statements are read (consolidated and standalone, June 2026 quarter); the second PDF
  (cash-flow pages, OCR text) has no dates in its column headings and is not promoted.
* No company has an overall score yet: pillar coverage is 10-55% of sector weight (minimum 60%).
* LLM runs are not perfectly repeatable and free-tier quotas limit batch size.

## Added in Phase 10 (2026-11-10)

* **Data for the nine new companies is Upstox only.** Upstox has no depreciation, finance costs, borrowings,
  capex or shares outstanding, so EBITDA, ROCE, interest cover, debt/equity, free cash flow, market cap and
  P/B are missing for them. This is the main reason twelve companies have no absolute score. Filings (text PDFs)
  are the fix; the TVS annual report found has no text layer.
* **HUL, ITC, Reliance, Maruti** have no absolute score. Reliance has no segment data (SOTP is a framework only).
  HUL's FY26 growth may be affected by its ice-cream demerger; it is not in the identity registry yet.
* **Valuation benchmarks** need >=12 observations: price-date multiples give one per company, so valuation is
  usually unscored.
* **Phase 8 vs the scoring layer**: `sector analyze-all` still emits relative scores wherever there are three
  same-industry peers (Maruti, M&M, Hyundai). The scoring layer additionally applies peer *eligibility*; its
  verdict (shown in the API and dashboard) is authoritative.
* **TMPV relative score is 0.0** (last among three peers on every compared metric, standalone basis, small sample,
  restructured entity). It is shown with LOW confidence and the restructuring note; treat as unreliable.
* **Convention curves** are not validated benchmarks. No inflation or accounting-standard adjustment of benchmarks
  yet; at most four fiscal years per company, so no business-cycle depth.
* **Hybrid scoring** is disabled (calibration cannot be shown with this sample).
* **Dashboard**: scorecard, radar, drill-down with benchmark bars, history list and methodology switch are built.
  The compare view, score-movement chart, benchmark-coverage chart and the Reliance conglomerate page are not
  (the compare API exists). There are no automated frontend tests (none are configured); lint, type check and
  production build pass.
* **Bank metrics** (NIM, CASA, GNPA, NNPA, provision coverage, capital adequacy) are not extracted for any bank.
* **Recompute endpoints** are disabled by default (no authentication layer).

## Added in Phase 11 (2026-11-10)

* The report is structured data shown in the dashboard; there is no PDF rendering yet.
* Vehicle volumes, attrition, utilisation and FMCG volume growth cannot be read from any document
  yet; they are disclosed as unavailable and cannot be supplied by upload.
* NIM and CASA are read only from ratio tables printed in a document; they are not computed.
* A user who confirms an unreadable issuer still gets no figures if the statement pages name nobody.
* Peer and benchmark changes of OTHER companies do not trigger a re-run of the PEERS/SCORING stages;
  a source refresh or `force` does.
* Background jobs and the rate limiter live in one process; several workers need a shared queue and
  limiter.
* Private or licensed uploads are stored but never feed the shared evidence, so they cannot settle a
  requirement.
* The scorer still treats bank NPA / capital adequacy as having no source although ICICI has
  validated disclosure values.

## Added in Phase 11b (2026-11-10)

* Scanned results filings (SBI, HDFC Q2 FY27) have an embedded OCR text layer with misread digits and
  Tesseract confidence of about 50-65%; statement values from them are accepted only when arithmetic
  identities confirm them, so most of their statements are not read. A text-based filing, XBRL or
  Excel results file would fix this.
* Key figures from presentations are read only from simple tables, sentences and single-quarter
  slide bullets; multi-panel slides are skipped rather than guessed.
* SBI reports domestic and whole-bank NIM; only the domestic one is in its press-release table (the
  whole-bank NIM is in a sentence without a table period) and is labelled domestic.
* Gross advances and total deposits from a release are labelled as such; they are not the net
  balance-sheet advances, and credit-deposit ratio and provisions still need the statements.
* The operating readers need the period in the sentence; monthly sales releases and quarterly fact
  sheets of Maruti, M&M, TVS, TCS and Infosys were not among the stored documents, so only two
  real figures could be checked (Maruti quarterly volume, TCS annual attrition).
* The frontend unit tests mock the API; browser tests exist since Phase 12 (also with a mocked API).

## Added in Phase 11c (2026-11-10)

* P/B for a bank can be refused by the evidence ledger when the share count is consolidated and the
  only equity found is standalone ("inputs mix CONSOLIDATED and STANDALONE"). That rule is correct as
  written; whether a share count should carry an accounting basis at all is an open design question.
* SBI and HDFC have no shares-outstanding evidence (no paid-up capital in a validated statement), so
  market cap and P/B are missing for them.
* The Axis standalone balance sheet states no basis on its page ("Statement of Assets and Liabilities
  is given below"), so its values are not accepted; only the consolidated one is.
* Price-gap detection reports a suspected split/bonus; it is not applied until a corporate action
  confirms it, so multi-year price returns for HDFC Bank and Reliance remain unadjusted.
* (Resolved in Phase 12: the absolute bank score now reads NPA, NIM, capital adequacy and CASA.)

## Added in Phase 12 (2026-11-10)

* **XBRL reader has never seen a real exchange filing.** Concept names in `docintel/xbrl_map.py` come
  from published taxonomy naming and are exercised only on synthetic fixtures. Unlisted concepts stay
  raw and are counted as unmapped. Real files may state units, basis or percentages differently; the
  reader then refuses to promote (basis/unit/ratio scale are never guessed) rather than risk a value.
  Inline XBRL (iXBRL .htm) is refused. No automated download exists (terms unresolved).
* XBRL facts have no page: `page 1` stands for "the instance"; evidence cites the document.
* XBRL values are promoted when stated basis and unit exist and no identity fails, even if no identity
  covers the column (the source is machine-readable). A power-of-ten difference from an existing stored
  value blocks promotion.
* Corporate-action verification handles bonus issues stated in stored documents. Splits, rights and
  demergers are not auto-verified. Reliance's 2024-10-28 gap remains unexplained.
* Bank scores need disclosure ratios no older than 200 days; NIM definitions differ between banks
  (domestic / interest-earning assets / total assets) and the one used is named, not harmonised.
  Curves are conventions, not benchmarks. Scores are labelled with the effective config version 1.2.
* Browser tests mock the backend; they do not exercise the real API.

## Added in Phase 13 (2026-11-11)
* Only the 24 pilot companies have a stored website. For any other ticker the fetch reports
  `NO_WEBSITE`; add one with `set-website` (no automatic source is permitted: NSE/BSE terms are
  unresolved and Wikidata's SPARQL endpoint is disallowed by its robots.txt).
* Script-driven investor sites (Britannia, IOC, TMPV) expose no static file list; sites that return
  HTTP 403 (Godrej, Nestle) or disallow document paths in robots.txt (ONGC) cannot be fetched. These
  are reported, not worked around.
* The HTTP client caps a download at 60 MB; larger annual reports fail with `TOO_LARGE`.
* A link's period is a hint from its text. Annual statement PDFs published as separate files are
  stored as `quarterly_result` type because the link text says "Financial Statement"; extraction
  reads the real period from the document.
* Two-up detection needs two statement titles on one line; other multi-column spreads are not split.
* HCL Technologies' results document holds fewer promoted facts (53) than before the Phase 13
  reprocessing (73): it was reprocessed before the guard existed and its LLM header reading varies
  between runs. The earlier extraction runs are kept (marked superseded) so the facts can be
  recovered; they are not restored yet.
* One HDFC Bank value (Q1 FY27 standalone net profit) differed by a single digit between two reads of
  a scanned document; the latest read is not confirmed against the source and should be reviewed.
* Reprocessing skips documents that need our own OCR (not installed locally); run those in Docker.

## Universe coverage, uploads and industry research (2026-11-13)

Numbers change with every batch; `python -m app.providers.cli universe --status` prints the current ones.

**Coverage**
* Full NSE/BSE coverage is NOT claimed. Only a fraction of the 5,551 listed companies has a verified website,
  Upstox fundamentals and a provider industry label; the rest are simply not processed yet (the status command
  shows processed vs not processed). Nothing is estimated for them.
* Many company sites cannot be registered: robots.txt disallows automated access, the host answers 403, or the
  domain cannot be told apart from another company's (flagged `AMBIGUOUS`/`FAILED` for a person, never guessed).
  A host that does not answer at all is now reported as unreachable, not as a robots refusal.
* A full Upstox refresh takes about 20 seconds per company (about 30 hours for the universe) and Upstox's
  terms and rate limits for fundamentals are undocumented; the run is deliberately small-batch and stops after
  three consecutive refusals.
* Upstox values are in crore and lack some line items (depreciation, finance costs, borrowings for some
  companies); the report states what is missing rather than filling it.

**Industry research**
* The industry label is the data provider's (Upstox `sector`), granularity varies ("Finance", "Software",
  "Refineries"), and it is not an NSE/BSE or EquityOS classification. The BSE industry list still needs a
  manual download.
* Industry-group growth covers only the companies in the label that have stored annual filings for the same
  two fiscal years on the same basis (at least 5). It describes those companies, not the whole industry; with
  few refreshed companies per label the figure is often withheld with its reason.
* No market-share or industry-size statistic other than the provider's sector market capitalisation (shown for
  context, not used in calculations) is available. The qualitative sector template is general context.
* Growth alpha is relative operating growth, not investment alpha; acquisitions, demergers and restatements
  inside the span are not adjusted for.

**Website enrichment**
* Quotes are the company's own words taken from its own pages (verbatim-checked), so they are marketing-adjacent
  and are labelled "not independent research". Sites that block automated access (Infosys, TCS and others)
  give nothing. A model reading a page can still miss facts; it cannot add any.

**Document uploads**
* Scanned filings depend on Tesseract, which is installed only in the Docker image. Locally, a scan is
  explained and the Excel/XBRL file is recommended. On the real scanned SBI results the denser OCR reads pages
  at about 83% confidence and now yields an income block and a balance-sheet block, but only 2 of 12 income
  columns have a column heading clean enough to use, so most SBI values are still withheld.
* The Nestle results layout is read (all six statements, unit from OCR in Docker). Without OCR the unit is
  asked; without an answer the figures are not accepted.
* Existing vault entries uploaded before these repairs (for example the Nestle and SBI files) keep their old
  status until someone chooses "process again" for them; the repairs do not rewrite stored records.
* Browser tests mock the backend; they check the Documents tab interactions, not the live API.

## Audit findings of 2026-11-13 and their status on 2026-11-14

Fixed (code, tests, and checked on live data; see PROJECT_STATUS.md):

* Fetched documents were stored but never read. The `extract` stage now reads them in the pipeline
  (type check, extraction, promotion), records every outcome, and retries failures.
* `upstox-refresh` alone left companies without validated evidence (117 of 134). The `evidence` and
  `calculate` stages cover this, and `run --select missing-evidence` clears any backlog.
* Document typing: compliance, governance, brochure, certificate and policy files are never financial
  filings (link level and content level). 4 earlier mislabelled files were retyped with audit events.
* Website verification: a parked/dispute page, a page in an unreadable script, or a page that does not
  describe the provider's business is AMBIGUOUS. Re-review withdrew the 3 wrong sites in the audit list
  and 3 more for a person to look at (below). A rejected domain cannot be accepted again.
* Provider labels `DVR`, `Miscellaneous` and similar build no industry group.
* Readiness no longer says "no validated revenue" when the chosen basis stopped reporting and the other
  basis is current (AVSL), and keeps the specific stale reason when a held document is unread.
* An interrupted run left a company half processed with no record. Every stage is checkpointed.

Still limited:

* **Image-only PDFs need Tesseract.** It is installed in the Docker image only. Locally those files are
  recorded NEEDS_OCR and picked up automatically when the pipeline runs where OCR exists (run the worker
  in Docker with the current `app/` mounted, or rebuild the image). Old, low-quality scans can be read
  and still contain no recognisable statements (Aurus Gem's 2013-2016 results).
* **Website verification is still name and description based.** Only a printed ISIN is conclusive (2 of
  the 68 verified sites print one). The business-description check runs only where the provider's profile
  is stored. 55 of the 56 sites verified before the new checks were re-reviewed (Asian Paints could not be reached
  that day and was left as it was); sites found since use the new checks.
  A few are moderate confidence (ABH Healthcare: the page names it as the owner of a division).
  `MANUAL` is the only confirmed state. Three sites withdrawn for review (AAA Technologies, ABM
  International, AVRO INDIA) may be correct; their site did not resemble the provider's business
  description.
* **Provider industry labels are the provider's.** Some look wrong for the company (3B Blackbio Dx is
  filed under "Agrochemicals"); 55 of 149 labels have fewer than 5 members, so most industry-group
  statistics stay withheld by design.
* **Document selection takes what the site offers.** The newest file on a site can be years old (Aurus
  Gem publishes results only up to 2016), and script-driven sites yield nothing.
* **No wall-clock budget per stage.** A slow site with many large files can hold a worker for 20+ minutes
  (observed once in the 25-company run). Other companies continue on other workers; a budget per stage
  is an open item.
* **Upstox is one request stream (about 20 s per company)**, so the full universe is about 30 hours of
  Upstox time however many workers run.
* **The pipeline worker is a separate process** (`python -m app.pipeline.cli serve`); the API only
  records runs. Without a running worker, runs queued from the browser wait.
* **Pause and cancel act between stages**, not inside one: a stage in progress finishes first.
* **Stage rate limits are in-process.** Two workers on two machines would not share the gates.

## Pipeline reliability (2026-11-14)

* Stage budgets stop network and document stages (child process); database-only stages are bounded by a
  best-effort `statement_timeout`. A document that hangs the reader every time is retried only through the
  stage back-off (up to 7 days); the stage does not yet record which document it was reading.
* A run whose worker died is taken over after a 10 minute heartbeat gap, not sooner.
* Scheduled, unattended execution has been exercised for about 25 minutes on 5 companies, not for hours.
* The integrity audit reports 26 stale peer-rank and peer-median calculations in 3 companies (their peer
  inputs were refreshed afterwards). They are kept, not repaired; re-running the sector PEERS and SCORING
  stages for those companies produces current versions.
* Pacing between processes is per machine (lock files in the data folder); two machines would not share it.

* Docker treats `docker kill` (SIGKILL) as a manual stop and does not restart the worker; a crash of the
  worker process (or an out-of-memory exit) does restart it under `restart: unless-stopped`.
* A scheduled cycle of five companies takes about 40 s even when everything is current, because each
  isolated stage that skips still starts a child process.
* Memory and CPU were sampled once, not over time; a long soak should record them.

## Annual-report coverage (CP1, 2026-11-14)

* Inaccessible sources are skipped, never worked around: a site that answers 403 (TCS, Infosys, Hindustan
  Unilever, Nestle India, Godrej Consumer), robots.txt that disallows the files, and files a server refuses
  or fails to deliver (HDFC Bank: HTTP 403 and 524) stay as recorded gaps with a reason code.
* Reports that are only reachable through script-loaded lists or interactive viewers (Reliance, ITC, Maruti
  Suzuki, Britannia, Indian Oil, Hyundai and others) are not found by the static pages or the sitemap.
* A gap is requested from the end user when a report is generated; nobody is asked to download files by hand.

## Statement extraction (CP2, 2026-11-14)

* A table that states no accounting basis is promoted only when a figure in the same document proves it;
  otherwise its values stay extracted but unpromoted. Provider data is never used for this.
* Seven quarterly results (Axis Bank, HCL Tech, Reliance, HUL, ICICI Bank, M&M, Kotak) keep a previous
  extraction made by an older extractor version: a re-read cannot reproduce 6 to 84 of its facts, so it is
  rolled back rather than lose them. The new line items for those documents are not applied.
* Restated comparatives across documents can block an evidence identity check for a period (Wipro FY2023);
  the engine refuses to mix them rather than choose.
* Segment values need a stated or proven basis and a unit; banks' segment tables printed without either remain
  unpromoted (Axis Bank, Kotak).
* Entity confirmation still needs the company's name on the page or cover; reports that name the company only
  by an abbreviation need an alias (added for ONGC, SBI and the former name of Tata Motors).


## Growth and profitability (CP3, 2026-11-14)
- EBITDA, EBIT and gross profit exist only where the filing prints the lines they need (depreciation,
  finance costs, goods-cost lines); other companies show the reason instead. Services companies have no
  gross profit.
- Peer comparisons need at least three peers with the same fiscal years on the same basis; with the
  pilot universe some comparisons are unavailable and say so.
- No industry benchmark: the comparison is with the peer group; growth alpha is not calculated.

## Cash flow and earnings quality (CP4, 2026-11-14)
- Free cash flow and the capex ratios exist only where the filing's cash flow statement was read with a
  capital-expenditure line; others show the reason. Operating cash flow itself comes from the provider
  summary or the filing.
- Banks are excluded from cash conversion and accrual analysis by design.
- Readings (strong/weak, accrual levels) are stated rules of thumb, not accounting conclusions.

## Working capital and financial health (CP5, 2026-11-14)
- Days, net debt, debt-to-equity, ROE and DuPont need balance sheets read from filings for two consecutive
  year ends; many pilot companies have only some years read, so those sections show their reasons.
- Lease liabilities are not included in borrowings or net debt.
- Days use closing balances (not averages) and a 365-day year; services companies have only receivable days.

## Sector indicators (CP6, 2026-11-14)
- Only bank ratios (NIM, NPA, CASA, capital adequacy), vehicle volumes, attrition and utilisation are read from
  documents today. FMCG volume growth, energy production, realisation, throughput and GRM are not extracted,
  so they show "not stated in the stored documents".
- Bank peer comparisons need three banks stating the same ratio for the same period; the pilot set rarely has that.

## Business analysis (CP7, 2026-11-14)
- Quoted topics depend on a stored annual report with page text (none for Maruti, Nestle, Britannia, Godrej,
  Hyundai and IOC at present) and on keyword cues; an annual report states few weaknesses, so that list is often
  short. An empty list means none was stated or detected, not that none exists.
- Segment profit share needs segment results printed in the segment note; many companies have revenue only.
- There is no structured founder/milestone database; history is what the report states.

## Management and governance (CP8, 2026-11-14)
- Board composition, tenure, background and related-party statements need a stored annual report with page text
  (and may be from an earlier year than the figures: the report names its period). Promoter pledges are not stored.
- Buybacks and acquisitions cash lines exist only after a filing is read with the new taxonomy items; dividend
  announcements cover about the last year; no dividend-per-share total or history is derived.
- Related-party amounts, director attendance and remuneration are not extracted.

## Industry and peer analysis (CP9, 2026-11-14)
- No verified industry statistics are held, so industry size, growth and CAGR are unavailable by design; structure is
  measured only among stored companies and is withheld when they cover under 60% of the sector-label group (banks: 18%).
- Peer coverage in the pilot universe is thin: four or fewer peers per company, and several (Reliance, ITC, TVS, ONGC)
  have none. Ranks are withheld below four comparable peers covering half the selection.
- Business similarity uses the traits stored (segments, label, cost structure); companies with few traits are ordered by
  size.

## Valuation inputs (CP10, 2026-11-14)
- No risk-free rate, equity risk premium, beta, cost of equity or growth has been entered for any pilot company, so
  residual-income scenarios are NOT COMPLETED until someone enters them; relative scenarios are unaffected.
- A citation is recorded exactly as given and cannot be verified by the system; market inputs are not fetched
  automatically (no verified automated source is held).
- There is no editing screen in the web frontend yet; inputs are entered through the CLI or the API.

## DCF (CP11, 2026-11-14)
- No DCF assumption has been entered for any pilot company, so no scenario is completed; each report lists what is missing.
- Net debt and borrowings are stored for only some non-financials (BPCL, ITC, TMPV, Wipro among them); without them a scenario
  cannot be completed. Non-controlling interests are not stored for most and are flagged as not deducted.
- The model is a stated five-year, constant-ratio FCFF model, not a bottom-up forecast; no per-year paths or segment builds.

## Relative valuation (CP12, 2026-11-14)
- EV/EBITDA needs net debt and EBITDA (finance costs and depreciation): current EV/EBITDA exists for few pilot companies, so
  peer and relative EV/EBITDA are mostly unavailable; historical P/B needs equity and shares for each year end.
- SOTP needs segment results in the stored segment note (few companies) and an entered multiple per segment; none is entered.
- There is no NBFC-specific model beyond the lender methods (P/B against ROE, residual income); no pilot NBFC exists.

## Verdict back-test (CP13, 2026-11-15)

- The production verdict is not back-tested: score, peer standing and valuation scenarios cannot be rebuilt as of a past date.
  Only a three-component subset with a valuation proxy is tested, and the target, stop and reward-to-risk rules are not tested.
- About three years of prices are stored (Nov 2023 onward): at most ~15 month-end dates at a 12-month horizon, ~21 at 6 months,
  on 24 companies chosen today (survivorship and selection bias), in one market regime, with overlapping windows (observations
  are strongly dependent), no index benchmark (excess return is against the pilot mean), a convention publication lag of 90 days,
  windows with splits/bonuses excluded rather than adjusted, and no costs. The SELL bucket is too small to read.
  Treat the thresholds as documented conventions, not validated parameters, until longer history exists.
- Risk assessment points and bands, the 12-month review horizon and the HOLD band (+/-15%) are conventions.
- Most pilot reports are WATCH because no valuation method completes (no entered DCF/SOTP assumptions, thin peer coverage).

## Report validation findings (CP14, 2026-11-15)

- "Net profit" from the market-data feed includes the non-controlling interests' share (Reliance 80,787 cr against about 69,600 cr
  attributable; Infosys 26,750 against about 26,713). The calculation engine records this in its note and labels such figures
  accordingly, but ratios built on it (and the "attributable" labels in the governance and health sections) are slightly
  overstated for companies with material minorities until the attributable line is read from filings.
- 19 of 24 pilot verdicts are WATCH and risk is "not established" for the same companies. After the closure programme the cause is
  known precisely (see "Post-acceptance closure" below): market inputs are now entered, but for 14 companies no valuation method
  can complete because of STORED-DATA gaps (peers with data, net debt, share counts, three or more years of EBIT, capex and
  depreciation), which assumptions cannot supply. This is a data state, not a defect.
- Single-method verdicts (SBIN, INFY, HCLTECH rest on relative P/E alone) are marked "one method only", halve the gap to the
  target and carry lower confidence; they should be read with that in mind.
- Net debt, share counts and segment results are missing for most non-financials, so EV/EBITDA, SOTP and DCF stay empty for them.


## Post-acceptance closure (2026-11-15)

- **Valuation inputs.** Dated, named market inputs are entered for all 24 companies through the assumption ledger
  (`python -m app.research.setup_cli market`): risk-free rate 5.44% (the 7.31% 10-year yield of 13 Nov 2026, Trading Economics, corroborated by Kotak Neo, less Damodaran's 1.87% India default spread, so country risk is not counted twice; the earlier 7.31% record is kept as superseded),
  India equity risk premium 7.08% (Damodaran, Jan 2026), industry betas (Damodaran India, Jan 2026; industries with fewer than 10
  firms fall back to the all-market beta), and the statutory 25.17% tax rate for non-financials. Betas are industry averages, not
  company regressions. Company-level inputs (growth, margin, capex, depreciation, working capital) are rule-derived from each
  company's own stored years (`setup_cli history`), labelled "EquityOS history rule v1", never as analyst judgement; an input is
  produced only with at least three stored years. Long-run growth and the shift in return on equity cannot be derived from history; for
  ICICIBANK and AXISBANK they were entered by the user (growth 4/6/8%, ROE shift -2/0/+2 percentage points, absolute, bear/base/bull)
  and the bear and bull cost of equity was set equal to the base CAPM value (13.99%), also by the user. All are USER_ASSUMPTION
  records, editable and traceable per company. Every other company has none.
- **DCF cannot complete for any of the 24 on today's stored data.** Closest is BPCL (missing depreciation history and long-run
  growth). Net debt, borrowings or share counts are absent for 19 companies; EBIT, capex or depreciation have fewer than three
  stored years for most. Nothing is assumed in their place. Residual income now completes for ICICIBANK and AXISBANK
  (bear/base/bull justified P/B 0.91/1.13/1.51 and 0.64/0.80/1.07); HDFCBANK and SBIN now have a verified share count and P/B (see the DCF data-coverage section below); KOTAKBANK's P/B is withheld.
- **Verdict distribution explained.** Ten banks and IT companies have a valuation (relative P/E): 2 BUY, 2 HOLD, 1 SELL, 5 WATCH
  (the valuation gap is not confirmed by the other evidence). The 14 others have no valuation: no peer with data (peer sets exist
  but, checked for ITC, none of the peers has ingested statements, and the pilot's FMCG names sit in different industry groups) and
  no completable intrinsic method. No threshold was changed.
- **Back-test.** Still INSUFFICIENT. Prices and statements both start in late 2023, so a bucket holds 1 to 4 independent
  (non-overlapping) observations. Each bucket now also reports worst drawdown, excess return per unit of risk and the round-trip
  cost that would remove its average excess return. No transaction cost is assumed (the data holds none); there is no index
  history, so the benchmark remains the pilot mean. Extending prices alone was not done: the stored statements are no deeper.
- **Upstox.** Authenticated and verified live for eight datasets (balance sheet, cash flow, profile, corporate actions, annual and
  quarterly income, key ratios, shareholding). COMPETITORS fails with UDAPI100011 (a client error, not authentication) and stays
  unverified. Found and fixed: `verify` was answered from a 6-hour URL-keyed cache and could report VERIFIED with an invalid
  token; it now bypasses the cache (an invalid token reports ACCESS_DENIED).
- **LLM.** Groq (openai/gpt-oss-120b) and Gemini (gemini-3.5-flash) both authenticate and answer. A live header-interpretation run
  over four TECHM annual reports recorded the LLM step, promoted nothing and left the evidence store byte-identical. The table-reading
  stage was not separately exercised live. Rate-limit, timeout and fallback
  behaviour are covered by unit tests with fakes, not by live fault injection.
- **OCR.** Real scanned filings are held (337 OCR'd pages, mean confidence 73.7). Re-running an 8-page scanned results filing in
  the Docker image gave page confidence 74-93%, kept page numbers, flagged a 34%-confidence value and promoted nothing (basis not
  stated). Across all 595 OCR-extracted facts, 71 are flagged for review and none is promoted. Only filings already in the store
  were used; no new scanned filings were obtained.
- **Stale prices.** The pipeline already refreshed stale prices; the verdict now also reports how many trading days the reference
  price lags the latest expected close and the report says so on the trade-level table.
- **Not verified.** Gemini and Groq rate-limit behaviour live; Upstox COMPETITORS; any company outside the pilot.
- **Residual-income results sit far below market prices.** For both banks even the bull value (ICICIBANK Rs 818, AXISBANK Rs 789)
  is 40-48% below the price, and the base values are 56-61% below. Under book-value-based residual income with a 14% cost of
  equity (industry beta 1.207), growth of at most 8% and ROE of 12-17%, an Indian bank trading well above book cannot be
  justified; that is the method's answer to these inputs, not a calibration. It drives AXISBANK to SELL (net -3, downside target
  Rs 787, blended with the peer P/E value) and leaves ICICIBANK at WATCH. Read both with the inputs in view; no threshold was changed.

## DCF data coverage and share counts (2026-11-15)

- **New reader: paid-up share counts from the share-capital note** (`docintel/share_counts.py`, run inside document
  extraction and applied to stored annual reports with `docintel reprocess`, which keeps a result only if it loses or changes
  nothing already promoted). A count is accepted only when `count x face value` equals the printed paid-up amount in the
  statement's unit (rounding only), on a page whose issuer is confirmed, on a line that says "paid-up" and is not the authorised
  capital. It is stored as `SHAREHOLDING.total.shares`, as printed at the balance-sheet date; a later split or bonus is applied by
  the corporate-action adjustment, never by the reader. Verified counts: HDFCBANK 15,39,33,68,328 (31 Mar 2026, post the
  1:1 bonus), SBIN 923,06,17,586 (31 Mar 2026), HINDUNILVR 2,349,591,262 and TCS 3,618,087,518 (31 Mar 2026), KOTAKBANK
  1,986,556,582 (31 Mar 2023). HDFCBANK and SBIN now have market cap and P/B. Nothing was estimated.
- **KOTAKBANK's P/B is withheld on purpose.** The only verified count is from March 2023 (face value Rs 5); Kotak split 1:5 on
  14 Jan 2026. The adjustment engine does not apply a bare "1:5" (ambiguous) and holds the price break as REPORTED_ONLY, so the
  old count beside today's price gave a P/B of 0.72, which is wrong. `calc/market.py` now withholds market cap and P/B, and
  withdraws any value stored earlier, whenever a split or bonus after the count's date is not applied. The same open item means
  Kotak's price history is not split-adjusted. A FY2026 annual report for Kotak (not stored) would resolve it.
- **Counts are still missing for 11 pilot companies** (HCLTECH, MARUTI, NESTLEIND, BRITANNIA, GODREJCP, HYUNDAI, TVSMOTOR, IOC and
  others): the stored documents are scanned or are results releases without a share-capital note. The market feed carries EPS but
  no share count; PAT/EPS would be a weighted-average count, a different definition, and was not substituted.
- **DCF inputs from the feed are not available.** The Upstox statements carry revenue, profit, total assets, net worth, EPS and
  cash-flow totals only: no borrowings, depreciation, capital expenditure or finance cost. Those come only from filings.
  Complete stored actuals (revenue, net debt, borrowings, shares) now exist for WIPRO, ITC, TMPV and BPCL; ONGC lacks net debt.
- **DCF still completes for none of the 24.** Even for those, the scenario inputs that remain are analyst-owned or need history that
  is not stored: long-run growth, bear and bull cost of equity, the pre-tax cost of debt, and for most the depreciation, capex
  and EBIT history (fewer than three stored years). No NBFC is in the pilot; lenders use residual income, not FCFF.
- **HDFCBANK and SBIN residual income (entered 2026-11-16)**: user-provided growth 4/6/8 %, ROE shift -2/0/+2 pp, bear and bull
  cost of equity equal to each company's base CAPM (13.99 %). Justified P/B 0.73/0.91/1.21 (HDFCBANK, value Rs 266/333/444 against
  Rs 804) and 0.87/1.09/1.45 (SBIN, Rs 514/643/857 against Rs 1,080). Standalone book value per share reconciles to stored equity
  / stored share count. The residual-income value is far below price for both; the report's value blends it with the peer-P/E
  value, so the verdicts are HDFCBANK SELL and SBIN HOLD. The two methods disagree widely, which the report should be read with.

