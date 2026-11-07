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

## Seed data

`python -m app.seeds.loader` (from `./backend`) loads exchanges, sectors, a few industries and the five-company test universe (Reliance, TCS, Infosys, HDFC Bank, ICICI Bank). It is idempotent and never overwrites existing rows. BSE codes were cross-checked against public secondary sources and ISINs pass the check digit; verification against primary NSE/BSE master files is Phase 4 work.

## Planned (added by the phase that needs them)

| Phase | Tables |
|---|---|
| 4 | `sources`, `documents`, `acquisition_jobs` |
| 5 | `document_sections`, `document_tables` |
| 6 | `evidence`, `evidence_validation`, `evidence_conflicts` |
| 7 | `financial_statements`, `income_statements`, `balance_sheets`, `cash_flows`, `quarterly_results`, `segment_financials`, `shareholding_data`, `market_data`, `technical_indicators`, `financial_metrics` |
| 8 | `sector_profiles`, `sector_weights`, `weight_adjustments`, `peer_sets`, `peers` |
| 9 | `research_jobs`, `agent_runs`, `agent_outputs`, `validation_results`, `llm_usage` |
| 10 | `forecasts`, `forecast_assumptions`, `valuations`, `valuation_assumptions`, `valuation_sensitivities`, `scenarios`, `risk_assessments` |
| 11 | `research_reports`, `report_sections`, `report_sources`, `investment_theses` |

Management, governance, corporate-announcement and news event tables (`management_events`, `governance_events`, `corporate_announcements`, `news_events`) are added alongside the acquisition and agent phases that populate them.
