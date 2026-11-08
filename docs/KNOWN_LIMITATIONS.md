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
* PyMuPDF is AGPL-3.0 (see PHASE5_STABILIZATION.md 2.7). pdfminer.six is a tested MIT alternative
  for text and positions; not switched.
* The API has no authentication and no upload endpoint by design.
* Superseded extraction runs are kept (audit); `document_pages` text of old runs uses disk space
  until you approve pruning.
* CI does not build the Docker images.
* The frontend was never exercised in a real browser.

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

