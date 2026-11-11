# Next steps (updated 2026-11-10, after Phase 12)

Raw documents are no longer available from the project owner, so nothing here waits for uploads.
Documents are *optional accelerators*: if one ever arrives, save it in `data\inbox\<SYMBOL>\` (PDF) or
run `python -m app.docintel.cli xbrl --symbol <SYMBOL> --file <file.xml>` (XBRL), then press
**Resume research**. Companies lacking evidence are shown as *awaiting evidence*, not blocked.

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
