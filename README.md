# EquityOS

An India-focused, evidence-driven AI equity research platform. Enter an NSE/BSE listed company and EquityOS (when complete) produces an institutional-style PDF research report in which every material claim traces back to a recorded source.

> EquityOS produces research assistance, not investment advice or a promise of returns. It makes no claim of SEBI registration or analyst accreditation.

**Status:** Phase 3 of 12 complete (company resolution). See [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md).

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
