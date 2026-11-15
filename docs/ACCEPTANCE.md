# EquityOS 1.0 - Final acceptance (CP15, 2026-11-15)

This is the record of what was run to accept 1.0, what it showed, and what was not verified.

## Results

| Area | Check | Result |
|---|---|---|
| Backend regression | `pytest` (full suite, one process) | 1,961 passed |
| Backend lint | `ruff check` | clean |
| Frontend | `tsc --noEmit`, `eslint`, `vitest run`, `next build` | clean; 65 tests passed (6 files); build succeeds |
| Migrations | fresh database migrated to head by the test fixtures; `alembic check` | head 0023, no model/migration drift |
| API | server started; every parameter-free GET called | public endpoints 200; endpoints needing parameters 422; research and pipeline endpoints 401 without a key |
| Worker | queued run 58 with the Docker worker | picked up and completed with nobody present |
| Failure recovery | queued run 59 (6 companies), `docker kill` of the worker mid-run, restarted it | run reclaimed once its heartbeat went stale (600 s limit) and completed 6/6 |
| OCR | image rebuilt from current code; a generated page OCR'd inside the container | Tesseract 5.5.0 (eng, osd); both figures read correctly, mean confidence 96% |
| Deployment config | `docker compose config`; image build; stack started with `--profile worker` | valid; `/health/ready` reports the database up |
| Environment and keys | `.env` untracked; `POSTGRES_PASSWORD` required by compose; key-pattern scan of tracked files; log statements searched | no secrets in git; only a SHA-256 digest of research keys is kept; keys compared in constant time; no log line prints a key |
| Data integrity | `python -m app.evidence.cli integrity` | no failures; 76,346 evidence rows; only INFO is 2 open Wipro tax-expense conflicts (deliberately unresolved) |
| Calculations and traceability | `python -m app.research.validate_pilot` | 24 of 24 pass, 0 failures (statement identities, peer medians and rank guards, scenario ordering, evidence ids, verdict recomputation) |
| Reproducibility | analysis built twice for each of the 24 companies on a fixed date, compared by hash | 24 of 24 identical |

## Defects found during acceptance

* A stray empty `backend/app;C` directory (a shell artefact) was removed.
* One test run reported 979 errors. Cause: two test processes were running at once and one force-dropped the shared `equityos_test` database. It was a process mistake, not a code fault; the rerun alone passed. The rule stands: one pytest process at a time.

## Operating it

* Start: copy `.env.example` to `.env`, set `POSTGRES_PASSWORD`, then `docker compose up -d --build`. The backend applies migrations on start.
* Unattended pipeline: `docker compose --profile worker up -d worker`. The worker restarts on failure, stops cleanly on SIGTERM, and an interrupted run is picked up again after 10 minutes without a heartbeat.
* Health: `GET /health/ready`; pipeline monitoring under `/api/pipeline/*` and `python -m app.pipeline.cli health|alerts|metrics`.
* Keys: `GROQ_API_KEY`, `GEMINI_API_KEY`, `UPSTOX_ANALYTICS_TOKEN`, `RESEARCH_API_KEYS` (`name:secret[:admin]`) are read from the environment only. A provider with no credentials reports UNCONFIGURED; the research API refuses all requests while `RESEARCH_API_KEYS` is empty.
* Reports: `python -m app.pipeline.cli run --symbols ... --no-llm`, then generate from the research workflow. Missing documents are requested from the end user at generation time; nothing is assumed in their place.

## Not verified, and known limits

* The Playwright end-to-end suite was not run, and the frontend Docker image was not rebuilt or started; the frontend was verified by type-check, lint, unit tests and a production build.
* LLM-backed stages were not exercised: pipeline runs used the no-LLM path.
* Live Upstox access was not re-tested in this pass.
* The OCR check used one generated page, not scanned filings.
* Recovery from a killed worker takes about 10 minutes, because that is the stale-heartbeat limit.
* Investment conclusions are limited by input coverage: of 24 pilot companies, 19 are WATCH because no valuation method completes without entered assumptions. The back-test of verdict rules reads INSUFFICIENT. See `KNOWN_LIMITATIONS.md`.
* The market feed's net profit includes minority interests for some companies, so a few ratios are slightly overstated.
* Scope is the 24-company pilot universe.
