# The research pipeline

One pipeline moves every company from "listed" to "research-ready", and records what happened at each
stage. The `universe` command, runs created in the browser, and the scheduler all use it
(`backend/app/pipeline/`); there is no second implementation.

## Stages (always in this order)

| Stage | What it does | Reuses |
|---|---|---|
| `classify` | provider industry label and business description | `providers/classification.py` |
| `website` | discover, verify and register the official website | `acquisition/website_discovery.py` |
| `upstox` | refresh Upstox fundamentals | `providers/upstox_refresh.py` |
| `documents` | download newly published public documents | `acquisition/evidence_fetch.py` |
| `extract` | type-check, read and promote the stored PDFs (OCR in Docker) | `docintel` (the `extract` command) |
| `evidence` | turn provider facts and extracted figures into evidence, then validate | `evidence/engine.py` |
| `calculate` | derived metrics (growth, margins, returns, valuation) from the validated evidence | `calc/engine.py` |
| `enrich` | verified quotes from the company's own pages | `research/enrich.py` |
| `readiness` | evaluate research readiness; the snapshot is stored | `research/readiness.py` |
| `report` | regenerate an EXISTING report when its inputs changed | `research/report.py` |

A stage ends SUCCEEDED (done, or already current), SKIPPED (nothing it could do; the reason is stored)
or FAILED (an error; retried with back-off). A company with no findable website is a SKIPPED website
stage, not a failure: the registry schedules that retry.

## What "processed" means

A stage finishing does not mean its output reached research. The data path is:

```
download -> type check -> issuer check -> store -> extract -> promote to facts
         -> evidence (sync + validation) -> calculation -> readiness -> report
```

* A downloaded file whose title or name says secretarial, governance, brochure, policy, certificate ...
  is never an annual report or results file (link level, `providers/company_ir.py`), and a file claimed
  to be an annual report that is short and untitled is retyped `other` (`docintel/doctype.py`), with a
  `RETYPED` audit event. Files with no text keep their type and go to OCR.
* A file issued by another entity stays unlinked and is never read.
* `extract` records every outcome on the extraction row. FAILED runs are retried; after 4 consecutive
  failures a document is given up on (an operator can force it). Scans read as NEEDS_OCR are retried
  automatically once OCR is available.
* Provider facts (`financial_facts`) are raw; they become validated statement evidence only in the
  `evidence` stage. A figure the provider did not return stays missing, never zero. Re-syncing is
  idempotent (evidence is versioned by source and content hash).
* Readiness uses evidence only. It says "a stored document ... has not been processed yet" only when
  that is true.

## Checkpoints and resumption

Tables (migration 0022): `pipeline_runs`, `pipeline_stage_states` (latest state per company and stage:
status, message, error, attempts, timestamps, next retry, input fingerprint, structured detail),
`pipeline_events` (append-only stage log), `pipeline_schedule` (one editable row).

* The checkpoint is committed after every stage. A killed process leaves the stage RUNNING; the restart
  repeats exactly that stage (stages are idempotent) and not the earlier ones.
* Within a run, a stage finished earlier in the same run is not repeated when the run is resumed.
* `evidence` and `report` carry an input fingerprint: unchanged inputs mean no repeated work.
  Time-based stages keep their own cool-downs (Upstox 30 days, documents 7, enrichment 30, website
  back-off 7 to 90).
* Automated runs (schedule, retry-failed) wait out a failed stage's back-off (15 min, 1 h, 6 h, 1 day,
  7 days). A manual run retries immediately.
* A company's exception never stops the run or the other companies.

## Running it

From `backend/` (virtualenv, database up). The worker is the only thing that executes runs.

```
.venv\Scripts\python -m app.pipeline.cli status                      # data backlog, stage counts, latest run
.venv\Scripts\python -m app.pipeline.cli status --symbol TCS         # one company, every stage
.venv\Scripts\python -m app.pipeline.cli run --symbols 63MOONS       # one company, every stage
.venv\Scripts\python -m app.pipeline.cli run --symbols TCS --stages extract,evidence,readiness
.venv\Scripts\python -m app.pipeline.cli run --limit 10 --workers 2  # the next batch
.venv\Scripts\python -m app.pipeline.cli run --select missing-evidence --limit 100 --stages evidence,readiness
.venv\Scripts\python -m app.pipeline.cli run --select unread-documents --limit 50 --stages extract,evidence,calculate,readiness
.venv\Scripts\python -m app.pipeline.cli retry-failed --limit 20
.venv\Scripts\python -m app.pipeline.cli pause 7                     # also: resume 7, cancel 7
.venv\Scripts\python -m app.pipeline.cli serve                       # worker: executes queued + scheduled runs
.venv\Scripts\python -m app.pipeline.cli schedule --enable --interval 360 --batch 10 --workers 2
.venv\Scripts\python -m app.pipeline.cli events --run 7
.venv\Scripts\python -m app.pipeline.cli review-websites --limit 20  # re-judge verified websites
.venv\Scripts\python -m app.pipeline.cli mark-website --symbol X --reason "..."
```

`python -m app.providers.cli universe --limit 10` is the same pipeline. `--select` picks companies by
need instead of the next batch: `failed` (retry due), `missing-evidence`, `unread-documents`, or `auto`
(all of those in that order, then the next batch; scheduled runs use `auto`).

Two runs never work on the same stage of the same company at once: the second skips it ("another run is
working on this stage"). A stage left RUNNING for more than an hour is treated as a crash and taken over.

## API (`/api/pipeline`, API key; changes need an admin key)

`GET /overview`, `GET /runs`, `GET /runs/{id}` (companies x stages, events), `GET /companies/{id}`,
`GET /failures`, `GET|PUT /schedule`, `POST /runs` (stages, symbols, batch size, workers, force,
select), `POST /runs/{id}/pause|resume|cancel`. The API only records runs; the worker executes them.
The frontend page `/pipeline` shows these persisted states.

## Time budgets and process isolation

A stage that reaches outside the database (`classify`, `website`, `upstox`, `documents`, `extract`,
`enrich`) runs in a child process when the worker runs with isolation (the default for `run`, `serve`
and the schedule; `--no-isolation` restores in-thread running). The parent waits up to the stage's
wall-clock budget, then stops the child and everything it started (Tesseract included), rolls back and
records the stage FAILED with `timed_out: true` in its stored detail. It is retried under the usual
back-off; whatever the child had committed stays, and the next stage still runs.

Default budgets in seconds: classify 120, website 300, upstox 300, documents 900, extract 1800,
enrich 300, evidence 600, calculate 600, readiness 300, report 600. Override per run with
`--stage-timeout documents=600` (repeatable) or per host with `PIPELINE_TIMEOUT_<STAGE>`. The
database-only stages (`evidence`, `calculate`, `readiness`, `report`) stay in the worker's thread and
are bounded by a Postgres `statement_timeout`; that bound is best effort (Postgres drops a session
setting when a transaction rolls back).

Because each child has its own HTTP client, request spacing per host is also kept in a small shared
file (`data/pacing`, `acquisition/pacing.py`), so children and workers on one machine still wait their
turn. It only adds waiting; if it cannot be used, the in-process spacing still applies.

## Monitoring (`pipeline/metrics.py`)

Everything below is read from stored events, stage states, runs and the schedule row, so every
view (CLI, API, the `/pipeline` page) shows the same picture. Each stage attempt records its
duration and whether it timed out (`pipeline_events.duration_ms`, `timed_out`).

```
.venv\Scripts\python -m app.pipeline.cli metrics --hours 168   # outcomes, p50/p95/max seconds, budget per stage
.venv\Scripts\python -m app.pipeline.cli health                # alerts; exit code 2 when CRITICAL
```

`GET /api/pipeline/metrics` and `GET /api/pipeline/health` serve the same data. Alerts:

| Alert | Level | Meaning |
|---|---|---|
| `RUN_STALLED` | critical | a RUNNING run has been silent for over 10 minutes |
| `WORKER_DOWN` | critical | a run is RUNNING but no worker has reported for 3 minutes |
| `STAGE_STUCK` | critical | a stage has run for more than twice its budget (the worker died or it is wedged) |
| `QUEUE_NOT_SERVED` | warning | a queued run has waited 30 minutes and no worker is running |
| `SCHEDULE_WITHOUT_WORKER`, `SCHEDULE_OVERDUE` | warning | the schedule is on but nothing is starting runs |
| `REPEATED_FAILURES` | warning | companies that failed a stage 3 or more times in a row |
| `HIGH_FAILURE_RATE` | warning | a stage failed 25% or more of at least 10 attempts in the window |
| `STAGE_TIMEOUTS` | warning | attempts that ran out of time in the window |
| `OCR_BACKLOG` | warning | scanned documents wait for OCR (run the worker in Docker) |

The worker records itself every 30 seconds from its own thread (`pipeline_schedule.worker_seen_at`),
and a run records its heartbeat every minute while it works, so a long stage never looks like a dead run.

## Evidence-integrity audit (read-only)

```
.venv\Scripts\python -m app.evidence.cli integrity [--symbol X] [--json]   # exit code 1 when an ERROR exists
```

`GET /api/pipeline/integrity` is the same audit. It writes nothing and repairs nothing. Errors:
duplicate ACTIVE versions of one source record, evidence whose provider fact no longer exists, evidence
whose original value or unit differs from its source fact, document evidence whose extraction was replaced,
evidence drawn from a document filed by another issuer, statement evidence with no basis, one metric in two
units, and calculations whose inputs are no longer ACTIVE. Warnings: provider facts or extracted figures
that never reached the evidence store, future periods, revenue/assets/equity of exactly zero, missing
files. Source keys are unique within a company only, so every lookup includes the company.

## Scheduling and the worker

The schedule is one row (`schedule` command). **A schedule works on selected companies**:

```
.venv\Scripts\python -m app.pipeline.cli schedule --enable --interval 360 --workers 2 --symbols ICICIBANK,TCS
.venv\Scripts\python -m app.pipeline.cli schedule --enable --pilot          # the 24 pilot companies
.venv\Scripts\python -m app.pipeline.cli schedule --disable
```

Enabling it without companies is refused unless `--allow-universe` is given (the universe is not
scheduled by default). A schedule runs every current stage (`--all-stages` forgets a stored list; a
saved list would go stale when a stage is added). The worker keeps polling through a database error,
and a run taken over from a dead worker is resumed from its checkpoints.

Run the worker in Docker so scanned documents are read (Tesseract is in the image):

```
docker compose build backend
docker compose --profile worker up -d worker       # restarts itself; uses the same image as the API
docker compose run --rm --no-deps backend python -m app.docintel.cli ocr-check   # proves OCR works there
```

`ocr-check` draws two statement lines, turns them into a picture-only PDF (a true scan) and reads it
through the same path real scans take; it fails loudly if OCR is missing.

## Rate limits and concurrency

HTTP requests are spaced per host by the shared polite client (robots.txt obeyed, no bypass of 403 or
bot protection). On top of that, `--workers N` runs N companies at once with provider gates: Upstox is
one request stream however many workers run, web stages use up to N threads, CPU stages (extraction,
validation) up to N/2. Each worker has its own database session.

## Website verification

A name match is not enough. A candidate is AMBIGUOUS (a person decides) when the page is a domain
dispute or parked page, is mostly in a script the check cannot read, or does not describe the business
the data provider describes (fewer than 20% of the profile's distinctive words appear on the page),
unless the page prints the ISIN. `review-websites` re-applies the current checks to verified sites;
a withdrawn site keeps its history and its domain is remembered so discovery cannot re-accept it. A
site set by hand is never touched.
