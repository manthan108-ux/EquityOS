# Known limitations

Honest list, as of 2026-11-08. Each item says what happens instead of a wrong answer.

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
