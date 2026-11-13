# Next steps (updated 2026-11-13, after the universe-coverage work)

Raw documents are no longer available from the project owner, so nothing here waits for uploads.
Documents are *optional accelerators*: if one ever arrives, save it in `data\inbox\<SYMBOL>\` (PDF) or
run `python -m app.docintel.cli xbrl --symbol <SYMBOL> --file <file.xml>` (XBRL), then press
**Resume research**. Companies lacking evidence are shown as *awaiting evidence*, not blocked.

## Continuing the full-universe processing (exact commands)

Run from `backend/` with the virtualenv and the database up (`docker start equityos-db-1`). Every command
is resumable and idempotent; run ONE at a time per host (they share per-host request spacing), and never
two copies of the same command (two concurrent runs can collide on the same company).

```
# where things stand (no network)
.venv\Scripts\python -m app.providers.cli universe --status
.venv\Scripts\python -m app.providers.cli classify --status
.venv\Scripts\python -m app.providers.cli website-status

# 1. industry labels - profile requests only, about 1 s each; repeat until "0 companies"
.venv\Scripts\python -m app.providers.cli classify --limit 500

# 2. Upstox fundamentals - about 20 s per company; stops itself after 3 refusals
.venv\Scripts\python -m app.providers.cli upstox-refresh --limit 50

# 3. official websites - discovery + verification; ambiguous ones are flagged, never guessed
.venv\Scripts\python -m app.providers.cli discover-websites --limit 50

# 4. everything for the next batch in one pass (website, upstox, documents, enrichment, evidence)
.venv\Scripts\python -m app.providers.cli universe --limit 10
.venv\Scripts\python -m app.providers.cli universe --symbols TCS,INFY --steps upstox,evidence --no-llm
```
Re-running any of them skips what is fresh (Upstox 30 days, documents 7, enrichment 30, website back-off
7 to 90 days). Failed websites are listed by `website-status` for a person to confirm with
`set-website`. For long runs use `run_in_background` and read the status commands, not the log
(Python buffers stdout unless started with `-u`).

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
