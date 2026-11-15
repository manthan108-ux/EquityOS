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


---

# Post-acceptance closure programme (2026-11-15)

## What was run, and the result

| Area | Check | Result |
|---|---|---|
| Backend | full suite, one process | 1,973 passed; ruff clean |
| Frontend | tsc, eslint, vitest | clean; 65 tests |
| Browser, mocked backend | Playwright (`npm run e2e`) | 22 of 22 passed |
| Browser, REAL stack | `npm run e2e:live` against the Docker Compose stack | 4 of 4 passed: no key is refused; company selection -> research -> report generated -> real PDF downloaded (`%PDF-`, > 50 KB); pipeline page shows real runs; a wrong key is refused and said so |
| Docker | backend and frontend images built; full stack with worker started | healthy; frontend reaches the backend (CORS follows `FRONTEND_PORT`) |
| Clean start | second project with an empty database volume | migrations applied, 0023 head, worker log clean, authenticated smoke test 200. Found and fixed a start-up race (worker started before the backend had migrated); the worker now waits for the backend's health check |
| Admin key | `python -m app.research.keys_cli add NAME --admin` | random 256-bit key written to the git-ignored `.env`, not printed unless `--show`; no source change; unauthenticated and wrong-key requests return 401; the key does not appear in any container log |
| Upstox | live `verify` for TCS and SBIN | authenticated; 8 datasets verified; COMPETITORS fails (UDAPI100011); an invalid token reports ACCESS_DENIED. Fixed: `verify` could be answered from the 6-hour cache |
| LLM | `python -m app.llm.cli check`; live header interpretation on 4 TECHM annual reports | both providers answer correctly; the LLM step ran; nothing promoted; evidence-store hash unchanged before and after |
| OCR | real scanned filing re-run inside the Docker image | Tesseract 5.5.0; page confidence 74-93%; page numbers kept; a 34%-confidence value flagged; nothing promoted. Whole store: 595 OCR facts, 71 flagged, 0 promoted |
| Backup and recovery | `scripts/backup.sh`, `scripts/restore.sh` on the real 1.4 GB database | 202 MB dump in 41 s; restored into a scratch database in 2 min; 8 key tables identical row for row; reports built from the restored copy hash identical to live for 5 companies |
| Worker recovery, monitoring | previous pass plus `pipeline health/alerts` | worker alive, 0 alerts |
| Data and research | integrity audit; `validate_pilot`; 24 reports regenerated | no failures; 24 of 24 pass; 28-45 pages each |

## Production-readiness criteria

1. Application starts and operates correctly: **met** (clean-start verified).
2. Primary workflow end to end: **met** (live browser journey to a downloaded PDF).
3. Valuation functional for all applicable companies: **NOT met.** Market inputs are entered for all 24; ten companies have a
   relative-P/E valuation and, since the user supplied growth, ROE-shift and scenario cost-of-equity values, ICICIBANK and
   AXISBANK also have a complete residual-income valuation (bear/base/bull, sensitivity, growth below cost of equity checked).
   DCF cannot complete for any company on stored data (net debt, share counts, three or more years of EBIT, capex, depreciation);
   HDFCBANK and SBIN now also have a complete residual-income valuation (user-entered growth 4/6/8, ROE shift -2/0/+2 pp, bear and
   bull cost of equity equal to each company's own base CAPM 13.99%; stored share counts reconcile to equity and market cap);
   KOTAKBANK's P/B is withheld
   (its only verified count predates a split that is not applied). See KNOWN_LIMITATIONS.md.
4. External integrations verified or documented as blocked: **met** (Upstox and both LLM providers verified live; limits recorded).
5. Conclusions explainable and validated: **partly.** Verdicts are explainable and recomputed by `validate_pilot`; the historical
   back-test reads INSUFFICIENT because only about three years of prices and statements exist.
6. All 24 pilot companies pass research validation: **met** (24 of 24, 0 failures). Fourteen of them are WATCH for stated reasons.
7. Regression and end-to-end tests pass: **met.**
8. Deployment, security and recovery verified: **met.**

## Status

EquityOS 1.0 is **accepted as a working, validated system**. It is **not declared production-ready in the sense of criterion 3**:
valuations remain unavailable where the evidence or the user's judgement is missing, and the product says so instead of
filling the gap.
