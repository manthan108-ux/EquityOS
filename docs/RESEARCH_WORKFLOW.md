# Evidence-aware research workflow (Phase 11)

Guiding rule: **EquityOS never pretends to know what it cannot verify.** When information is
missing it says exactly what, why it matters and what to supply; a missing value is never a zero and
no conclusion is drawn from it.

## 1. Audit of the existing pipeline (what was reused, what was missing)

| Area | Found | Used by the workflow |
|---|---|---|
| Report generator | **None** (the PDF report is a later phase) | New `research/report.py` assembles a structured, evidence-cited report; PDF rendering stays out of scope |
| Retrieval | Provider router (Upstox, exchange files, IR pages), inbox ingestion | `orchestrator` stage SOURCES (opt-in refresh), DOCUMENTS |
| Document intelligence | `store_document_bytes`, issuer check, `extract_document`, ratio/segment/section readers | Every upload goes through them; nothing duplicated |
| Evidence and validation | `sync_company`, `validate_company`, `EvidenceResolution` (selected, unblocked values) | Readiness reads only resolved, unblocked evidence |
| Calculation, peers, scoring | `calculate_company`, `analyze_company`, `score_company` | Stages CALCULATION, PEERS, SCORING; unchanged |
| API / dashboard | companies, evidence, scoring; search -> confirmation -> scorecard | New `/api/research/*`; the dashboard gains the research workspace |

Added (nothing replaced): `app/research/` (requirements, readiness, orchestrator, vault, report,
security, jobs, clock), `app/models/research.py`, migration `0013`, `app/api/research.py`, two
ratio-table items in the document taxonomy (`net_interest_margin`, `casa_ratio`), the frontend
research workspace.

## 2. Requirements registry (`research/requirements.py`, version 1.0)

Seven profiles, **not one list for all**: BANK, FMCG, AUTOMOBILES, IT_SERVICES, ENERGY,
CONGLOMERATE, GENERIC_NONFIN. For each requirement: identifier, label, pillar, importance
(CRITICAL / IMPORTANT / OPTIONAL), the evidence metrics that satisfy it (any one), required period
and maximum age, accounting basis, which documents normally hold it, the report sections it is
essential for, why it matters, what it affects, the official source, any permitted alternative, how
a document can settle it (`extractor`) and, for derived metrics, the inputs they are built from.

Examples of difference by industry: a bank is asked for NIM, gross/net NPA, capital adequacy,
deposits, advances, never for EBITDA or debt/equity; an automaker for operating margin, debt and
cash flow (debt/equity is CRITICAL there but OPTIONAL for IT); energy for segment revenue, debt,
capex; a conglomerate for segment revenue and profit (CRITICAL).

**Honesty about what an upload can do.** `extractor = NONE` marks figures nothing in the system can
read from a document yet (vehicle volumes, attrition, utilisation, FMCG volume growth). They are
listed as unavailable, never requested by upload, and never block research.

## 3. Research readiness (`research/readiness.py`)

Reads only validated evidence. Each requirement is AVAILABLE, STALE (older than its limit) or
MISSING with a reason: not stored, values blocked by validation (rule codes named), or a derived
metric whose named inputs are absent. Where EquityOS already holds a matching document it says so
(`EXTRACT_HELD`, or "held but the figure was not found") instead of asking for it again.

**Readiness score** (0-100, independent of the financial score) from seven components: financial
completeness, document coverage, source reliability, period consistency, validation coverage,
sector-specific metrics, valuation inputs. Weights are per profile. A component that does not apply
(only optional or unreadable sector figures) is left out, not scored zero.

**Eligibility** is not a percentage. AUTOMATIC needs all of: no CRITICAL item missing or stale, score
>= the profile minimum, every component >= the component minimum, no open validation error.
ASSISTED otherwise. BLOCKED when there is neither validated revenue nor profit.

Result on the 24-company sample (2026-11-10): TCS is AUTOMATIC; every other company is ASSISTED
(operating margin / debt for companies whose only source is Upstox, bank-specific figures for most
banks, market capitalisation where shares outstanding are missing).

## 4. Orchestrator (`research/orchestrator.py`)

Stages: IDENTITY, SOURCES (opt-in provider refresh), DOCUMENTS, EXTRACTION, EVIDENCE, CALCULATION,
PEERS, SCORING, READINESS, REPORT. Each is a row in `research_steps` holding a fingerprint of the
inputs it depends on that it does not itself write (documents; documents + extraction runs; fact
tables; validated non-engine evidence). On resume a stage with an unchanged fingerprint is skipped
("already done, not repeated"); a failed stage is retried; a stage left RUNNING by a crash is
retaken after 30 minutes. READINESS and REPORT always run on the current evidence.
Limitation: a change in *another* company's data (peers, benchmarks) does not by itself re-run PEERS
and SCORING; use `force` / a source refresh.

Outcome: AUTOMATIC -> a COMPLETE report is generated. ASSISTED -> the session waits
(AWAITING_DOCUMENTS) and nothing is generated until the user uploads documents or chooses to
continue with what is available.

## 5. Uploads and the vault (`research/vault.py`)

Stages recorded per file: FORMAT (real content: PDF / XLSX / CSV; extension only compared; size,
macro, ZIP, corrupt and encrypted files refused), DUPLICATE (SHA-256), ISSUER (a PDF's own first
page must name the company; another issuer is refused, an unreadable one needs the user's
confirmation), TYPE/PERIOD/BASIS/STANDARD (from content; ambiguity is shown, not guessed), EXTRACT
(existing pipeline, page-level evidence), VALIDATE and CONFLICTS (arithmetic checks; disagreements
with stored values listed, values refused by the extractor reported with reasons), LICENCE.

* Only documents declared as **published filings** feed the shared evidence. A private/licensed file
  is stored in the uploader's vault and used by nobody.
* A user's confirmation of the issuer links the document but cannot make a nameless statement page
  count as the company's: the extractor still refuses it and says why.
* CSV/XLSX tables use EquityOS line-item names (`GET /api/research/template`), need a source
  reference, import all-or-nothing at the lowest source authority, and every ISIN must be the
  company's.
* Vault entries are organised by company, fiscal year, quarter, category, basis and accounting
  standard, with hash, source, timestamp, issuer status, extraction/validation status, evidence
  references and version. Other users see only validated public filings, and only metadata.
* Heavy work runs on a worker pool with its own database session; state lives in the database, so
  lost jobs are re-queued (`jobs.recover`). An accepted upload resumes its research session.

## 6. Reports (`research/report.py`)

Two modes, **same twelve sections**. AVAILABLE_EVIDENCE is generated at once; every gap appears in
its section and in the Limitations block (missing critical/important/optional items, calculations
that could not be made, incomplete valuation, unavailable score, explicit limits on conclusions).
COMPLETE is refused (HTTP 409 with reasons) unless the company is AUTOMATIC-eligible. Each figure
carries evidence id, document and page; the score comes from the stored score run and peers from
the stored peer set; narrative sections are quoted excerpts with their page, never model-written.
Reports are immutable versions; unchanged content does not create a new version.

## 7. API (`/api/research`, API key required)

`POST /sessions`, `GET /sessions` (history, `only_open`), `GET /sessions/{id}`, `/readiness`,
`/missing`, `POST /resume`, `/continue`, `POST /uploads` (multipart, several files), `GET /uploads`,
`GET /uploads/{id}`, `POST /uploads/{id}/clarify`, `POST /reports` (`mode`), `GET /reports`,
`GET /reports/{id}`, `GET /companies/{id}/vault`, `GET /template`, `GET /requirements/{company}`.

Security: `X-API-Key` against `RESEARCH_API_KEYS="alice:secret,ops:secret:admin"` (hashes compared
in constant time, never logged); no keys configured = every endpoint answers 503. Sessions, uploads
and reports belong to their creator (others get 404; admin sees all). Rate limits per client:
30 requests/minute for writes, 20 uploads/hour; 50 MB per file, 10 files per request. The limiter is
in-process (a shared one is needed with several workers).

## 8. Dashboard

Home: access key box, "Your research sessions" (saved incomplete sessions resume). Company page:
Start research -> live progress -> readiness panel (score, components, thresholds, reasons) ->
missing-data assessment (importance, why, affects, document, period, basis, official source, what
EquityOS already holds) with the two choices "generate with what is available" or upload -> upload
panel with per-file validation status, clarifications and conflicts -> report versions and report.

## 9. Research preferences (`research/preferences.py`)

Stored per session (`research_sessions.preferences`) with an append-only log
(`preference_log`), changed by `PUT /sessions/{id}/preferences`. Defaults: **consolidated** basis,
**latest available quarter**, **optional items off**.

* **Basis is the user's and is never switched silently.** Statement figures are used only on the
  chosen basis. If a figure exists only on the other one it is reported MISSING, with the reason and
  the other basis named (`CHANGE_BASIS_OR_UPLOAD`); nothing is borrowed. Bank-level regulatory
  ratios (NIM, NPA, capital adequacy, CASA) are not published per basis and are not filtered by it.
* **Period.** `LATEST_QUARTER` prefers a quarter within 240 days and falls back to the latest annual
  figure only when none exists for that figure; the fallback is labelled on the figure and in the
  readiness reasons. `LATEST_ANNUAL`, `QUARTER` (e.g. Q2 FY2027) and `FISCAL_YEAR` are exact: a
  chosen period is never replaced by a newer or older one and is not judged "stale".
* **History.** A saved session keeps its own preferences and the log of every earlier choice; each
  report records the preferences it was made under, and a changed choice produces a new report
  version while old versions stay untouched.
* **Optional items** are not evaluated at all when off, so they never appear as gaps or affect the
  readiness score; when on they still never block research.

## 10. Bank key figures, operating disclosures and the PDF

* `docintel/kpi.py` reads NIM, gross/net NPA ratio, capital adequacy, CASA ratio, total deposits,
  gross advances (and NPA amounts) from born-digital press releases, key-parameter sheets and
  presentations: tables whose header carries two or more period labels, sentences with an explicit
  "as of <date>", and slide bullets under a heading that names exactly one quarter. A label must
  match exactly (sub-portfolios and different measures are ignored); two measures of one name are
  kept apart by a dimension (domestic / whole-bank NIM, NIM on assets / on interest-earning assets);
  a row with fewer than two period values is skipped; a percentage under an amount label is a
  growth figure and is refused; implausible values, net NPA above gross NPA, and a gross NPA ratio
  that disagrees with printed amounts are dropped with a reason. These pages never go through the
  statement route's arithmetic checks, so they are labelled as key figures in the validation notes;
  scanned pages are not read this way. A release that never names the bank is accepted only after
  the uploader confirms the issuer, and the figures are then marked as such.
* `docintel/operating.py` reads total vehicle sales volume, employee attrition and utilisation from
  sentences that name their own period (quarter, financial year or month). Actuarial "attrition
  rate (for gratuity)", capacity utilisation and utilisation of funds/reserves are refused; variants
  (voluntary / LTM attrition, with / without trainees) are kept as dimensions. Checked on the held
  documents: Maruti's Q2 FY27 press release (696,654 units) and TCS's FY2026 annual report (voluntary
  IT-services attrition 13.7%) are read; TVS's gratuity attrition is not; Infosys and the TCS/Infosys
  quarterly statements held contain no such disclosure, so nothing is read from them.
* `research/pdf.py` (ReportLab) renders a stored report: same twelve sections, gaps in amber boxes,
  citations (page, evidence id), limitations, readiness components, the basis and period used,
  page numbers and the report id/hash in the footer. `GET /api/research/reports/{id}/pdf`.

## 11. Not done / limits


* Only FMCG volume growth still has no reader (`extractor = NONE`; optional).
* Scanned filings: SBI's and HDFC's Q2 FY27 results are scans with a poor text layer; the
  statement route accepts only checked values from them (SBI: none; HDFC: 10 of 417), so their
  interest earned/expended for the quarter are not read. Axis's presentation tables are
  multi-panel slides and are not read (its headline ratios come from the slide bullets).
* Axis's balance-sheet header assignment still refuses its deposits/advances columns.
* NIM and CASA are read only where a document prints them; nothing computes NIM.
* Peer or benchmark changes do not auto-trigger a re-run of PEERS/SCORING.
* In-process job pool and rate limiter (single server).
* Scanned PDFs need OCR (not installed on the host; installed in Docker).
* The scoring registry still lists bank NPA/capital-adequacy as "no source"; validated values do
  exist for ICICI as disclosure evidence (`RATIOS.*`) and are used by the readiness engine and
  `bank_coverage`, not yet by the scorer.
