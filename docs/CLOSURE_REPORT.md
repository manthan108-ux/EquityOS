# EquityOS - Project closure report (2026-11-16)

Closure of the EquityOS development project. This was a verification and documentation exercise, not a development phase. The
detailed, dated evidence is in [ACCEPTANCE.md](ACCEPTANCE.md); limitations are in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## A. Project completion summary

EquityOS is an India-focused, evidence-driven equity research platform, built and validated on a 24-company pilot (banking, IT,
FMCG, autos, energy). It implements:

- acquisition through a provider router (Upstox analytics feed, exchange files, company investor pages), legal sources only;
- document intelligence (PDF, XBRL, OCR, statement and ratio readers, a share-capital note reader with an arithmetic identity check);
- an evidence ledger with provenance, validation and an integrity audit;
- calculation, sector and peer engines; scoring; valuation (residual income for banks, relative P/E, DCF where inputs suffice,
  bear/base/bull scenarios and sensitivity grids); a rule-based verdict (v2) with fixed thresholds;
- an assumption ledger that keeps sourced inputs, rule-derived inputs and user assumptions apart, each with author and rationale;
- a resumable pipeline with an unattended worker, monitoring, research API keys, backup and restore;
- a Next.js dashboard and a 28-45 page PDF report per company.

## B. Final technical verification

Status key: **this closure** = run on 2026-11-16; **earlier** = a previously documented run, not repeated.

| Check | Result | Status |
|---|---|---|
| Backend tests, Windows | 1,981 passed | this closure (before the final fix; isolation tests 19 passed after it) |
| Backend tests, Linux, CI-like (fresh Postgres 17, Python 3.13) | 1,981 passed, 1 skipped (after all fixes) | this closure |
| `ruff check`, `ruff format --check` | clean (334 files) | this closure |
| Migrations | fresh database to head 0023; `alembic check`: no drift | this closure |
| GitHub Actions | run 43831461250 for `68e30d5`: backend, frontend and docker all passed | this closure |
| Docker Compose config; database health | valid; healthy | this closure |
| Pilot validation | 24 of 24 pass, 0 failures | this closure (before the last changes, which touched no valuation or report code) |
| Frontend `tsc`, `eslint`, `vitest` (65), `next build` | clean | earlier, and in the green CI run (lint, tests, Playwright, build) |
| Playwright, mocked API | 22 of 22 | earlier, and in the green CI run |
| Live E2E on the Docker stack, to a downloaded PDF | 4 of 4 | earlier |
| Worker start, recovery after a killed worker | verified | earlier |
| Backup and restore on the 1.4 GB database | verified, 8 tables identical | earlier |
| Upstox, both LLM providers, OCR | verified on small samples | earlier |
| Deterministic reports | 24 of 24 identical | earlier (before the share-count and bank-assumption changes) |
| Full-stack start, PDF generation and live E2E in this closure | not repeated | not verified this closure |
| Hosted production deployment, load, soak, multi-user | never run | not verified |

**A defect found and fixed during closure.** CI had failed since the project's earlier commits. First cause: 16 files not in
`ruff format` layout (fixed, `fe25b92`). Second cause, hidden behind the first: on Linux, stopping a timed-out pipeline stage
killed its parent process, so pytest and the CI runner died ("runner lost communication"); Windows never showed it. Fixed in
`app/pipeline/isolation.py` (`99cbabb`), reproduced and verified on Linux. This also protected the Docker worker, which would
have killed itself on a stage timeout. A third, intermittent failure (the worker skipped its first alert delivery on a host with
under 300 s of uptime, because the timer was compared with `time.monotonic()`) was found when one CI run failed, and fixed
(`68e30d5`) with a regression test. Details in ACCEPTANCE.md.

## C. Feature acceptance

| Capability | State |
|---|---|
| Acquisition, document intelligence, evidence ledger and validation | Complete for the pilot |
| Calculation, sector and peer engines, scoring, verdict v2 | Complete |
| Residual-income valuation (banks) | Complete for ICICIBANK, AXISBANK, HDFCBANK, SBIN (user assumptions). KOTAKBANK: P/B withheld |
| Relative P/E valuation | Available for ten companies |
| DCF valuation | **Unavailable** for all 24 on stored data (see D) |
| Verdict back-test | **Insufficient** (about three years of history) |
| Research workflow, readiness, report and PDF export | Complete; all 24 produce a report |
| Reports needing no documents from the user (readiness `AUTOMATIC`) | INFY, AXISBANK, TCS, TECHM; the rest ask for documents |
| Pipeline, worker, monitoring, keys, backup and restore | Complete |
| Dashboard | Complete |

## D. Known limitations

1. DCF completes for none of the 24 companies: stored history lacks three or more years of EBIT, depreciation, capex, borrowings
   and working capital for most, the Upstox feed has none of them, and the scenario inputs are analyst-owned and not entered.
2. Bank residual-income and peer-P/E values disagree widely (for example SBIN about Rs 643 against about Rs 1,690 per share, base
   case); the verdict inherits it. The residual-income inputs are user assumptions.
3. The verdict back-test cannot support the rules statistically.
4. Eleven companies have no verified share count, so their market cap and P/B are withheld; KOTAKBANK's count predates a split.
5. Integrations and LLM workflows were verified on small samples, not under production-scale conditions.
6. No hosted production deployment, load or operational validation has been run.
7. The Linux verification image had Tesseract; the GitHub runner does not.
8. Scope is the 24-company pilot; universe expansion and an AI copilot were dropped by decision.

## E. Operational handover

Start, configuration, pipeline, reports, tests and backup are documented in [README.md](../README.md). In short:

- configure: copy `.env.example` to `.env`, set `POSTGRES_PASSWORD` (also in `DATABASE_URL`); provider keys are optional;
  create a research key with `python -m app.research.keys_cli add NAME --admin` (written to `.env`, not printed);
- start: `docker compose up -d --build` (migrations run on start); worker: `docker compose --profile worker up -d worker`;
- research: dashboard or `python -m app.pipeline.cli run --symbols ... --no-llm`; reports via the research API or dashboard;
- test: `pytest -q` (one process at a time), `ruff check . && ruff format --check .`, `alembic check`, `npm run lint|test|build|e2e`;
- backup: `./scripts/backup.sh`, `./scripts/restore.sh <file>`;
- documentation: ACCEPTANCE, KNOWN_LIMITATIONS, NEXT_STEPS (optional roadmap), ARCHITECTURE, PIPELINE, RESEARCH_WORKFLOW.

## F. Release readiness

**Development Complete - Production Readiness Pending.**

Evidence: the application starts, the primary workflow runs end to end, tests and CI are green, and all 24 pilot companies
validate. But valuation is not functional for every applicable company (DCF completes for none; one bank has no P/B), the
verdict rules have no statistical support, and nothing has run as a hosted production service. Passing tests does not close
those gaps, so production readiness is not claimed.

## G. Final repository state

- **Source code changed during closure:** yes, three defects fixed: ruff formatting of 16 files (layout only), the process-group
  bug in `app/pipeline/isolation.py`, and the first-alert timer in `app/pipeline/scheduler.py`. No valuation, scoring or verdict logic changed.
- **Documentation updated:** README (rewritten), ACCEPTANCE, KNOWN_LIMITATIONS, NEXT_STEPS (now an optional roadmap), and this report.
- **Tests executed:** yes, listed in B.
- **Data or configuration changed:** user-provided valuation assumptions were entered for HDFCBANK and SBIN (on 2026-11-16, at the
  user's instruction, with audit trails). No other data or configuration changed; no data was deleted.
- **Secrets exposed:** none. No `.env`, key or credential file is tracked; the diff was scanned before commits.
- **Commits and pushes:** made at the user's explicit request: `6a6ebf4`, `fe25b92`, `0f8786b`, `99cbabb`, `c10433b`, `68e30d5` and the commit carrying
  this update. No deployment occurred.
