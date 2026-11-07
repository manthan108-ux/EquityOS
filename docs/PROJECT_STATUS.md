# Project Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture and foundation | Complete |
| 2 | Database foundation | Complete |
| 3 | Company resolution | Next |
| 4 | Data acquisition | Pending |
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

## Known limitations

- Seed identity data (BSE codes, ISINs, names) is cross-checked against public secondary sources only; primary NSE/BSE verification is Phase 4. Reliance's founding year and all investor-relations URLs are intentionally unset.
- Only 3 industries are seeded (those the test universe needs); sector/industry taxonomy grows with the Phase 8 sector profiles.

- The frontend status panel's browser-side rendering was not exercised in a real browser; connectivity was verified via HTTP (API responses, CORS header, frontend returns 200).
- Tesseract OCR and Redis are not installed; they are needed only from Phase 5 and if background workers become necessary.
- Docker image builds are not yet part of CI.
