# Next steps (updated 2026-11-14, after the integrated pipeline)

Raw documents are no longer available from the project owner, so nothing here waits for uploads.
Documents are *optional accelerators*: if one ever arrives, save it in `data\inbox\<SYMBOL>\` (PDF) or
run `python -m app.docintel.cli xbrl --symbol <SYMBOL> --file <file.xml>` (XBRL), then press
**Resume research**. Companies lacking evidence are shown as *awaiting evidence*, not blocked.

## Continuing the full-universe processing (exact commands)

One pipeline does everything now (see [PIPELINE.md](PIPELINE.md)). Run from `backend/` with the
virtualenv and database up (`docker start equityos-db-1`). Every command resumes where it stopped.
Run ONE pipeline process at a time per host; the stage guard stops two runs working on the same
company, but they would still share the sites' request spacing.

```
# where things stand (no network)
.venv\Scripts\python -m app.pipeline.cli status
.venv\Scripts\python -m app.providers.cli universe --status

# 1. keep the backlogs at zero (local, fast, no network)
.venv\Scripts\python -m app.pipeline.cli run --select missing-evidence --limit 200 --stages evidence,calculate,readiness
.venv\Scripts\python -m app.pipeline.cli run --select unread-documents --limit 50 --stages extract,evidence,calculate,readiness

# 2. industry labels for the rest of the universe (profile requests only, about 1 s each)
.venv\Scripts\python -m app.providers.cli classify --limit 500

# 3. the next batch, every stage (about 2 min per company; Upstox alone is about 20 s each)
.venv\Scripts\python -m app.pipeline.cli run --limit 25 --workers 3

# 4. retry what failed (waits out each stage's back-off)
.venv\Scripts\python -m app.pipeline.cli retry-failed --limit 25

# 5. unattended: schedule, then run the worker (it also executes runs queued from the browser)
.venv\Scripts\python -m app.pipeline.cli schedule --enable --interval 360 --batch 25 --workers 3
.venv\Scripts\python -m app.pipeline.cli serve
```

Scanned PDFs need Tesseract, which only the Docker image has. To run any command above with OCR,
without rebuilding the image, mount the current code over it:

```
docker compose run --rm --no-deps -v "<repo>/backend/app:/app/app" backend python -m app.pipeline.cli run --select unread-documents --stages extract,evidence,calculate,readiness
```

(on Windows use the `C:/...` form of the path). To make this permanent, rebuild: `docker compose build backend`.

Pause, resume, cancel: `python -m app.pipeline.cli pause|resume|cancel <run id>`, or the buttons on the
`/pipeline` page (needs an admin key). `events --run <id>` is the stage log. Failed websites are listed
by `website-status`; confirm one by hand with `set-website`; withdraw a wrong one with `mark-website`.
Re-judge verified sites with `review-websites`.

## Before the full-universe run

The 5-, 25- and backlog runs are done and verified. These still decide whether a long unattended run is
safe, in this order:

1. **Review the 15 AMBIGUOUS websites** (`website-status`), including AAA Technologies, ABM International
   and AVRO INDIA (withdrawn by the new checks; they may be right) and AGI Greenpac, ACS Technologies,
   AA Plus Tradelink. Use `set-website` for the ones you confirm. Do not guess domains.
2. **Add a wall-clock budget per stage** (one `documents` stage ran 33 minutes on a slow site) so a slow
   host cannot hold a worker. Until then keep `--workers` at 3 or fewer and watch `/pipeline`.
3. **Run the worker in Docker** (or rebuild the image) so scanned documents are read during the run,
   not left NEEDS_OCR.
4. **Try one unattended scheduled run** (`schedule --enable --interval 60 --batch 5`, `serve`) and read
   `/pipeline` after it. Scheduled and unattended execution has been exercised by tests and the
   worker, not yet left alone for hours.
5. Decide the basis question for small companies: most have only standalone statements, so readiness
   (default consolidated) shows them as "exist on the other basis". That is the user's choice per
   report; changing the default is a product decision.

## Open after the 2026-11-13 work
1. **Coverage** is a small share of 5,551 companies; the batches above are the way to raise it. Expect
   most refusals to come from sites that disallow automated access; those need `set-website` by hand or
   stay unprocessed.
2. **SBI scan**: only 2 of 12 income columns have a readable heading. Options: upload the Excel/XBRL file,
   or improve header repair for "Quarter ended / Half year ended" in `docintel/ocr.py`.
3. **Re-process the stored Nestle and SBI uploads** ("process again" in the Documents tab) to apply the new
   readers; this changes stored records, so it was not done automatically.
4. **BSE industry list** (manual download to `data/master/bse_Equity.csv`) would give an exchange
   classification to put beside the provider label.
5. **Industry-group coverage** grows with `upstox-refresh`; until a label has 5 refreshed companies with the
   same fiscal years its statistics are withheld.
6. Windows Tesseract install (administrator prompt) would let local runs read scans; Docker already can.

## Done
* Automatic evidence fetch from the company's own website (Phase 13); BPCL went from no documents to a score with no upload.
* Basis-safe P/B; verified corporate actions (HDFC Bank bonus applied, raw prices preserved);
  adjusted prices stored separately; bank NIM/NPA/CAR/CASA in absolute scoring; offline XBRL reader
  with raw preservation; browser tests in CI.

## Decisions already made (do not re-ask)
XBRL offline only, no automated NSE/BSE download until terms are clarified in writing; PyMuPDF for
private development behind `docintel/pdf.py`; share counts carry a basis; adjustments need
verification and raw prices stay untouched.

## Still genuinely open (needs real data or an external answer)
1. **Real XBRL validation** - the reader has only been tested on synthetic files. When one real
   exchange filing is available, run it with `--dry-run` and extend `xbrl_map.py`.
2. **NSE terms** for automated retrieval (written reply) - blocks any downloader.
3. **PyMuPDF licence** before any public release.
4. **Reliance 28 Oct 2024** price gap: needs a stored official announcement to verify.
5. **SBI/HDFC statements** are scans; interest earned/expended, shares outstanding and P/B stay
   missing until a text/XBRL filing exists.
6. Upstox-only companies lack D&A, finance costs and borrowings (operating margin, debt/equity).

## Engineering backlog (no new data needed)
1. Reprocess all 24 companies against the new parsers (two-up pages, heading durations) and publish a coverage/parser-gap report; add websites with `set-website` for companies beyond the 24.
2. Source-health and ingestion-status view on the dashboard (source unavailable / not published /
   retrieved but extraction failed / not disclosed / unmapped / validated / conflict).
3. Score-history explanation when scores move; recalculate only when validated data changes.
4. Operating readers: track actual source coverage separately from parser capability.
5. RBI/DBIE feasibility note (company-level coverage must be proven before any use).

## Report engine follow-ups (after the 2026-11-11 report transformation)
1. Business-model and bank-franchise analysis (CASA, retail/corporate mix, fee income) needs investor
   presentation / Pillar 3 figures; extend `docintel/disclosures.py` first.
2. Carry document titles and filing dates in report content so the source register names documents.
3. Operating metrics (vehicle volumes, IT attrition/utilisation) into the sector profiles once the
   readers have collected data.
4. A DCF for non-banks needs a documented forecast and discount-rate policy (user decision).
5. Peer coverage: HDFC, Kotak and SBI consolidated ROE/P-B (extraction) so bank comparisons need not
   fall back to the standalone basis.

## Milestone 1 commands (2026-11-14)

```
# is it healthy? (exit code 2 when CRITICAL)
.venv\Scripts\python -m app.pipeline.cli health
.venv\Scripts\python -m app.pipeline.cli metrics --hours 168
.venv\Scripts\python -m app.evidence.cli integrity            # read-only audit; exit code 1 on an ERROR

# unattended, selected companies only, in Docker (OCR included)
docker compose build backend
docker compose --profile worker up -d worker
.venv\Scripts\python -m app.pipeline.cli schedule --enable --interval 360 --workers 2 --symbols ICICIBANK,TCS
.venv\Scripts\python -m app.pipeline.cli schedule --disable
docker compose --profile worker stop worker
docker compose run --rm --no-deps backend python -m app.docintel.cli ocr-check
```

Run the schedule for a few hours on the selected companies and read `/pipeline` before trusting it with
more. Roadmap order after Milestone 1: financial statement depth, sector peer metrics, DCF and sector
valuation, management and governance, business model and segments, company-specific risks and thesis,
report quality gate.
