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
- 104 backend tests.

## Known limitations

- **The full universe is not loaded yet.** Neither exchange allows programmatic download from this project (NSE drops the connection; BSE returns 403) and we do not bypass that. The user must download the two official files and run the import command in DATA_SOURCES.md. Until then only the 5-company test universe exists.
- The importer was written against the documented column layouts and tested with synthetic fixtures, not yet against a real exchange file. It fails loudly on unexpected headers.

- Seed identity data (BSE codes, ISINs, names) is cross-checked against public secondary sources only; primary NSE/BSE verification is Phase 4. Reliance's founding year and all investor-relations URLs are intentionally unset.
- Only 3 industries are seeded (those the test universe needs); sector/industry taxonomy grows with the Phase 8 sector profiles.

- The frontend status panel's browser-side rendering was not exercised in a real browser; connectivity was verified via HTTP (API responses, CORS header, frontend returns 200).
- Tesseract OCR and Redis are not installed; they are needed only from Phase 5 and if background workers become necessary.
- Docker image builds are not yet part of CI.
