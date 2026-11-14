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

## Phase 10 - 24 companies, benchmarks and absolute scoring (2026-11-10)

Full description: [SCORING.md](SCORING.md).

* **Sample 15 -> 24**: Nestle India, Britannia, Godrej Consumer, Tata Motors Passenger Vehicles, Hyundai Motor
  India, TVS Motor, ONGC, IOC, BPCL. Identity checked against the NSE master and recorded
  (`seeds/identity.py`): TMPV is the renamed original Tata Motors (CV demerged to TMCV, which is NOT added);
  Nestle changed its year end; Hyundai listed in Oct 2024. Data: Upstox fundamentals for all nine (verified
  live); filings: only a TVS annual report was found, and it has no text layer, so nothing was extracted from it.
* **Peer groups are industry-level** (banks are one group). Reliance is a diversified conglomerate with no
  industry peers. Passenger vehicles (Maruti, M&M, TMPV, Hyundai) is the only new group with 3+ peers.
* **Scoring methodologies**: RELATIVE (Phase 8, unchanged), ABSOLUTE (new, benchmark-based), HYBRID (built, disabled
  until calibration can be justified), UNAVAILABLE (with reasons). Benchmark database, metric definitions, scoring
  configuration and score runs are versioned tables (migration 0012). New API, scorecard on the dashboard.
* **Scores**: ten relative scores unchanged; TMPV relative (0.0, LOW confidence); M&M absolute 77.0 (LOW). Twelve
  companies are UNAVAILABLE for lack of evidence, each with a stored reason.
* **SBI bank-specific coverage**: investigated (`sector/bank_coverage.py`). No source held for SBI provides interest
  earned/expended, deposits, advances, NII, NIM, NPA or capital adequacy; Upstox gives only summary lines. They stay
  gaps; no non-bank metric substitutes. The same NPA/NIM/CAR/CASA gap applies to every bank.
* 805+ tests.

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


## Phase 11 - evidence-aware research workflow (2026-11-10)

Built and tested: requirements registry (7 profiles), readiness engine with a separate readiness
score and multi-rule eligibility, research orchestrator with resumable stages, company evidence
vault with validated uploads (PDF, XLSX, CSV), structured available-evidence and complete reports
with version history, `/api/research` endpoints (API key, ownership, rate limits, size limits,
background jobs), migration `0013`, and the dashboard research workspace. See
`docs/RESEARCH_WORKFLOW.md`.

Also corrected: `sector/bank_coverage.py` looked for the wrong evidence codes (`ASSET_QUALITY.*`)
and wrongly reported that no bank had NPA or capital-adequacy figures. ICICI has validated gross
NPA, net NPA and capital adequacy (`RATIOS.*`); the other banks and the NIM / CASA ratios remain
gaps.

Known open item: Reliance's 1:1 bonus (28 Oct 2024) is not in `corporate_actions` or
`price_adjustments`. It does not affect today's 1-year metrics but would corrupt multi-year price
returns.

### Phase 11b - bank documents, preferences, PDF, frontend tests, operating readers (2026-11-10)

Q2 FY27 documents of SBI, HDFC Bank and Axis Bank were processed through the upload pipeline.
Bank figures now extracted and cited to a page: SBI (press release): domestic NIM 3.11%, gross NPA
1.39%, net NPA 0.36%, CRAR 16.09%, CASA 38.32%, gross advances and deposits. HDFC: NIM 3.2% (assets) and
3.3% (interest-earning assets), CASA 31.6%, gross NPA 1.11%, net NPA 0.37%, CAR 18.9%, gross advances,
deposits, and (consolidated, Q1 comparative column) interest earned/expended. Axis: interest
earned/expended, net profit (standalone and consolidated, Q2), NIM 3.36%, gross NPA 1.16%, net NPA
0.35%, CAR 16.27%. Still missing: SBI interest earned/expended and NII (scanned filing), HDFC's Q2
statements (scan), Axis deposits/advances/CASA, P/B and market cap for SBI and HDFC (shares outstanding).
Also: ReportLab PDF output, research preferences (migration 0014), 49 frontend tests (Vitest), readers
for vehicle volumes, attrition and utilisation. See `docs/RESEARCH_WORKFLOW.md` sections 9-11.

### Phase 11c - statement-parser fixes, price-gap detection, overview (2026-11-10, unattended)

* Statement parser (`docintel/statements.py`, `taxonomy.py`): a balance sheet or cash-flow block now
  ends where numbered notes begin (a note table below it had added a spurious value column); a
  one-phrase "As on 30.09.2026 As on 31.03.2026 As on 30.09.2025" header is split per column when every
  piece repeats the same lead; a bare TOTAL row closes the liabilities or assets half of a bank
  balance sheet (so its identities can check the column); "Employees' Stock Options Outstanding" with an
  apostrophe is recognised. Checked against all 53 stored PDFs before and after: five documents
  changed in column structure (Axis's two balance sheets and Q1 balance sheet, HUL's cash flow now have
  usable columns; ITC's mis-assigned cash-flow block and one scanned balance sheet are no longer
  produced), and bank balance sheets gained recognised rows (totals, ESOP). Nothing already stored
  was changed; the new readers apply when a document is processed again.
* Axis Bank is now AUTOMATIC with a complete report (readiness 88.6): its consolidated balance sheet
  (deposits, advances) validates, so consolidated P/B exists. Because P/B now exists, Axis's Phase 8
  relative score moved from 22.8 to 40.5 (valuation pillar present, weight coverage 100%); no scoring
  rule changed. HDFC, SBI, Kotak, TCS and the other IT scores are unchanged; ICICI moved from 72.5 to 71.2 because it is
  ranked against Axis, whose valuation data changed.
* Price history: a close that falls by a common split/bonus factor while the exchange's reference
  price shows no gap is flagged `PRICE_GAP_ONLY` and carried into the report, never applied. It found
  HDFC Bank (2025-08-26, factor 2.02) and Reliance (2024-10-28, factor 1.99) among the 24 companies.
* `GET /api/research/overview`: readiness of every company with stored evidence, what is critically
  missing, which documents would settle it; shown on the dashboard.

### Phase 12 - basis-safe P/B, verified corporate actions, bank scoring, offline XBRL, browser tests (2026-11-10)

No new documents were available, so everything uses the stored evidence.

* **P/B and accounting basis** (`calc/market.py`): the share count keeps its basis and date; P/B uses
  equity on the same basis or is unavailable with a stated reason. 6 tests (`test_pb_basis.py`).
* **Corporate actions** (`calc/verified_actions.py`, migration 0015): a bonus is recorded only when a
  stored document and the official prices agree. Result on stored evidence: **HDFC Bank 1:1 bonus,
  record date 27 Aug 2025, ex-date 26 Aug 2025** (annual report FY2026 p.295 + price factor 2.0) is
  verified and applied; **Reliance 28 Oct 2024 is NOT verified** (no stored document states a bonus),
  so it stays a flagged suspicion. Raw prices unchanged; `adjusted_prices` holds adjusted bars with the
  events behind them. Reports list applied adjustments with their source. 9 tests.
* **Bank scoring** (`scoring/disclosures.py`, registry 1.2): NIM, gross/net NPA, capital adequacy,
  CASA scored from disclosures with checks; gross NPA and capital adequacy are critical. 18 tests.
  Effect: banks lacking them have no absolute score (stated reason) instead of a misleading one.
* **Offline XBRL** (`docintel/xbrl*.py`, migration 0016, CLI `python -m app.docintel.cli xbrl`):
  see KNOWN_LIMITATIONS: tested on synthetic fixtures only. 54 tests.
* **Browser tests** (`frontend/e2e`, Playwright, in CI): 10 scenarios. Frontend unit tests: 53.
* Also: the CI formatting check was failing on migrations 0013/0014 (unformatted); fixed.

### Phase 13 - automatic evidence fetch from company websites (2026-11-11)
* New `acquisition/evidence_fetch.py`, table `evidence_fetch_runs` (migration 0017, applied), CLI
  `python -m app.providers.cli fetch-evidence --symbol S [--force]` and `set-website`. The research
  background job now runs it inside the DOCUMENTS step; tests and the inline path stay offline.
* Statement reader: side-by-side (two-up) statement pages are split; a duration stated once in the
  heading is applied to the columns (`docintel/statements.py`, `periods.stated_duration`).
* Dashboard progress list explains what the fetch did (downloaded N / refused / nothing found / ...).
* Pilot on the 8 companies that had no document: BPCL 0 -> 4 documents, 355 promoted facts, score
  UNAVAILABLE -> ABSOLUTE 78.58 (readiness 43.2 -> 64.1). Hyundai: 1 presentation. Godrej and Nestle:
  HTTP 403 (recorded). ONGC: robots.txt disallows its document path (respected). Britannia, IOC,
  TMPV: script-driven sites, nothing static to read.
* Tests: 28 new fetch tests, 8 layout tests, 3 + 1 frontend/browser tests.

### Report transformation, phases 1-7 (2026-11-11)

Full description: [REPORT_ENGINE.md](REPORT_ENGINE.md).

* New institutional-style PDF (`research/pdf_pro.py`, `pdf_analysis.py`): cover, executive summary, ten
  sections, charts, source register, methodology. Stored reports and score history are untouched; the PDF
  is a rendering of stored content, and `?style=basic` keeps the old register.
* Analytical layer in `content["analysis"]`: trends and unusual-movement flags, peer benchmarking on a
  consistent period/basis/unit with ranks only from 3+ comparable peers, extractive management
  commentary, historical/peer/intrinsic valuation with scenarios and sensitivity, bull/bear thesis,
  catalysts, risks, monitoring indicators and a nine-question conclusion. No recommendation or target
  price: no validated methodology exists.
* Sector profiles for banks, IT services, FMCG, automobiles, energy and generic non-financials.
  `python -m app.research.compat` builds and renders all 24 pilot companies (24 of 24 without error).
* Tests: report-analysis suite (hand-checked arithmetic, basis and peer-comparability rules, thesis and
  conclusion rules, full Axis PDF), PDF renderer suite.

### Universe coverage, upload repairs and industry research (2026-11-12 to 2026-11-13)

Commits `3868a9f` (ingestion, uploads, enrichment, blank-section rules; migrations 0018-0020) and the work
after it (migration 0021). Architecture and rules: [REPORT_ENGINE.md](REPORT_ENGINE.md). Limits:
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md). Commands to continue: [NEXT_STEPS.md](NEXT_STEPS.md).

**Implementation matrix** (T = covered by automated tests, L = run live)

| Area | Module / command | Status |
|---|---|---|
| Website registry, discovery, verification | `acquisition/website_discovery.py`, `discover-websites`, `website-status` | implemented, T, L (partial coverage) |
| Upstox universe refresh (batch, retry, breaker) | `providers/upstox_refresh.py`, `upstox-refresh` | implemented, T, L (partial coverage) |
| Provider industry label | `providers/classification.py`, `classify`, migration 0021 | implemented, T, L (partial coverage) |
| Universe batch (website, upstox, documents, enrich, evidence) | `providers/universe.py`, `universe` | implemented, T (13 tests), L small batches; tiered `pick_batch` so every run advances |
| Website enrichment (verified quotes) | `research/enrich.py`, `enrich` | implemented, T, L (a few companies) |
| Upload engine: classification, basis/unit questions, private holds | `research/vault.py`, `docintel/*` | implemented, T; Documents tab buttons browser-tested (mocked API) |
| Mid-row-label statements, OCR unit line, denser scan OCR | `docintel/layout.py`, `pdf.py`, `statements.py` | implemented, T; Nestle read; SBI partial |
| Uploads reach facts, readiness and the report | `tests/test_upload_flow.py` | T |
| Industry-group CAGR, company CAGR, growth alpha | `research/industry.py` | implemented, T; shown only when at least 5 comparable group members exist |
| Sections omitted when empty, contents recalculated | `research/pdf_pro.py`, `pdf_sections.py` | implemented, T |

**Fixed in this pass:** the full-suite failure `test_one_companys_crash_does_not_stop_the_batch` (the test
fixture never committed its seed rows, so the crash handler's rollback removed them; the test now commits as
production does and asserts the other companies were saved); a batch ordering bug in `pick_batch` that kept
returning the same failed websites; a robots.txt check that reported unreachable hosts as "refuse automated
access"; a race that crashed a batch when two classification runs overlapped (the write is now one atomic
upsert); upload buttons that showed "CONSOLIDATED" in capitals.

**Measured coverage on 2026-11-13** (of 5,551 listed companies; `universe --status`, `classify --status`)

| Measure | Count | Not done / failed |
|---|---|---|
| Verified or manual official website | 55 (31 verified by discovery, 24 pilot) | 5,496 without; of those tried: 39 failed (mostly guessed domains that do not exist or sites that refuse robots), 2 ambiguous for a person |
| Upstox fundamentals refreshed | 84 (0 errors, 0 mapping failures) | 5,467 not yet processed (about 20 s each) |
| Provider industry label stored | 1,929 | 143 refused because the Upstox profile did not name the company; about 3,480 not yet processed |
| Companies with verified website quotes | 7 | sites that block automated access give none |

Live batches this pass: discovery of 60 (22 verified, 36 failed, 2 ambiguous); classification of about 1,900;
Upstox refresh of 40 more. Full NSE/BSE coverage is not claimed.

### Post-execution audit of the universe pipeline (2026-11-13)

Read-only audit of what the latest runs (`classify --limit 500`, `upstox-refresh --limit 50`,
`discover-websites --limit 50`, `universe --limit 10`) actually produced in the database. Nothing was
modified, deleted or re-scored. Terms: **implemented** = code exists; **executed** = it ran on live data;
**verified** = its output was checked against the stored rows / files; **pending** = not done.

| Component | Implemented | Executed | Verified | Verdict |
|---|---|---|---|---|
| Guessed URLs are never registered | yes | yes (66 failed, 4 ambiguous) | yes: all 66 FAILED rows have no website anywhere; the 4 AMBIGUOUS candidates sit only in the registry, `companies.website` is empty | working |
| Website verification (62 VERIFIED) | yes | yes | **partly**: only 2 verified by an ISIN printed on the page; 60 by company name in title/body. At least 3 are wrong or doubtful (see below) | partial |
| Upstox refresh (134 companies) | yes | yes | yes: 134 REFRESHED, every fact has a source URL, period, unit and basis; no cell is held by two providers | working |
| Upstox facts become research evidence | yes (inside `universe`'s evidence step) | **only 17 of 134** | 117 companies have facts but no validated statement evidence (e.g. ABB India: 312 facts, 0 evidence, readiness BLOCKED) | partial: not part of `upstox-refresh` |
| Provider industry label (2,263 stored) | yes | yes | yes; but 28 companies are labelled `DVR` (a share class, not an industry), 44 `Miscellaneous`, 55 of 149 labels have fewer than 5 members | partial: data quality |
| Document fetch (10 FETCHED runs) | yes | yes | files exist and are readable PDFs, but only 4 runs gave documents worth reading (see below) | partial |
| Document **extraction** of fetched files | yes (`docintel.cli extract`) | **no, not part of `universe`** | 0 of the 21 files fetched in the last two days have an extraction, fact or promoted figure | **broken link in the chain** |
| Readiness and report from fetched documents | yes | yes (3 reports generated) | 0 requirements are satisfied from a pipeline-fetched document; readiness says so ("a stored document of the right type has not been processed yet") | working as designed, nothing to use yet |
| Document-to-company association check | yes | yes | yes: 5 wrong-issuer files were stored unlinked (ICICI Pru AMC, ITC Hotels, BSE letters) | working |
| Resumability after an interrupted run | yes | partly | A B Infrabuild: documents and enrichment saved, evidence step never ran, and nothing records or re-queues it | partial |

**Companies in the latest runs:** 134 Upstox refreshed (44 on 2026-11-12, 90 on 2026-11-13); 132 website
attempts (62 verified, 66 failed, 4 ambiguous) plus 24 manual; 2,263 industry labels; 11 fetch runs on
2026-11-13 (20 Microns to AWL Agri Business, alphabetical, NSE first; the 10 FETCHED runs include the BPCL and Hyundai pilot runs of 2026-11-10).

**Traced end to end**

| Company | Website / IR | Upstox | Documents | Facts and evidence | Readiness | Report |
|---|---|---|---|---|---|---|
| 63 moons technologies (verified, with IR) | `63moons.com`, `/investors.aspx` (name in title + copyright) | REFRESHED, 312 facts, latest FY Mar 2026 and quarter Sep 2026, consolidated and standalone | 3 linked (annual reports FY2025/26, a 2019 results PDF), 2 BSE letters correctly left unlinked | 312 Upstox facts, 360 evidence rows, tier 5 | ASSISTED, 44/100, 6 of 18 available, from Upstox only | 18 pages, mode AVAILABLE_EVIDENCE; none of the fetched documents is cited |
| AVSL Industries (verified, no IR) | `avsl.co.in`, no IR URL | REFRESHED, 216 facts, annual only (quarterly: no data) | 1 file, "CG Report", typed `annual_report`, image-only (2 characters of text), association UNVERIFIED | 216 facts, 237 evidence rows | BLOCKED, 5/100; annual figures flagged STALE because no quarter is stored; message says "no validated revenue or profit" although annual revenue exists | 14 pages |
| ABB India (website discovery failed) | none stored; every guessed domain unreachable, retry 2026-11-20 | REFRESHED, 312 facts, calendar-year Dec 2025 | none (no website) | 312 facts, **28 evidence rows, no statement evidence** | BLOCKED, 34/100, "no validated value" for all statement figures | 12 pages |

**Document usefulness of the 10 FETCHED runs** (files opened and read)

* Useful: BPCL (4 files, all extracted earlier by the manual command), 63 moons (2 annual reports),
  360 ONE (Q2 FY27 results update), 20 Microns (July 2026 presentation).
* Readable but old or partly image-only: A B Infrabuild (2020-21 annual report, results of 2019 to 2024, one
  garbled text layer, one image-only), Aurus Gem (annual reports 2017-18 and 2022-23 issued as *Lypsa Gems*;
  results from 2013 to 2016, four image-only).
* Mislabelled or empty: AVG Logistics ("Corporate Brochure" stored as `quarterly_result`), AVSL ("CG Report"
  as `annual_report`), AWL (two "Secretarial Compliance Report" files as `annual_report`, no text layer),
  Hyundai (a one-page notice). Cause: landing-page PDFs inherit the page's kind and
  `_NOT_AN_ANNUAL_REPORT` / `_NOT_FINANCIAL_RESULTS` in `providers/company_ir.py` have no secretarial,
  compliance, governance, brochure terms.

**Wrong or doubtful website verifications (not changed; a person should review):** AGI Greenpac ->
`hsil.com` (the page title is a domain-dispute ruling, so the domain does not appear to be the company's), ACS Technologies ->
`acstechnologies.com` (a church-software company, not this one), AA Plus Tradelink -> a page titled in Bengali about a
"live game", ABH Healthcare -> a page titled "Samsunn Health Care", AMBANI ORGOCHEM -> "Ambani Organics".
None has been fetched or enriched yet, so no evidence is contaminated.

**Failures and skips (from the database; the latest four commands left no log files)**
* Websites: 66 failed after 538 candidate URLs; 368 (68%) were guessed domains that do not resolve, 117 pages
  did not mention the company, 32 refused access, 19 were blocked by robots.txt. Retries are scheduled
  (60 on 2026-11-20); the 4 ambiguous need a person.
* Documents: TCS, Infosys, Nestle and Godrej answered HTTP 403 (not bypassed); Britannia, IOC, Tata Motors PV,
  3i Infotech, 5Paisa, AVRO found nothing (script-driven pages); ONGC's files are blocked by robots.txt.
* Upstox: 0 errors, 0 mapping failures, no retries needed.
* Duplicates: none found (unique file hashes, no ISIN shared between companies, no website shared).
  Stale: 1 company with its newest fact older than 15 months.

**Tests:** full backend suite re-run after the audit, 1,313 passed, 0 failed (9 min 1 s). The audit itself added no code and no tests; it changed documentation only. The gaps above have no failing test because the tests assert the designed behaviour (download, validate, report) and none covers "a fetched document is extracted".

### Integrated pipeline: documents, evidence, calculation, checkpoints, schedule (2026-11-14)

Answers the audit above. Architecture, commands and API: [PIPELINE.md](PIPELINE.md). Open limits:
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md). Not committed or pushed. Migration 0022 is applied
(upgrade only).

Terms: **implemented** = code exists, **executed** = ran on live data, **verified** = its output was
checked in the database or the generated report, **automated test** = covered by tests.

| Stage / capability | Implemented | Executed (live) | Verified | Status |
|---|---|---|---|---|
| `extract` in the pipeline (type check, read, promote) | yes | yes: 1 company, then 10, then 20+ more | 63 moons: 3 of 3 read, 14 figures promoted, 2 readiness requirements and the report now cite the documents (id and page) | working |
| Document typing | yes | yes | 4 mislabelled files retyped with audit events (AVG brochure, AVSL CG report, AWL x2 compliance); wrong-issuer files stay unlinked and unread | working |
| OCR for image-only PDFs | uses existing OCR | yes, in Docker (`app/` mounted): 2 + 5 companies | scans read (A B Infrabuild, 5 companies of the 25-company run); Aurus Gem's 4 old scans read, no statements in them; none left NEEDS_OCR | partial: works only where Tesseract is (Docker) |
| `evidence` for provider facts | yes | yes: 117-company backlog cleared | companies with statement evidence 41 -> 158, missing 117 -> 0; revenue from a document equals Upstox's figure to the rupee (63 moons, Rs 211.59 crore) | working |
| `calculate` (derived metrics) | yes (new stage) | yes: 35 companies | growth etc. appear in readiness; found and fixed a calc bug on list-style bonus data | working |
| Readiness accuracy | yes | yes | AVSL and ABB India: reasons now state the real cause; ABB BLOCKED -> ASSISTED 44.0 | working |
| Website verification checks | yes | yes: 61 of 62 re-reviewed (1 unreachable that day) | 3 withdrawn from the audit list, 3 more for a person; ABH kept (moderate); Ambani confirmed (matches profile) | partial: name/description based |
| Industry labels | yes | yes | `DVR`, `Miscellaneous` no longer build groups | working |
| Stage checkpoints, retry, resume | yes | yes | live run paused mid-company and resumed: finished stages not repeated; 2 failed stages retried and recovered | working |
| Concurrency guard | yes | yes (found by a live collision) | two runs cannot start the same stage of a company | working |
| Central orchestration (10 stages) | yes | yes: 5, 25 and 2+2 company live runs | see below | working |
| Scheduler, worker, API, UI | yes | scheduler/API/UI by tests only | the worker executed queued runs (live); the schedule was not left enabled | partial: no unattended run yet |
| Report regeneration | yes | no (no saved report existed for a processed company) | automated test: new version only when inputs change | implemented, not executed live |
| Per-stage time budget | no | - | one stage ran 33 minutes on a slow site | open |

**Live runs** (all against the real database and the real sites, robots.txt obeyed, nothing bypassed)

| Run | What | Result |
|---|---|---|
| 63MOONS | extract, evidence, readiness | 3 documents read; readiness 44.0 -> 55.3 |
| backlog | evidence, readiness for 117 companies | 117 of 117 done in about 2 minutes; 0 missing now |
| 5 companies, every stage | paused from another process mid-company, then resumed by the worker | completed: 5 of 5; the resumed company continued from `upstox`, earlier stages not repeated |
| 25 companies, every stage, 3 workers | next batch of never-processed companies | completed in 50 minutes: 25 of 25 reached readiness; 10 websites verified, 15 not found (documents correctly skipped, nothing guessed); 8 companies fetched 2 to 7 documents each; 7 companies' documents read; 2 `calculate` failures (a real bug in `calc/adjust.py`, fixed) recovered on retry; 0 failed stages now |
| unread documents | extract, evidence, calculate, readiness for 10 companies, then OCR in Docker for 2 + 5 more | of the 83 documents fetched since the audit: 49 read, 21 contain no statements (mostly presentations), 13 deliberately not read (4 retyped as not financial, 9 wrong-issuer and unlinked), 0 need OCR; 0 companies with unread documents |
| 2 never-refreshed companies | classify, upstox, evidence, calculate, readiness | fresh Upstox refresh and everything after it in one run |

Not run live: a scheduled (unattended) run, and report regeneration for a saved report. The 25 companies
all landed as ASSISTED, none AUTOMATIC: most small companies have only standalone statements (readiness
defaults to the consolidated basis and says so) or too little recent data. Readiness measures
evidence, and the pipeline does not raise it.

**Company-level evidence** (the three audit cases)

| Company | Before | After |
|---|---|---|
| 63 moons technologies | 3 fetched PDFs unread; readiness 44.0; 18 pages | all 3 read (96/188/109 facts); 55.3; 20 pages; revenue, total assets etc. cite the annual report page |
| ABB India | 312 facts, 28 evidence rows, BLOCKED 33.8 ("no validated value") | 312 facts + statement evidence; ASSISTED 44.0 (6 of 18); 16-page report |
| AVSL Industries | BLOCKED 5.3, "no validated revenue or profit" and "document not processed" | ASSISTED; reason: consolidated figures last reported 31 Mar 2018, standalone exist for 31 Mar 2026; the CG report is retyped as not financial |
| Aban Offshore | 5 documents unread | all read; its report draws revenue, assets, equity and cash flow from the FY2026 annual report (pages 113-115) |

**Tests (final, run alone: concurrent test runs share one test database and corrupt each other)**

| Check | Result |
|---|---|
| Backend `pytest` (whole suite) | 1,405 passed, 0 failed (was 1,313; +92 new, no existing test weakened or removed). It includes migration down/up on the isolated test database, model-versus-migration drift, PDF generation and the one-module-imports-pymupdf rule |
| Backend `ruff` check and format | clean |
| Frontend `tsc`, `eslint` | clean |
| Frontend `vitest` | 59 passed (+10 for the pipeline page) |
| Playwright (mocked backend, existing dev server on port 3000) | 21 passed (+4 for the pipeline page) |
| Live runs | 63MOONS; 117-company backlog; 5 companies (paused and resumed); 25 companies; 10 + 7 companies' documents; Docker OCR |

The full suite caught one real defect during this work (a new module imported `pymupdf` directly,
which the architecture test forbids); the live 25-company run caught two others (a calc-engine crash on
list-style corporate-action details, and two runs colliding on one company). All three are fixed and
covered by tests.

**Earlier flaky failures (migration, resolver):** one cause confirmed. Running two pytest processes at
the same time shares `<database>_test` and produces hundreds of errors (seen again this session); run
alone, the suite passes every time. Run one pytest process at a time.

### Milestone 1: pipeline reliability (2026-11-14)

Roadmap change: Milestones 2 (universe coverage) and 4 (AI copilot) are frozen; Milestone 1 (reliability)
and Milestone 3 (institutional-quality research) are the priorities. Nothing is committed or pushed.
Migration 0023 is applied to the development database (upgrade only).

| Item | Implemented | Executed (live) | Verified | Status |
|---|---|---|---|---|
| Per-stage time budgets, child-process isolation | yes (`pipeline/isolation.py`) | yes: a real stage stopped at a 1 s budget; extract/documents ran as child processes in the Docker worker | killed at budget, helper processes stopped too, exit codes reported, later stages still run | working |
| Cross-process request spacing | yes (`acquisition/pacing.py`) | worker ran children against real sites | two processes wait their turn; stale locks recover | working |
| Read-only evidence-integrity audit | yes (`evidence/integrity.py`, CLI, API) | yes, 69,636 evidence rows | 13 planted-defect tests; found a real issue class (26 stale peer-rank calculations in 3 companies, left untouched) | working |
| Monitoring: stage durations, health alerts | yes (`pipeline/metrics.py`, CLI, API, `/pipeline`) | yes | `WORKER_DOWN` raised within 3 minutes of a killed worker and cleared after recovery | working |
| Worker heartbeat, run heartbeat thread | yes | yes | a long stage no longer looks like a dead run | working |
| Schedule scope (selected companies only) | yes | yes: 5 pilot companies | enabling without companies is refused; the whole universe needs `--allow-universe` | working |
| OCR deployment | image rebuilt, `ocr-check`, Docker `worker` service | yes | Tesseract 5.5.0 read a self-drawn scan (figures recovered, 95% confidence) in the rebuilt image without a code mount | working |
| Report regeneration | yes | yes: 4 saved reports (Axis Bank, Infosys, Nestle India, TCS) | originals untouched (13 of 13 identical id/hash), new versions only when inputs changed, forced rerun writes nothing, PDFs render (18 to 27 pages) | working |
| Unattended scheduled run | yes | yes: 4 scheduled cycles in the Docker worker, plus a hard kill mid-run | see below | working on a 5-company scope; not run for hours |

**Unattended test** (Docker worker, schedule every 5 minutes, ICICIBANK, AXISBANK, INFY, NESTLEIND, TCS, all
10 stages): run 18 did the real work (Upstox refresh, 4 new Axis Bank documents fetched, 1 document set read,
evidence, calculation, readiness) in 139 s; run 19 skipped what was current (Upstox, evidence, calculation,
classification) and regenerated 4 reports; the worker container was then killed mid-run (run 20), health went
CRITICAL (`WORKER_DOWN`, exit code 2), and a restarted worker took the run over after its 10 minute heartbeat
limit and finished in 29 s with no stage repeated; run 21 showed `enrich` and `website` no longer repeated.
0 failed stages in every run. The schedule is disabled again and the worker stopped.

**Defects the live test found and fixed:** the scheduler's stored stage list had gone stale (it lacked
`calculate`: a schedule now means every stage); `enrich` and `website` repeated network and model work every
cycle when nothing was stored (stage-level cooldowns, 30 days); the report stage printed a skip reason one
character at a time; a first audit lookup ignored the company (source keys are unique per company) and
reported 44,000 false mismatches.

**Known open items:** 26 stale peer-rank/peer-median calculations in 3 companies (documented limitation:
other companies' changes do not re-run PEERS/SCORING); a document that always hangs the reader is only handled
by stage back-off; the database `statement_timeout` for DB-only stages is best effort; scheduled runs have been
exercised for about 25 minutes, not hours.
