# Project Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture and foundation | Complete |
| 2 | Database foundation | Next |
| 3 | Company resolution | Pending |
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

## Known limitations

- The frontend status panel's browser-side rendering was not exercised in a real browser; connectivity was verified via HTTP (API responses, CORS header, frontend returns 200).
- No ORM models or migrations yet (Phase 2).
- Tesseract OCR and Redis are not installed; they are needed only from Phase 5 and if background workers become necessary.
- Docker image builds are not yet part of CI.
