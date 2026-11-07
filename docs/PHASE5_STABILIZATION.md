# Phase 5 stabilization (in progress, 2026-11-08)

Status: PARTIAL. Work stopped when the usage limit was reached. Everything below marked DONE is
tested (408 backend tests) and checked on the real ICICI Bank and Reliance documents.

## Done
- **Storage unified.** One `DATA_DIR`; Docker bind-mounts the same `./data` folder. `storage-check`
  CLI and `GET /api/storage/status`. Verified locally and in Docker (4 documents, hashes match).
- **Company association.** Issuer read from each document's first page. Audit table
  `document_link_events`. With your approval the ICICI Prudential AMC deck was unlinked (file kept,
  0 validated values withdrawn). ICICI Bank's own annual report lists the AMC as "Subsidiary"
  (page 228); this is stored in `group_entities` and shown by `audit-associations`.
  New documents are checked at ingestion (mismatch = stored but not linked / manual ingest refused).
- **Half-year / nine-month periods.** HALF_YEAR and NINE_MONTH stored as cumulative figures;
  `derive-quarters` derives standalone quarters only from exactly nested periods. Real check:
  36 ICICI and 10 Reliance half-year figures equal Q1+Q2 exactly, 0 inconsistencies.
- **Run handling.** Re-runs supersede (never delete) earlier runs; failures roll back to a savepoint
  and are recorded; page errors are isolated.
- **Extra disclosures.** Segments (ICICI standalone + consolidated, Reliance scanned: all
  identities pass), analytical ratios, schedules with total checks (10 of 13 ICICI schedules pass
  fully), shareholding pattern (sums exactly), related-party roster, auditor's report /
  accounting policies / management discussion sections with quoted flags.
- **OCR.** Tesseract 5.5 in the Docker image (per-word confidence, look-alike-character flags,
  independent second reading of scanned pages, header repairs). Windows install needs admin.
- **Integrity.** Checks measured on real data: zero relative slack is enough, tolerances tightened.

## NOT done
- Tests for the new modules (association, derive, ocr, disclosures, storage, supersede) beyond the
  existing 408; synthetic builders are in `tests/docintel_tables.py` but no test file uses them yet.
- Schedule 5 fixture needs its own column header (real filings repeat it).
- Inbox-based manual ingestion; probing TCS / Infosys / HDFC Bank sources.
- PDF licence evaluation (pypdfium2 / pdfminer alternative) - PyMuPDF (AGPL) still in use.
- API endpoints for disclosures; docs: KNOWN_LIMITATIONS.md, PROJECT_STATUS.md update.
- Docker volume `equityos_appdata` is now unused (safe to remove).
- Fully scanned dense statements: Tesseract reads many numbers but often garbles column dates,
  so those columns are refused and flagged, not guessed.
- Mistake to note: regenerating migration 0006 required a downgrade that dropped the audit rows;
  they were restored (marked as restored in the audit table).
