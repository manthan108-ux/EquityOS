# Project Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture and foundation | Complete |
| 2 | Database foundation | Complete |
| 3 | Company resolution (+ full-universe master import) | Complete |
| 4 | Data acquisition | Next |
| 5 | Document intelligence | Pending |
| 6 | Evidence and validation engine | Pending |
| 7 | Financial calculation engine | Pending |
| 8 | Sector and peer engine | Pending |
| 9 | Research agent system (LLM layer starts here) | Pending |
| 10 | Forecasting, valuation and risk | Pending |
| 11 | Investment Committee and PDF generation | Pending |
| 12 | Final testing and deployment | Pending |

## Phase 1 - what works

- FastAPI app with `/health` and `/health/ready` (database check), CORS for the frontend.
- Next.js 16 / TypeScript frontend showing live backend and database status.
- PostgreSQL 17 in Docker Compose; full stack runs with `docker compose up -d --build`.
- Backend: 6 pytest tests, ruff lint and format clean. Frontend: ESLint and production build clean.
- GitHub Actions CI for backend and frontend.

## Phase 2 - what works

- SQLAlchemy models + Alembic migration `0001` for exchanges, sectors, industries, companies, securities (listings) and company aliases. See [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md).
- Migration verified: upgrade, downgrade to empty, upgrade again, and no drift between models and database.
- Idempotent seed of the five-company test universe (Reliance, TCS, Infosys, HDFC Bank, ICICI Bank), each with NSE and BSE listings.
- ISO 6166 ISIN check-digit validator and text normaliser (reused by the Phase 3 resolver).
- 39 backend tests run against a real throwaway PostgreSQL database built with the real migrations. CI provides a Postgres service.
- The backend container applies migrations on start.

## Phase 3 - what works

- Deterministic resolver (no LLM): ISIN, then BSE code, then ticker, then exact name/alias, then fuzzy suggestions. Ambiguous or fuzzy results are never auto-selected. NSE+BSE listings of one company are one result.
- `GET /api/companies/search?q=` and `GET /api/companies/{id}` (unverified fields are listed, never invented).
- Frontend: search box with debounce and suggestions, company confirmation card (the Start research button is disabled until the pipeline exists).
- **All NSE/BSE companies:** importer for the official NSE/BSE security lists (see [DATA_SOURCES.md](DATA_SOURCES.md)); merges listings by ISIN issuer; audited; idempotent. Verified on a synthetic 5,000-company / 10,000-listing import.
- 108 backend tests.

## Universe loaded (2026-11-07)

Imported from the exchanges' official lists (saved in `data/master/`, git-ignored): NSE `EQUITY_L.csv` (2,630 rows) and BSE equity list (5,139 rows; 4,855 usable).
Result: **4,969 companies, 7,485 listings** (2,499 on both exchanges, 127 NSE only, 2,343 BSE only). 284 BSE rows were skipped and reported: 276 funds/ETFs (INF ISINs), 6 non-equity instruments (rights entitlements, preference shares) and 2 equities with no valid ISIN (A.D.S. Diagnostics 523031, Pushpsons Industries 531562). Re-importing creates nothing new. The 5 pre-existing seed companies were verified unchanged apart from previously empty fields being filled.

## Known limitations

- **Former names are not searchable** (e.g. "Zomato", now Eternal; "Tata Motors" demerger names) because exchange files hold only current names. Verified former-name aliases need a source (Phase 4).
- The BSE file downloaded has no `Industry` column, so `exchange_industry` is empty; research sectors come from Phase 8 anyway.
- The two BSE equities without an ISIN cannot be identified by our ISIN-based issuer key and are not imported.
- Fuzzy suggestions can still be loosely related when nothing is close; they are only ever suggestions, never auto-selected.
- The frontend status panel's browser-side rendering was not exercised in a real browser; connectivity was verified via HTTP (API responses, CORS header, frontend returns 200).
- Tesseract OCR and Redis are not installed; they are needed only from Phase 5 and if background workers become necessary.
- Docker image builds are not yet part of CI.
