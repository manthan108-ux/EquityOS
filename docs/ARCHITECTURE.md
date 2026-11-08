# Architecture

EquityOS is a modular monolith: one FastAPI backend, one Next.js frontend, one PostgreSQL database. The project ends at PDF report generation; there is no post-report monitoring.

```
Next.js UI ──► FastAPI API ──► Orchestrator ──► research agents (parallel where independent)
                                  │                 │ request missing data
                                  │                 ▼
                                  │       Acquisition layer ─► Providers (NSE, BSE, IR sites, regulators, market data)
                                  │                 ▼
                                  │       Document intelligence ─► Evidence DB (provenance on every value)
                                  ▼                 ▼
                      Financial engine (deterministic) · Sector weights · Peers · Forecast/Valuation/Risk
                                  ▼
                      Red-Team ─► Investment Committee ─► PDF report
```

## Non-negotiable principles

1. **Deterministic where it matters.** Financial arithmetic, company resolution and sector weights are plain, tested Python. LLMs interpret evidence and return typed (Pydantic) output only.
2. **Central evidence.** Agents never fetch the web themselves; they request data through the acquisition layer so every fact is stored with provenance (source, document, page, period, unit, method, confidence).
3. **No fabrication.** Missing data is reported as missing, never as zero or an invented value.
4. **Visible failure.** A failed module marks the report sections that depend on it; its output is never invented.
5. **Standalone vs consolidated and reporting periods are never mixed** without an explicit methodology.

## Current layout

```
backend/app/
  main.py              FastAPI app factory, CORS
  api/                 health, companies (search, coverage), providers (status), documents
  core/                settings, database, ISIN validation, name normalisation
  models/              SQLAlchemy models: company master, acquisition, facts, docintel
  services/            deterministic company resolver + cached search index
  master_data/         exchange security-list importer (NSE/BSE)
  acquisition/         PoliteClient (robots.txt, pacing, retries, cache, audit log) + CLI
  providers/           provider router, registry, adapters (Upstox, NSE/BSE files, company IR,
                       regulators, news, data.gov.in, manual), persistence, CLI
  docintel/            Phase 5 document intelligence: PDF text, layout, periods, units,
                       line-item taxonomy, statement extraction, arithmetic checks, pipeline, CLI
  coverage.py          Data Coverage Engine
  normalization.py     units, amounts, fiscal periods
backend/migrations/    Alembic migrations 0001-0005 (schema changes only happen here)
frontend/src/          Next.js 16: company search + confirmation, system status
```

Folders for later phases (providers, evidence, agents, ...) are created when a phase needs them, not as empty placeholders.

## Decision log

| # | Decision | Reason |
|---|---|---|
| 1 | PostgreSQL via Docker Compose, host port **5433** | Reproducible; avoids clashing with a locally installed Postgres on 5432. |
| 2 | Health endpoints split into liveness and readiness | Liveness must not fail when the DB is down; readiness reports 503 and exposes only the exception class name, never connection details. |
| 3 | Frontend status panel is a client component | The browser calls the API directly, which also proves CORS. Next 16 `cacheComponents` requires uncached server fetches to be wrapped in Suspense. |
| 4 | Backend dependencies pinned in `requirements*.txt` | Reproducible builds and CI. |
| 5 | LLM layer: provider-independent interface, Groq primary, Gemini fallback, **free tiers only**, no automatic switch to paid models; pause and notify when limits are exhausted; track tokens per report. Introduced in Phase 9. Phases 1-8 do not use an LLM. | Budget constraint of INR 0 for inference. |
| 6 | PDF engine: HTML/CSS templates (Jinja2) rendered to PDF with headless Chromium (Playwright); Plotly charts embedded as images/SVG. Final confirmation in Phase 11. | Best typographic and layout control (running headers/footers, page numbers, tables) with maintainable templates; WeasyPrint needs native GTK libraries on Windows and ReportLab is laborious for table-heavy layouts. |
| 7 | Companies and securities are separate tables; canonical company ID is a UUID | One company, many listings (NSE/BSE); no duplicate companies; IDs survive ticker changes. |
| 8 | DB tests use a real throwaway PostgreSQL database built from the real migrations | The schema relies on Postgres features (regex CHECKs, UUID defaults); testing migrations is testing the schema. |
| 9 | Synchronous httpx client behind one PoliteClient | Simple and testable with a mock transport and fake clock; per-host concurrency is deliberately limited anyway. |
| 10 | Robots.txt obeyed strictly (RFC 9309); 401/403/418 are final; honest User-Agent | Project rule. A source that forbids automated access is dropped, not worked around (Yahoo, Google News RSS and Wikidata were dropped for this reason). |
| 11 | Content validation on every fetch | Some sites answer HTTP 200 with an error page when throttled; without validation that would be stored as data. |
| 12 | Official static exchange files are the primary market-data source | Public, authoritative (tier 1) and fetchable without defeating protections. Prices are stored unadjusted with file hash and URL. |
| 13 | Providers return proposals; writes are explicit | Discovery (IR links, name history) never silently overwrites verified data. Name history only adds aliases, each with a recorded source. |
| 14 | Provider router + registry; declared vs verified support; every attempt returned | No single source is assumed to supply everything; a fallback is never silent; support is claimed only after a recorded live retrieval. |
| 15 | Authenticated documented APIs are called with `documented_api=True` (robots.txt skipped), credentials only in headers or redacted query parameters | robots.txt governs crawlers, not contractual APIs; access denials stay final; tokens never reach logs, cache keys, stored URLs or payload archives. |
| 16 | Coverage engine counts only data that exists; confidence is a category with reasons | Completeness must not be inflated; a quality category must not be read as a probability. |
| 17 | Manual routes (CSV, documents) are first-class, all-or-nothing and recorded as USER_SUPPLIED | Coverage can be completed legitimately where no API reaches, without pretending the data is provider-verified. |
| 18 | Financial statements are read from official PDFs by a deterministic layout engine (word positions -> lines -> numeric columns), not a generic table detector and not an LLM | Indian statements are whitespace-aligned, not ruled grids; positions plus strict label patterns are reproducible and testable. An LLM must never read or "fix" numbers. |
| 19 | Two-stage storage: every value read is staged in `extracted_facts`; only values passing all promotion rules enter `financial_facts` | Nothing read is lost or hidden, and nothing unproven is used. Each refusal has a recorded reason. |
| 20 | Arithmetic identities validate each statement column (rounding-derived tolerance); a column is trusted only if a check passed in it and none failed | Catches misread columns, wrong units and OCR/parsing errors without external data. |
| 21 | Periods require one month-end date plus an explicit duration (or "As at"); half-year and nine-month columns are staged, not promoted | Mislabelling a six-month figure as a quarter would silently corrupt every ratio; refusing is safer than guessing. |
| 22 | Document extraction is ranked first for statement datasets in the router | Company filings (tier 2) outrank aggregators; the router still reports every attempt. |
| 23 | PDF backend: PyMuPDF 1.26.5 (no dependencies; isolated in `app/docintel/pdf.py`). OCR only through an installed Tesseract | Fast and accurate word positions. **Licence: AGPL-3.0** (or commercial): fine for private research; if EquityOS is ever offered publicly as a network service, its source must be made available under AGPL terms, or the backend can be swapped (pdfminer.six / pypdfium2) without touching the rest. PyMuPDF 2.x was not used: it adds ML dependencies (onnxruntime, numpy) we do not need. |
| 24 | One `DATA_DIR`, bind-mounted into Docker; the database stores relative paths only | Local runs and Docker must see the same documents; a named volume silently diverged. |
| 25 | A document's issuer is read from its own first page; link changes are audited (`document_link_events`) and applied to existing records only with approval | Websites host subsidiaries' documents; coverage and numbers must not be attributed to the wrong company. Parent/subsidiary is quoted from the company's own related-party note, never inferred. |
| 26 | Extraction runs are superseded, not deleted; each runs in a savepoint | Audit history is kept, one bad document cannot corrupt a batch. |
| 27 | Cumulative (H1, 9M) figures are stored as reported; standalone quarters are derived only from exactly nested periods and stored as a separate provider with their inputs | Reported figures are never overwritten; a derived number must be reproducible. |
| 28 | OCR: Tesseract by command line with word confidence; scanned values need an arithmetic check AND an agreeing independent reading; doubtful values are flagged for review | OCR returning a number is not evidence. |
| 29 | Disclosure tables (segments, ratios, schedules, shareholding) are validated disclosures in `extracted_facts`, not rows of `financial_facts` | Different shape (dimensions, mixed units); served by their own API; Phase 6 can promote them to evidence. |
| 30 | Evidence is mirrored from the fact tables by natural key and content hash, never edited; changes create a new record and mark the old one SUPERSEDED | A calculation or sentence that cited a value keeps pointing at it, and validation can say the input is no longer current. Row ids are unusable as identity because Phase 5 re-inserts rows on every extraction. |
| 31 | Source authority resolves disagreement only when one source is strictly stronger; equal authority that disagrees beyond 1% is a CONFLICT and no value is chosen | Picking "the newer" or "the larger" of two equally authoritative figures would be a guess. |
| 32 | Calculations are computed by the engine from a formula over evidence ids, in exact decimals, with unit tracking; callers never supply the result | Keeps LLM agents (Phase 9) from supplying numbers; every calculated value can be recomputed and is, on each validation run. |
| 33 | An interpretation must cite evidence and every figure in its text must match a cited value (allowing the writer's rounding) | The methodology rule "an interpretation may never introduce a number" is enforced mechanically, not by prompt. |
| 34 | ERROR findings block the values involved; later phases read `evidence_resolutions` / `resolved_value`, never the raw fact tables | One place applies authority, conflicts and blocking. Blocking propagates to dependants but stays narrow (a broken calculation does not block the sound facts it used). |
| 35 | Narrative evidence (auditor flags, news) is a FACT with a statement instead of a number | Phase 9 must cite qualitative sources with the same traceability as numbers. |
| 36 | Every calculation is registered through the evidence ledger from resolved evidence, per basis and per period type; missing or blocked inputs produce a recorded gap, never a default | Calculated numbers carry lineage, are recomputed on every validation run, and "missing is not zero" holds for ratios as well as facts. |
| 37 | Metrics are declared as data (formula, named inputs, variants, preconditions, profile) | A reader can audit every definition in one file; the definition used is stored with each result. EBITDA/EBIT/ROCE are absent until their inputs are extracted. |
| 38 | Trailing twelve months is computed two ways and both are kept | Quarter sums and year + YTD - prior YTD must agree; a disagreement is a CONFLICT instead of one method silently replacing the other. |
| 39 | Prices are adjusted only for splits, bonuses and consolidations; events come from stored corporate actions or are implied by the exchange's reference price and must match a standard ratio within 0.5% | Mismatches that fit no ratio (a missing session) are recorded and NOT applied. Dividends and rights are not adjusted. |
| 40 | Technical indicators are CALCULATION evidence that stores a digest of the exact adjusted prices used and are recomputed by their method on each validation run | Hundreds of price rows cannot be linked individually; the digest still detects any change. |
| 41 | A statement value is brought across a split or bonus by an explicit adjustment-factor evidence record | A P/E or market cap that spans a share-structure change shows the factor and the events behind it. |
| 42 | The research sample is 15 companies (5 banks, 5 IT services, 2 FMCG, 2 automobiles, Reliance), defined in version control; sector/industry in it are curated starting points written only into empty fields | Peers and sector weights need several companies per sector. The real sector assignment stays with the Phase 8 engine; existing values are never overwritten and every change is audited in `master_data_imports`. |
| 43 | PyMuPDF (AGPL-3.0) stays while the project is private; no deployment or sharing until it is replaced or the project is deliberately published under AGPL; it is imported in one module only; the Phase 11 report generator must use a permissive library | See [LICENCES.md](LICENCES.md). Keeps the licence decision a one-file change. |
| 44 | A peer must share the statement profile and the sector; same industry first, then closest size; every exclusion is recorded with its reason | A bank is never compared with a manufacturer; the choice of peers is auditable. |
| 45 | Sector weights are curated, versioned, documented per pillar and labelled as project judgement | The specification forbids invented sector weights; making the judgement explicit and reviewable keeps it honest. A change is a new version. |
| 46 | A score is relative standing among peers (share of peers beaten), pillar scores average ranks, the overall is weighted over pillars with data, and **no overall is published below 60% weight coverage** | A single number summarising two of six pillars would mislead. Thin peer groups (< 3) are graded LOW. |
| 47 | Peer statistics and scores are evidence linked to the values they used and recomputed by validation; a changed or withdrawn input blocks them and everything above | Comparisons stay true when any company's data changes. |
| 48 | Period length must fit the period end under the April-March fiscal assumption (12 months ends in March, 6 in September, 9 in December); a column that violates it is withheld | Found on real documents: a quarter column that picked up "Year ended" was stored as an annual figure with consistent arithmetic. Arithmetic checks cannot catch a mislabelled period. |
| 49 | Calculations made by the engines whose inputs are no longer current are retired automatically; claims and assumptions made by others are only flagged | Re-extraction can withdraw inputs; derived numbers must not linger as active evidence. |
| 50 | A ZIP of filings is unpacked into its PDF members (magic-number check, member count/size limits, safe names, encrypted and nested archives skipped); documents are typed by what they contain, never refused by file name | Found with supplied filings: results kits arrive zipped; a user-verified file must not be rejected because of its name. |
| 51 | A document is attributed to a company from its cover and first pages (addressee exchanges such as NSE/BSE ignored, window widened for image-only covers); a whole-document dominance rule catches a file that is mostly about a different issuer | A subsidiary's or a rival's report must not be filed under the wrong company. |
| 52 | Shares outstanding are derived (paid-up equity capital / face value from the exchange's own security list, stored as a static FACT) and carry the capital's rounding; a company without both has no share count and market cap states why | The share count is not printed in a statement table; a derived figure with its inputs is better than a guess and better than none. |
| 53 | Page text is searchable with PostgreSQL full-text search that returns pages with document and page number, excludes mismatched documents and superseded runs, and marks every snippet UNTRUSTED | The Phase 9 agents get a deterministic, citable retrieval layer; document text can never become an instruction. |
| 54 | CI checks migrations against the models (`alembic upgrade head`, `alembic check`) and builds the backend Docker image (Tesseract present, app imports) | The OCR image is part of the product; a broken build should fail before it reaches the machine. |
