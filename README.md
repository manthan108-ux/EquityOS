# EquityOS

An India-focused, evidence-driven equity research platform. Pick an NSE/BSE listed company and EquityOS builds an
institutional-style research report (PDF) in which every material figure traces back to a recorded source, and where a
missing input is shown as missing instead of being estimated.

> EquityOS produces research assistance, not investment advice or a promise of returns. It makes no claim of SEBI
> registration or analyst accreditation.

**Status:** development complete, production readiness pending. It is validated on a 24-company pilot and has not been run
as a hosted production service. See [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) and [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## What it is for

Equity research for Indian listed companies is slow and hard to audit: figures sit in annual reports, exchange filings and
market feeds, and a conclusion rarely shows what it rests on. EquityOS collects those inputs, stores each figure with its
source, document and page, validates it (statement identities, cross-checks, conflicts), and only then calculates, scores,
values and reports. Intended users are analysts, students of equity research and developers who want an auditable research
pipeline. It is a decision aid, not a trading system.

## Stack

FastAPI · Pydantic · SQLAlchemy · Alembic · PostgreSQL 17 · Next.js 16 · React 19 · TypeScript · Docker Compose · GitHub Actions.
Document reading uses PyMuPDF and Tesseract OCR (inside the Docker image); optional LLM header interpretation uses Groq or Gemini.

## Capabilities

- **Acquisition:** provider router over the Upstox analytics feed, exchange files and company investor pages; legal-only sources,
  every refusal recorded.
- **Document intelligence:** PDF and XBRL reading (text and OCR), statement and ratio extraction, share-capital note reader;
  nothing is promoted to evidence unless an arithmetic identity or a text layer supports it.
- **Evidence ledger and validation:** one record per figure with provenance, conflicts surfaced, integrity audit.
- **Calculation, sector and peer engines:** ratios, growth, cash flow, working capital, financial health, bank and sector
  indicators, peer medians and ranks.
- **Scoring, valuation and verdict (v2):** absolute and relative score; valuation by residual income (banks), relative P/E,
  and DCF where inputs suffice; bear/base/bull scenarios, sensitivity grids; a rule-based verdict with fixed thresholds.
- **Assumption ledger:** every valuation input is either sourced, rule-derived or a user assumption, with author, date and
  rationale kept separately.
- **Research workflow and reports:** readiness checks, requests for missing documents, a 28-45 page PDF per company.
- **Operations:** resumable pipeline with an unattended worker, monitoring, backup and restore scripts, research API keys.

### The 24-company pilot

Banking (HDFCBANK, ICICIBANK, KOTAKBANK, AXISBANK, SBIN), IT (TCS, INFY, WIPRO, TECHM, HCLTECH), energy (RELIANCE, ONGC,
IOC, BPCL), FMCG (ITC, HINDUNILVR, NESTLEIND, BRITANNIA, GODREJCP) and autos (M&M, MARUTI, TMPV, HYUNDAI, TVSMOTOR).
Coverage differs by company; the report states what is missing. Scope is not being expanded.

## Architecture in brief

`acquisition / providers` -> `docintel` (documents) -> `evidence` (ledger and validation) -> `calc` (calculation engine) ->
`sector` and `research` (peers, scoring, valuation, verdict, thesis, report) -> PDF. A resumable `pipeline` runs the stages per
company; the FastAPI app exposes everything under `/api`, and the Next.js dashboard drives research and shows reports.
Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PIPELINE.md](docs/PIPELINE.md), [docs/EVIDENCE_ENGINE.md](docs/EVIDENCE_ENGINE.md).

## Configuration and security

All configuration is environment-based; see [.env.example](.env.example) for the variable names. Copy it to `.env` and set:

- `POSTGRES_PASSWORD` (required; also inside `DATABASE_URL`), `POSTGRES_USER`, `POSTGRES_DB`, `ENVIRONMENT`, `CORS_ORIGINS`,
  `DATA_DIR`, `NEXT_PUBLIC_API_URL`;
- optional provider credentials: `UPSTOX_ANALYTICS_TOKEN`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `DATA_GOV_IN_API_KEY`. A provider
  without credentials reports UNCONFIGURED; nothing is invented in its place;
- `RESEARCH_API_KEYS` (`name:secret[:admin]`). The research API refuses every request while it is empty. Create one with
  `python -m app.research.keys_cli add NAME --admin`, which writes a random key to `.env` without printing it.

`.env` is git-ignored and must never be committed. Only a SHA-256 digest of research keys is stored, keys are compared in
constant time, and no log line prints a key.

## Run it

**Everything in Docker** (needs Docker Desktop):

```bash
cp .env.example .env          # set POSTGRES_PASSWORD (and the same value inside DATABASE_URL)
docker compose up -d --build  # the backend applies migrations on start
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| Backend API docs | http://localhost:8000/docs |
| Liveness / readiness | http://localhost:8000/health , http://localhost:8000/health/ready |
| Postgres (host port) | `localhost:5433` |

If 3000 or 8000 is taken set `FRONTEND_PORT` / `BACKEND_PORT` (and `NEXT_PUBLIC_API_URL`). Stop with `docker compose down`
(`-v` also deletes the database).

**Local development** (database in Docker, apps on the host):

```bash
docker compose up -d db
# backend, from ./backend
python -m venv .venv && .venv\Scripts\activate        # source .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
alembic upgrade head
python -m app.seeds.loader                            # exchanges, sectors, seed companies (idempotent)
uvicorn app.main:app --reload                         # http://localhost:8000
# frontend, from ./frontend
npm install && npm run dev                            # http://localhost:3000
```

**Worker** (unattended pipeline, waits for the backend, restarts on failure, runs queued jobs):
`docker compose --profile worker up -d worker`.

## How the research pipeline works

Stages per company: classify -> website -> upstox -> documents -> extract -> evidence -> calculate -> enrich -> readiness -> report. Each stage is
resumable, isolated and time-limited; a failed stage is recorded and retried, never skipped silently.

```bash
# from ./backend
python -m app.pipeline.cli status
python -m app.pipeline.cli run --symbols TCS,SBIN --no-llm
python -m app.pipeline.cli health            # exit code 2 when CRITICAL
python -m app.evidence.cli integrity         # read-only audit of the evidence store
python -m app.research.validate_pilot        # recomputes and checks all 24 companies
```

Valuation inputs are managed with `python -m app.research.assumption_cli list|set --symbol X` (user assumptions) and
`python -m app.research.setup_cli market|history|show` (sourced market inputs and rule-derived history inputs).

## How reports are generated

In the dashboard choose a company, review its readiness (what evidence exists, what is missing and why it matters), supply any
requested documents, run the research, then generate the report. Through the API: `POST /api/research/sessions/{id}/reports`
creates it and `GET /api/research/reports/{id}/pdf` downloads the PDF (an API key is required). Missing documents are asked for
at generation time; nothing is assumed in their place.

## Tests and checks

```bash
# from ./backend  (database tests need Postgres: docker compose up -d db). Run ONE pytest process at a time.
pytest -q
ruff check . && ruff format --check .
alembic upgrade head && alembic check

# from ./frontend
npm run lint
npm test
npm run build          # also type-checks
npm run e2e            # Playwright, mocked API
E2E_LIVE_URL=http://localhost:3000 E2E_API_KEY=<admin key> npm run e2e:live   # real stack
```

## Backup and restore

```bash
./scripts/backup.sh                              # writes backups/<file>.dump
./scripts/restore.sh backups/<file>.dump         # into a scratch database unless --replace is given
```

## Known limitations

DCF completes for none of the 24 companies on stored data; bank residual-income and peer-P/E values can disagree widely;
the verdict back-test is insufficient; share-count and document coverage is partial; integrations and LLM workflows were
verified on small samples; no production deployment has been run. Full list: [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## Documentation

- [Final acceptance record and handover](docs/ACCEPTANCE.md)
- [Known limitations](docs/KNOWN_LIMITATIONS.md)
- [Optional roadmap](docs/NEXT_STEPS.md)
- [Project status](docs/PROJECT_STATUS.md), [Architecture](docs/ARCHITECTURE.md), [Database schema](docs/DATABASE_SCHEMA.md)
- [Pipeline](docs/PIPELINE.md), [Research workflow](docs/RESEARCH_WORKFLOW.md), [Report engine](docs/REPORT_ENGINE.md),
  [Methodology](docs/RESEARCH_METHODOLOGY.md), [Data sources](docs/DATA_SOURCES.md)

## License

MIT, see [LICENSE](LICENSE).
