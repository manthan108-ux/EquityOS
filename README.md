# EquityOS

An India-focused, evidence-driven AI equity research platform. Enter an NSE/BSE listed company and EquityOS (when complete) produces an institutional-style PDF research report in which every material claim traces back to a recorded source.

> EquityOS produces research assistance, not investment advice or a promise of returns. It makes no claim of SEBI registration or analyst accreditation.

**Status:** Phase 4 of 12 complete (data acquisition and multi-source provider router). See [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md).

## Stack

FastAPI · Pydantic · SQLAlchemy · PostgreSQL · Next.js 16 · React 19 · TypeScript · Docker Compose · GitHub Actions

## Quick start (everything in Docker)

Requires Docker Desktop running.

```bash
# from the repository root
cp .env.example .env          # then set POSTGRES_PASSWORD (and the same value inside DATABASE_URL)
docker compose up -d --build
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| Backend API docs | http://localhost:8000/docs |
| Liveness | http://localhost:8000/health |
| Readiness (checks DB) | http://localhost:8000/health/ready |
| Postgres (host port) | `localhost:5433` |

Stop with `docker compose down` (add `-v` to also delete database data).

## Local development (hot reload)

Run only the database in Docker, and the apps on your machine:

```bash
# repository root
docker compose up -d db

# backend  (terminal 1, from ./backend)
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
uvicorn app.main:app --reload     # http://localhost:8000

# frontend (terminal 2, from ./frontend)
npm install
npm run dev                       # http://localhost:3000
```

The backend reads `DATABASE_URL` from `.env` (see `.env.example`). Local runs reach Docker's Postgres on port **5433**.

## Database migrations and seed data

```bash
# from ./backend, with the database running
alembic upgrade head              # apply migrations (the Docker backend does this on start)
python -m app.seeds.loader        # load exchanges, sectors and the 5-company test universe (idempotent)
# in Docker:  docker compose exec backend python -m app.seeds.loader
```

## Acquiring data (Phase 4)

```bash
# from ./backend
python -m app.acquisition.cli probe             # what each source returns right now
python -m app.acquisition.cli nse-equity-list   # refresh the NSE company list automatically
python -m app.acquisition.cli name-history --apply
python -m app.acquisition.cli prices --exchange NSE --from 2026-11-03 --to 2026-11-06
```

See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) for what each source provides and what is blocked, and [docs/DATA_ACQUISITION_STRATEGY.md](docs/DATA_ACQUISITION_STRATEGY.md) for the provider router, Upstox evaluation and coverage engine.

```bash
python -m app.providers.cli providers                 # registry: configured / verified / unverified
python -m app.providers.cli coverage --symbol TCS     # what data exists and how much to trust it
python -m app.providers.cli compare --symbols RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK
```

## Loading all NSE/BSE companies

Download the exchanges' official security lists in your browser and import them; see [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md):

```bash
python -m app.master_data.cli --nse EQUITY_L.csv --bse bse_scrips.csv   # from ./backend
```

## Tests and checks

```bash
# from ./backend  (database tests need the Docker Postgres running: docker compose up -d db)
pytest -q
ruff check . && ruff format --check .

# from ./frontend
npm run lint
npm run build      # also type-checks
```

## Configuration

All configuration is environment-based; see [.env.example](.env.example). Never commit `.env`.

## Documentation

- [Project status](docs/PROJECT_STATUS.md)
- [Architecture and design decisions](docs/ARCHITECTURE.md)
- [Database schema](docs/DATABASE_SCHEMA.md)
- [Data sources](docs/DATA_SOURCES.md)

## License

MIT, see [LICENSE](LICENSE).
