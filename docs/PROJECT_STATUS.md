# Project Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture and foundation | Complete |
| 2 | Database foundation | Complete |
| 3 | Company resolution (+ full-universe master import) | Complete |
| 4 | Data acquisition (+ 4b multi-source provider router, coverage engine) | Complete |
| 5 | Document intelligence | Complete |
| 6 | Evidence and validation engine | Complete |
| 7 | Financial calculation engine | Complete |
| 8 | Sector and peer engine | Complete |
| 9 | Research agent system (LLM layer starts here) | Next |
| 10 | Forecasting, valuation and risk | Pending |
| 11 | Investment Committee and PDF generation | Pending |
| 12 | Final testing and deployment | Pending |

## Phase 1 - what works

- FastAPI app with `/health` and `/health/ready` (database check), CORS for the frontend.
- Next.js 16 / TypeScript frontend showing live backend and database status.
- PostgreSQL 17 in Docker Compose; full stack runs with `docker compose up -d --build`.
- GitHub Actions CI for backend and frontend.

## Phase 2 - what works

- SQLAlchemy models + Alembic migrations for exchanges, sectors, industries, companies, securities (listings) and company aliases. See [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md).
- Migrations verified: upgrade, downgrade, upgrade again, and no drift between models and database.
- Idempotent seed of the five-company test universe; ISO 6166 ISIN validator; shared text normaliser.
- Tests run against a real throwaway PostgreSQL database built with the real migrations. The backend container applies migrations on start.

## Phase 3 - what works

- Deterministic resolver (no LLM): ISIN, then BSE code, then ticker, then exact name/alias, then fuzzy suggestions. Ambiguous or fuzzy results are never auto-selected. NSE and BSE listings of one company are one result.
- `GET /api/companies/search?q=` and `GET /api/companies/{id}` (unverified fields are listed, never invented).
- Frontend: search box with suggestions and a company confirmation card (the Start research button stays disabled until the pipeline exists).
- Importer for the official exchange security lists: merges listings by ISIN issuer, idempotent, audited, never deletes or overwrites.

## Phase 4 - what works

- One audited, polite HTTP client: honest User-Agent, robots.txt (RFC 9309), per-host pacing, retry/backoff with `Retry-After`, per-host throttle handling (BSE's HTTP 406), content validation, disk cache, size cap, and a full audit log. Tested with a mock transport and fake clock, no network.
- Providers (see [DATA_SOURCES.md](DATA_SOURCES.md)): NSE equity and SME lists, NSE name/symbol history, NSE and BSE official end-of-day prices, RBI/SEBI feeds, publisher news feeds, company IR link discovery, safe document download and storage (hash-named, magic-byte checked, deduplicated).
- Tables `sources`, `acquisition_log`, `documents`, `eod_prices` (migration 0003). `GET /api/providers/status` reports per-source reliability and the last failure.
- Live results (2026-11-07): the universe is **5,551 companies / 8,068 listings** (NSE list refreshed automatically, including 582 SME companies). 1,065 former names/symbols were added from NSE history, so searching "Zomato" finds Eternal. Official prices are stored for 3 trading days on both exchanges (about 22,000 rows); re-running stores nothing new.
- 170 backend tests.

## Phase 4b - multi-source acquisition (2026-11-07)

Triggered by the risk that exchange APIs would not supply fundamentals. See [DATA_ACQUISITION_STRATEGY.md](DATA_ACQUISITION_STRATEGY.md).

- **Provider router** with a registry (datasets, auth, access method, rate limits, coverage, terms, availability, last retrieval) and **declared vs verified** support. No silent substitution: every route lists all attempts.
- **Adapters:** Upstox Fundamentals (written from the official documentation), market data (NSE/BSE files), NSE, BSE, company IR (now follows investor pages one level and classifies links by file name), regulators, news, government (data.gov.in), manual documents and a strict financial-CSV import.
- **Normalized fact tables** (`financial_facts`, `shareholding_facts`, `corporate_actions`, `ratio_snapshots`, `raw_payloads`, `dataset_verifications`) preserving original value, unit and period; units, periods and fiscal quarters normalized deterministically (`app/normalization.py`).
- **Coverage engine** (`GET /api/companies/{id}/coverage`): required vs available periods, sources, freshness, verification, confidence with reasons, limitations and affected research pillars.
- **Live verification results:** prices (all 5 companies, 247 NSE trading days), NSE lists/history, BSE list, news, regulators verified; company IR works for ICICI Bank and Reliance only (TCS and Infosys 403, HDFC Bank no links). A real 19 MB ICICI Bank annual report and Q2 FY2027 results were downloaded, validated and stored.
- 291 backend tests.

**Upstox was verified live on 2026-11-10** (see the Phase 9 section). Only datasets with a recorded live retrieval are claimed.

## Phase 5 - document intelligence (2026-11-07)

Financial statements are now read directly from official PDFs, so fundamentals exist even without
any API provider. See [DOCUMENT_INTELLIGENCE.md](DOCUMENT_INTELLIGENCE.md).

- Deterministic pipeline (no LLM): PDF text and word positions (PyMuPDF 1.26.5), layout engine
  (lines, numeric columns, rows), statement titles, units (crore / lakh / million / thousand),
  consolidated vs standalone basis, column periods (dates split over lines, fiscal labels,
  "As at"), line-item taxonomy for Ind AS and bank formats, statements continuing across pages.
- Arithmetic validation per column (income, balance sheet, cash flow identities, bank and
  consolidated forms) with rounding-based tolerance; confirm-only component sums.
- Two-stage storage (migration 0005): every value read is staged in `extracted_facts` with page,
  label, header, raw text and position; only values passing all promotion rules enter
  `financial_facts` (provider `DOCUMENT_EXTRACTION`, document and page on each row).
- Safeguards: entity check (a subsidiary's document hosted on the parent's site is refused),
  scanned pages with embedded OCR text promote only check-confirmed values, restatements (newer
  document wins, change noted), in-document conflicts withdrawn, cross-document agreement recorded.
- Router: `DOCUMENT_EXTRACTION` is first for income, balance sheet and cash flow datasets.
  CLI `python -m app.docintel.cli`, API `/api/companies/{id}/documents`,
  `/api/documents/{id}/extraction`, `/api/documents/{id}/pages/{n}`.
- Phase 4 fix found on the way: IR downloads now skip links labelled for a group company
  ("ICICI Prudential AMC: ..." on icicibank.com).
- **Real results:** ICICI Bank Q2 FY2027 results (126/126 checks, 302 values promoted) and FY2026
  annual report (349 pages, 36/36 checks, 81 promoted); 65 of 67 FY2026 values agree across the two
  documents (the 2 differences were a real taxonomy distinction, now fixed). Reliance's scanned
  Q2 FY2027 integrated filing: 72 check-confirmed values promoted, verified by hand against the
  page text. The mislinked ICICI Prudential AMC deck: 0 values (entity check).
- Coverage now (fundamentals): ICICI Bank 38-40%, Reliance 20-25% (income, balance sheet);
  TCS, Infosys, HDFC Bank still have no stored documents.
- 400 backend tests (synthetic page geometry, generated PDFs, and a real-layout bank page).

## Phase 5 stabilization (2026-11-08)

Full report: [PHASE5_STABILIZATION.md](PHASE5_STABILIZATION.md); limits: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

- Local and Docker now share one data folder; storage health check and API.
- Document issuer verification with an audit trail; the ICICI Prudential AMC deck was unlinked with
  your approval; ICICI Bank's own filing lists it as "Subsidiary".
- Half-year / nine-month figures; standalone quarters derived only from exact nested periods
  (ICICI 36 and Reliance 15 half-year figures equal Q1 + Q2, 0 inconsistencies).
- Segments, ratios, schedules, shareholding, group roster and narrative sections extracted and
  validated on the real documents; OCR (Tesseract in Docker) with a second-reading cross-check.
- HDFC Bank discovery fixed (67 documents listed) but its PDFs time out; TCS and Infosys remain
  403. Manual `ingest-inbox` added. PDF licence alternatives tested (pdfminer.six reproduces
  PyMuPDF with no wrong values); not switched.
- 443 backend tests.

## Phase 6 - evidence store and validation engine (2026-11-08)

Details: [EVIDENCE_ENGINE.md](EVIDENCE_ENGINE.md); limits: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

- Evidence store (migrations 0007, 0008): every fact, calculation, assumption and interpretation is a
  record with provenance, authority tier, quality, status and links to what it was built from.
  Facts are mirrored from the Phase 4/5 tables by natural key (nothing is edited; changes create a
  new version and supersede the old); narrative evidence (auditor flags, news) is supported.
- Source authority and conflicts: stronger source wins; equal-authority disagreement over 1% is a
  CONFLICT and no value is used; rounding-aware agreement; wrong-company and failed-issuer documents
  excluded. Quality HIGH / MEDIUM / LOW with reasons.
- Validation rules (balance sheet, income, cash flow, quarters vs cumulative, signs, year-on-year
  jumps, staleness, document discrepancies) with per-rule counts of checks performed and passed;
  ERROR findings block the values involved and everything built on them.
- Ledger for later phases: `resolved_value`, `register_calculation` (engine computes the value,
  units tracked, one basis, one period type), `register_assumption`, `register_interpretation`
  (every number must come from cited evidence), `register_text_fact`, `price_evidence`.
  Every active calculation is recomputed on each run; stale inputs are flagged.
- CLI `python -m app.evidence.cli` (validate, validate-all, summary, findings, resolved, show,
  check-claim) and a read-only API.
- **Real results:** ICICI Bank 1,605 values and Reliance 220 values validated with 0 errors and
  0 warnings (ICICI: 10/10 balance-sheet, 14/14 income, 8/8 cash-flow, 8/8 roll-forward and 36/36
  quarter-sum checks). Two defects found only on real data (shareholding units, cash-flow exchange
  rate line) were fixed. TCS, Infosys, HDFC Bank: nothing to validate (no documents).
- 557 backend tests (114 new for this phase); the cross-source conflict rules have been exercised
  with synthetic data only because no second real provider has supplied fundamentals yet.

## Phase 7 - financial calculation engine (2026-11-08)

Details: [CALCULATION_ENGINE.md](CALCULATION_ENGINE.md).

- 37 fundamental metrics (45 definitions, counting the TTM methods) declared as data (growth, margins, returns, bank ratios, leverage,
  liquidity, cash flow, trailing twelve months by two methods), calculated per basis and period
  through the evidence ledger; missing or blocked inputs become recorded gaps with the reason.
- Corporate-action price adjustment: from stored actions or implied by the exchange's reference
  price (standard ratios only; unexplained gaps recorded, not applied). 25 technical indicators on
  adjusted prices with reproducible, digest-checked evidence; valuation multiples (market cap, P/B,
  P/E on trailing and annual EPS) with explicit adjustment-factor evidence across splits.
- Calculation quality is capped by the weakest input; method-based calculations are recomputed on
  each validation run.
- CLI `python -m app.calc.cli` and a read-only API (`/metrics`, `/metrics/gaps`, `/technicals`,
  `/price-adjustments`, `/calculation-run`).
- **Real results:** ICICI Bank 220 calculations (69 gaps), Reliance 62 (73 gaps), 0 validation
  errors after the run, identical on a second run. ICICI FY2026 consolidated ROE 15.04%, ROA 1.86%,
  profit growth 6.23%, P/E 19.05x (trailing) / 20.7x (annual), P/B 2.90x, market cap about
  Rs 11.25 lakh crore; the bank income identity holds exactly.
- 631 backend tests (74 new for this phase: indicator maths against hand calculations, adjustment
  detection, every ratio against hand-calculated values, bank and non-financial profiles, both TTM
  methods, split/bonus handling, tamper detection, API).
- Not possible yet and not approximated: EBITDA, EBIT, operating margin, interest cover, ROCE
  (depreciation and finance costs are not extracted); Reliance market cap and P/B (no share count);
  corporate actions (none acquired, so only price-implied events are available).

## Phase 8 - sector and peer engine (2026-11-08)

Details: [SECTOR_ENGINE.md](SECTOR_ENGINE.md).

- Peer selection by statement profile, sector, industry and size, with a recorded reason for every
  company left out; comparisons per metric on the period most peers share, one basis per company.
- Curated, versioned, documented sector weight sets (banking, IT services, FMCG, automobiles, oil and
  gas, generic) with a stated reason for every weight. They are project judgement, never derived from
  data and never LLM output.
- Relative standing (share of peers beaten), pillar scores and a weighted overall, all CALCULATION
  evidence linked to the values used and recomputed by validation; thin groups (< 3 peers) graded LOW;
  **no overall score unless 60% of the sector's weight has data**.
- CLI `python -m app.sector.cli` and read-only API (`/peers`, `/scorecard`, `/sector-weights`).
- **A safeguard found on the way (Phase 5 layer):** the first manual-document extraction stored 52
  quarter values for TCS and HDFC Bank as "annual" figures ending 30 June 2025, because a quarter
  column picked up the neighbouring "Year ended" heading and its arithmetic was consistent. A general
  fiscal-alignment rule now withholds any period whose length cannot end on that date in an
  April-March year (12 months must end in March, 6 in September, 9 in December); the values were
  re-extracted and the dependent calculations retired automatically.
- **Real result, honestly:** peers are found (banks and IT services have 2 each, so thin), 6-9
  metrics compared per company, pillar scores for growth, profitability, balance sheet and momentum
  where data exists, **no overall score for anyone yet** (15-45% of weight covered). M&M and Reliance
  have no peer with data. The limit is data coverage (common financial years, share counts, the seven
  companies without values), not the engine.
- 684 backend tests.

## Phase 9 - LLM intelligence and data recovery (2026-11-08)

* **Providers**: Groq (`openai/gpt-oss-120b`) and Gemini (`gemini-3.5-flash`) both authenticate and return
  correct structured JSON, on the host and inside Docker. Retry, backoff, fallback, secret scrubbing and
  malformed-answer handling are tested with mocked HTTP. See [LLM_LAYER.md](LLM_LAYER.md).
* **Recovered with the new documents** (validated, 0 errors): ITC 638 values (consolidated and standalone, Q2
  and H1 FY27, balance sheet, cash flow; quarters-vs-half-year 39/39), HCL 157 (June 2026 quarter, both bases),
  HUL 170 (quarter, half-year, year; quarters-vs-half-year 13/13).
* **Not recovered from documents**: SBI and Maruti (press releases and scans) - now covered by Upstox, below.
* **Engine changes found on real files**: fused dates, garbled rupee units, serial-number columns, heading
  readers and a table reader that takes column assignment from geometry, not from the model.
* **Upstox (live, 2026-11-10)**: token configured; income (annual, quarterly), balance sheet, cash flow,
  profile, key ratios, shareholding and corporate actions were retrieved for all 15 companies and recorded in
  `dataset_verifications`. `COMPETITORS` returns a client error (UDAPI100011) and stays NOT VERIFIED. A bug found on
  real data (two dividends on one date collided on the unique key) was fixed.
* **Upstox -> evidence**: `providers/upstox_map.py` files 12 Upstox line names under the canonical keys, after
  checking each against filing-extracted values (same company, period, basis). "Equity Capital" turned out to be
  total equity. Revenue, total revenue and PAT differ from filings for some companies (ITC: gross vs net of
  excise), so they are filed canonically only where no filing covers that statement/period/basis.
* **Scores (2026-11-10)**: 10 companies now have an overall score (banks: Axis, HDFC, ICICI, Kotak, SBI; IT:
  TCS, Infosys, HCL, Wipro, Tech Mahindra), all at >=65% weight coverage. HUL, ITC, M&M and Maruti have pillar
  scores only (one peer: below the 3-peer minimum); Reliance has no peers. Validation: 0 errors, 0 warnings.
* **Rules added**: a thin peer group (<3) never yields an overall score; a company in the banking sector is
  scored with the bank profile even when no bank-format statement has been read.

## Phase 8 close-out - ready for Phase 9 (2026-11-08)

Finished before the LLM layer starts:

* **Prices**: 512 more NSE sessions loaded (`backfill-prices`), so history runs from November 2023;
  the 200-day average, 1-year return and 52-week range have full windows.
* **Documents**: ZIP kits are unpacked safely; documents are typed by content; issuer checks handle
  exchange addressees, image-only covers and whole-document dominance; font and OCR repairs.
* **Calculations**: EBIT/EBITDA/ROCE/interest cover; shares outstanding derived from paid-up capital
  and the exchange face value (feeds market cap and P/B).
* **Retrieval**: `GET /api/companies/{id}/search?q=` and `docintel search` return pages with document
  and page citations, marked UNTRUSTED. This is what the agents will use instead of opening PDFs.
* **Review queue**: `GET /api/companies/{id}/review-queue` lists withheld scanned values and open findings.
* **CI**: migration drift check and a Docker build (Tesseract present) added.
* **Final run**: calculations, sector analysis and validation re-run for all 15 sample companies:
  0 validation errors (10 companies with statements; 5 have none: SBI, HCL, HUL, ITC, Maruti).
  Banks and IT each have 3 peers; no company has an overall score yet because pillar data covers
  15-55% of the sector weight (minimum 60%). Pillar scores are shown. M&M and Reliance have no peers.
* Tests: 714 passing, ruff clean, `alembic check` clean.

## Research sample (2026-11-08)

Peers and sector weights (Phase 8) need more than five companies, so the work widens to a
15-company sample defined in `backend/app/seeds/sample_universe.py`: banks (ICICI, HDFC, Kotak, Axis,
SBI), IT services (TCS, Infosys, Wipro, Tech Mahindra, HCL Tech), FMCG (ITC, HUL), automobiles
(M&M, Maruti) and Reliance. All 15 already exist in the master data with 247 NSE sessions of
prices (the bhavcopy files cover the whole market). What they lack is documents and a
sector/industry assignment.

Document access was probed with the polite client (robots.txt honoured, no bypass):

| How filings can be obtained | Companies |
|---|---|
| Stored already | ICICI Bank, Reliance |
| Automatic (investor pages expose document links) | Kotak, Wipro, Tech Mahindra, ITC, M&M |
| Manual: download in a browser into `data/inbox/<SYMBOL>/` | HDFCBANK, AXISBANK, SBIN, TCS, INFY, HCLTECH, HINDUNILVR, MARUTI (HTTP 403, robots.txt, or no investor pages) |

Per filing, the most useful are the latest annual report and the last 2-3 quarterly results (the
stand-alone and consolidated results PDF). `python -m app.seeds.sample_loader status` shows, per
company, what is held and the next step; `plan` / `apply --yes` fill only EMPTY sector, industry and
website fields (10 companies) and write an audit row. Folders for the manual companies exist.

### Sample status after the first automatic downloads (2026-11-08)

Approved by you: empty sector, industry and website fields were filled on the 10 new companies
(audited in `master_data_imports`; four reference industries added). Filings were then downloaded
through each company's own investor pages (robots.txt honoured) and run through document
intelligence (Tesseract in Docker for scans):

| Company | Stored | Result of extraction |
|---|---|---|
| M&M | 2 results documents | 470 values pass all checks (consolidated + standalone); calculation run made |
| Wipro | 2 results documents | 36 values pass (prior-year column only; the current quarter did not pass), so the newest period is 2025-09-30 and validation warns it is stale; 27 scanned values await review |
| Kotak | 2 scanned results PDFs + 1 ZIP results kit | nothing promoted: scanned pages with a poor text layer ("KOTAI( MAHINDRA BANI<"), arithmetic checks fail on misread digits; the ZIP is not supported |
| Tech Mahindra | FY2026 integrated annual report (683 pages) | nothing promoted: the cover is an image, so no page can confirm the issuer (53 statements, several pass their checks) |
| ITC | 2 press releases + 2 shareholder-voting PDFs | nothing: press releases carry no statement tables; the voting PDFs were wrongly classified as results (classifier fixed, files relabelled); a results document naming ITC Hotels Limited was correctly stored but NOT linked |

Fixes made on the way: the link classifier no longer treats voting results, postal ballots, AGM
notices, intimations or newspaper advertisements as financial results; OCR text that renders
"rupee in crore" as a lone "f crore" line is now recognised as a unit (OCR text only).

Still to do for the new layouts (next work block, ideally with your manual PDFs): ZIP result kits;
confirming the issuer when an annual report's cover is an image; the full results annexure for ITC;
dense scanned bank statements (Kotak) need a better second reading than Tesseract gives at this
quality, so a text-layer PDF from the exchange is preferable.

### Your manual filings ingested (2026-11-08)

Eight companies' PDFs were ingested from `data/inbox/`. **Documents are now typed by their content
first and their file name only when the content is inconclusive; a file that cannot be typed is
stored as `other` company material instead of being refused** (the issuer check, wrong company,
non-PDF and oversize still refuse). Content typing found that Maruti's and ITC's "results" and
"annual report" files are press releases (relabelled), TCS's three untitled statement files are
results, and HCL's file is a market-research report (stored as `other`).

Values that pass every check, after extraction and a calculation run:

| Company | Values (evidence) | Calculations | Main reason the rest were withheld |
|---|---|---|---|
| ICICI Bank | 1,826 | 220 | (already complete) |
| TCS | 568 | 144 | annual-report statements: reporting unit not printed in a recognisable form, duration not stated |
| M&M | 518 | 47 | - |
| HDFC Bank | 527 | 56 | scanned results: headers that cannot be assigned to a column, misread characters |
| Axis Bank | 586 | 76 | Q1 results: issuer read as another entity; September results: several dates in a header |
| Reliance | 283 | 62 | (already complete) |
| Infosys | 215 | 55 | one validation warning |
| Wipro | 70 | 33 | current-quarter column did not pass |
| SBI, Kotak, Tech Mahindra, HCL, ITC, HUL, Maruti | 0 | - | see below |

Why the seven have nothing yet (recorded reasons, not guesses): **SBI** results PDFs contain no
recognisable statement tables and the annual report cannot confirm the issuer; **HUL** headers
carry no date or more than one duration/date; **HCL** headers carry no date or duration;
**Kotak** scanned pages with a poor text layer; **Tech Mahindra** image-only cover; **ITC** and
**Maruti** supplied press releases, which hold no statement tables (their results annexes, or the
exchange filing PDFs, are needed); **HUL/HCL/SBI** need column-header handling for their layouts.

## Licence gate (2026-11-08)

PyMuPDF is AGPL-3.0. EquityOS stays private until it is replaced (pdfminer.six + pypdfium2 tested)
or deliberately published under AGPL; the report generator must use a permissive library. Details:
[LICENCES.md](LICENCES.md).

## Universe load (2026-11-07)

BSE list imported from the manually downloaded file (5,139 rows, 4,855 usable). 284 BSE rows were skipped and reported: 276 funds/ETFs (INF ISINs), 6 non-equity instruments, and 2 equities with no valid ISIN (A.D.S. Diagnostics 523031, Pushpsons Industries 531562). The 5 pre-existing seed companies were verified unchanged apart from previously empty fields being filled.

## Known limitations

- **Fundamentals exist only where official documents are stored** (ICICI Bank, Reliance). TCS and Infosys block automated access to their sites (403) and HDFC Bank's pages exposed no document links; their documents can be supplied with `ingest-document`, or fundamentals come from Upstox once its token is configured (expected 2026-11-08). Exchange announcements, filings and XBRL have no permitted automated source (protected APIs, not used).
- Still true: announcements, shareholding patterns and XBRL are served by the exchanges' protected APIs, which we do not use. Statements now come from company documents (Phase 5), so coverage varies by company with what each company's website exposes; gaps are disclosed.
- Stored prices are unadjusted for splits and bonuses; corporate actions are not yet acquired.
- The BSE company list still needs a manual download (BSE's API is blocked); the NSE list refreshes automatically.
- BSE historical price files are patchy (BSE removes older files) and prices have only been backfilled for 3 days; deeper NSE history is a longer, rate-limited run.
- News feeds hold only a recent window and matching is a local phrase check; items are leads, not conclusions.
- Fuzzy suggestions can be loosely related when nothing is close; they are only ever suggestions.
- The frontend was not exercised in a real browser (API and build were verified).
- Tesseract is installed in Docker only (Windows needs an administrator install); see KNOWN_LIMITATIONS.md. Redis is not installed (only needed if background workers become necessary).
- Document intelligence reads the primary statements only; notes, segment tables, shareholding and ratios inside PDFs are not yet extracted. Half-year and nine-month figures are staged but not promoted. Real-document validation so far covers ICICI Bank (bank format) and Reliance (Ind AS, scanned); other layouts may need taxonomy additions, which surface as unmatched labels or failed checks, never as wrong numbers.
- One stored document (ICICI Prudential AMC's presentation) is linked to ICICI Bank from Phase 4 and still counts toward ICICI Bank's 'investor presentations' coverage; its numbers are refused by the entity check. It should be unlinked (not done automatically: existing records are not changed without your confirmation).
- PyMuPDF is AGPL-3.0: fine for private use; see ARCHITECTURE decision 23 before offering EquityOS publicly.
- Docker image builds are not part of CI yet.
