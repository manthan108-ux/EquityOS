# Project Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture and foundation | Complete |
| 2 | Database foundation | Complete |
| 3 | Company resolution (+ full-universe master import) | Complete |
| 4 | Data acquisition | Complete |
| 5 | Document intelligence | Next |
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

## Universe load (2026-11-07)

BSE list imported from the manually downloaded file (5,139 rows, 4,855 usable). 284 BSE rows were skipped and reported: 276 funds/ETFs (INF ISINs), 6 non-equity instruments, and 2 equities with no valid ISIN (A.D.S. Diagnostics 523031, Pushpsons Industries 531562). The 5 pre-existing seed companies were verified unchanged apart from previously empty fields being filled.

## Known limitations

- **Financial statements, quarterly results, shareholding patterns, announcements and XBRL are not yet obtainable programmatically.** The exchanges serve them through protected APIs that we do not use. Phase 5 must rely on company investor-relations documents, so coverage will vary by company and gaps must be disclosed in reports. This is the biggest open risk to the project.
- Stored prices are unadjusted for splits and bonuses; corporate actions are not yet acquired.
- The BSE company list still needs a manual download (BSE's API is blocked); the NSE list refreshes automatically.
- BSE historical price files are patchy (BSE removes older files) and prices have only been backfilled for 3 days; deeper NSE history is a longer, rate-limited run.
- News feeds hold only a recent window and matching is a local phrase check; items are leads, not conclusions.
- Fuzzy suggestions can be loosely related when nothing is close; they are only ever suggestions.
- The frontend was not exercised in a real browser (API and build were verified).
- Tesseract OCR and Redis are not installed; they are needed from Phase 5 and only if background workers become necessary.
- Docker image builds are not part of CI yet.
