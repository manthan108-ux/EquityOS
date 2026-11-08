# Database Schema

PostgreSQL, managed by SQLAlchemy models (`backend/app/models/`) and Alembic migrations (`backend/migrations/`). The schema is only ever changed through migrations.

## Implemented (Phase 2): company master data

```
exchanges ──< securities >── companies >──< company_aliases
                                │
                                ├── sectors ──< industries
                                └── industries
```

| Table | Purpose | Key constraints |
|---|---|---|
| `exchanges` | NSE, BSE (extensible) | unique `code`, unique `mic` |
| `sectors` | Research sectors; each maps to a Phase 8 sector profile | unique `code`, unique `name` |
| `industries` | Industries within a sector | unique (`sector_id`, `code`) |
| `companies` | One row per issuer; UUID canonical ID; `issuer_key` (ISIN chars 1-7) merges NSE/BSE listings | unique `issuer_key`; names are NOT unique; `founding_year` 1600-2100 |
| `securities` | One exchange listing of a company | unique (`exchange_id`, `symbol`); unique (`exchange_id`, `security_code`); ISIN matches `^[A-Z]{2}[A-Z0-9]{9}[0-9]$`; BSE code is 6 digits |
| `master_data_imports` | Audit of each imported exchange file (SHA-256, row counts, skip reasons) | row counts add up |
| `company_aliases` | Names users may type; `normalized_alias` is what the resolver matches | unique (`company_id`, `normalized_alias`); index on `normalized_alias` |

### Design notes

- **Company vs security.** A dual-listed company is one `companies` row with two `securities` rows (NSE and BSE), sharing an ISIN. No duplicate companies.
- **UUID company ID** is opaque and independent of any exchange code, so it never changes if a ticker does.
- **Aliases may collide across companies** by design; the Phase 3 resolver asks the user when a name is ambiguous.
- **Unverified means NULL.** Missing facts are stored as NULL, never as zero or a guess. `companies.master_data_source` records provenance and verification status of the identity fields.
- **Enums are `VARCHAR` + `CHECK`** instead of native PostgreSQL enums, which are awkward to migrate.
- **Constraint names** follow a naming convention (`uq_<table>_<all columns>`, `ck_`, `fk_`, `pk_`) so migrations are deterministic.
- **Timestamps** (`created_at`, `updated_at`, timezone-aware) on every table.
- `ISIN` validity beyond the regex (the ISO 6166 check digit) is enforced in application code (`app/core/identifiers.py`) when seeding or ingesting.

## Implemented (Phase 4): acquisition

| Table | Purpose |
|---|---|
| `sources` | Registry of data sources with authority tier (1 exchange files, 2 company, 3 regulator, 4 government, 5 reputable secondary, 6 other) and terms notes |
| `acquisition_log` | One row per fetch: source, URL, outcome, HTTP status, attempts, bytes, duration. Outcomes: OK, CACHED, BLOCKED_BY_ROBOTS, ACCESS_DENIED, RATE_LIMITED, NOT_FOUND, CLIENT_ERROR, TIMEOUT, SERVER_ERROR, NETWORK_ERROR, INVALID_CONTENT, TOO_LARGE |
| `documents` | Downloaded files stored on disk under their SHA-256; unique per content hash; type, source, publication date and financial period |
| `eod_prices` | Official exchange end-of-day prices per security, date and series; unique (security, date, series); keeps source URL, file SHA-256 and retrieval time. Unadjusted for corporate actions. NULL delivery fields mean not published, never zero |

`company_aliases.source` records where an alias came from (for example `NSE namechange.csv (2025-04-09)`); it is NULL for seed and exchange-list aliases.

## Implemented (Phase 4b): provider facts

| Table | Purpose |
|---|---|
| `financial_facts` | One line item of one statement for one period, provider, basis (CONSOLIDATED/STANDALONE) and period type. Keeps `original_value`, `original_unit`, `period_label` and the normalized `value_inr` (NULL if the unit is not convertible), plus source URL, retrieval time and the archived payload. Unique per company/provider/statement/line/period/type/basis |
| `shareholding_facts` | Percent held per category and quarter (0-100 enforced) |
| `corporate_actions` | Dividend / bonus / split / rights events as published, with the raw ratio text and event details |
| `ratio_snapshots` | Provider-computed ratios with the raw strings and the provider's own definitions |
| `raw_payloads` | Every provider response archived verbatim (JSONB, SHA-256), never containing credentials |
| `dataset_verifications` | A recorded live retrieval attempt per provider and dataset. A dataset is "supported" only if one succeeded |
| `sources` (extended) | Registry columns: auth requirement, access method, rate limits, declared datasets, coverage note, redistribution policy |

## Implemented (Phase 5): document intelligence (migration 0005)

| Table | Purpose |
|---|---|
| `document_extractions` | One processing run of one document: extractor version, status (COMPLETED, NO_STATEMENTS, NEEDS_OCR, UNSUPPORTED, FAILED), page counts, pages needing OCR, statements found, values extracted and promoted, structured issues, error |
| `document_pages` | Text of every page of a run (for citations), character count, whether the page needs OCR and how its text was obtained. Unique per run and page |
| `extracted_statements` | A statement table recognised on a page: type, basis, unit (NULL = not stated), title, how it was detected, per-column header/period/audited/problem, matched and unmatched rows, every arithmetic check with the numbers used |
| `extracted_facts` | EVERY value read: canonical item, printed label, page, row, column, column header, period end and duration (3/6/9/12 or NULL), audited flag, raw cell text, original and normalized value, position on the page, validation status (PASSED/FAILED/UNCHECKED) with notes, and whether it was promoted (or why not) |
| `financial_facts` (extended) | `document_id` and `source_page`: values promoted from a document point to the exact page |

## Implemented (Phase 5 stabilization): migration 0006

| Change | Purpose |
|---|---|
| `document_link_events` (new) | Audit trail of every change to a document's company: before/after, action, reason, actor, evidence (never deleted) |
| `documents.issuer_name`, `association_status`, `association_note`, `association_checked_at` | Who issued the document (read from its first page); only non-MISMATCH documents count for coverage |
| `document_extractions.superseded_at`, `ocr_engine` | Newer runs supersede older ones (kept for audit); engine used for OCR |
| `document_pages.ocr_confidence` | Mean Tesseract confidence of OCR'd pages |
| `extracted_facts.dimension`, `needs_review`, `ocr_confidence` | Segment name / schedule title; flagged for a person; lowest word confidence |
| `extracted_statements.statement` also SEGMENT, RATIOS, NOTE, SHAREHOLDING | Disclosure tables |
| `document_sections` (new) | Auditor's report, accounting policies, management discussion: page range, excerpt, explicit flags |
| `group_entities` (new) | Entities named in a company's related-party note with the relationship word printed, page, and a link to our universe when listed |
| `financial_facts.derived_from`, `history`; period types HALF_YEAR, NINE_MONTH | Derived-quarter inputs; earlier values replaced by a restatement; cumulative periods |
| 11 new indexes | Foreign keys of the Phase 5 tables and `financial_facts.payload_id`, `dataset_verifications.company_id`, `documents.source_id` |

## Implemented (Phase 6): evidence store (migrations 0007, 0008)

| Table | Purpose |
|---|---|
| `evidence_records` | One citable record: class (FACT / CALCULATION / ASSUMPTION / INTERPRETATION), metric, dimension, period, basis, normalized value and unit (plus the published value/unit for facts), formula or statement text, provider and authority tier, source table + natural key, document and page, source URL, quality inputs in `details`, content hash, status (ACTIVE / SUPERSEDED / WITHDRAWN) and reason. Unique per (source table, source key, content hash). CHECK constraints: facts need a provider and URL, calculations a number and a formula, assumptions and interpretations text, a value needs a unit, a FACT needs a number or a statement |
| `evidence_links` | `evidence_id` depends on `input_id` with a role (INPUT, CITES, SUPPORTS) and the name used in the formula. No self-links |
| `evidence_resolutions` | Derived index, rebuilt on every validation run: the one value per (metric, dimension, period, period type, basis), its status, quality with reasons, alternatives and their differences, and whether it is blocked and by which rules |
| `validation_runs` | One validation of one company: rules version, as-of date, counts, per-rule checked/passed counters. Kept as history |
| `validation_findings` | Findings of a run: rule, severity, message, metric, period, the evidence ids involved |

## Seed data

`python -m app.seeds.loader` (from `./backend`) loads exchanges, sectors, a few industries and the five-company test universe (Reliance, TCS, Infosys, HDFC Bank, ICICI Bank). It is idempotent and never overwrites existing rows. BSE codes were cross-checked against public secondary sources and ISINs pass the check digit; verification against primary NSE/BSE master files is Phase 4 work.

## Planned (added by the phase that needs them)

| Phase | Tables |
|---|---|
| 4 | `sources`, `documents`, `acquisition_jobs` |
| 7 | `financial_statements`, `income_statements`, `balance_sheets`, `cash_flows`, `quarterly_results`, `segment_financials`, `shareholding_data`, `market_data`, `technical_indicators`, `financial_metrics` |
| 8 | `sector_profiles`, `sector_weights`, `weight_adjustments`, `peer_sets`, `peers` |
| 9 | `research_jobs`, `agent_runs`, `agent_outputs`, `validation_results`, `llm_usage` |
| 10 | `forecasts`, `forecast_assumptions`, `valuations`, `valuation_assumptions`, `valuation_sensitivities`, `scenarios`, `risk_assessments` |
| 11 | `research_reports`, `report_sections`, `report_sources`, `investment_theses` |

Management, governance, corporate-announcement and news event tables (`management_events`, `governance_events`, `corporate_announcements`, `news_events`) are added alongside the acquisition and agent phases that populate them.
