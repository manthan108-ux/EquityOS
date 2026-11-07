# Phase 5 stabilization report (2026-11-08)

Scope: the 12-point stabilization brief for Phase 5 (document intelligence), before Phase 6.
Phase 6 was **not** started.

**Unit tests vs real-world accuracy.** The 443 backend tests prove the rules work on synthetic and
generated PDFs. Real-world accuracy is measured separately, on the stored ICICI Bank results,
ICICI Bank annual report and Reliance filing (section "Real-document evaluation"). A passing test
suite does not mean extraction is accurate on every company; the limits are in
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## 1. Issues found by the audit (priority order) and what was done

| # | Issue | Fix | Status |
|---|---|---|---|
| 1 | **Critical.** Docker used a different data folder (`/app/data` named volume): the container could not see any stored document | One `DATA_DIR`; Docker bind-mounts the same `./data`. `app/core/storage.py`, `storage-check`, `GET /api/storage/status` | Fixed, verified in Docker |
| 2 | ICICI Prudential AMC deck linked to ICICI Bank; no audit trail for link changes | Issuer read from the first page; `document_link_events`; unlink (your approval); ingestion-time check; coverage excludes mismatches | Fixed |
| 3 | Re-running extraction piled up duplicate runs (1,674 stale rows) | Runs are superseded, never deleted (`superseded_at`); readers use the current run | Fixed |
| 4 | Half-year / nine-month figures unusable | `HALF_YEAR`, `NINE_MONTH` types; `derive-quarters` | Fixed |
| 5 | No OCR; one odd page or error aborted a whole run | Tesseract (Docker); savepoint-isolated runs; per-page error isolation | Fixed (Windows install needs admin) |
| 6 | Only the three main statements | Segments, ratios, schedules, shareholding, related-party roster, narrative sections | Done for the formats found in the real documents |
| 7 | TCS / Infosys / HDFC Bank: no documents and coverage silent about why | Coverage states why; HDFC discovery fixed; inbox ingestion | Partly (see below) |
| 8 | AGPL PDF dependency | Alternatives implemented and compared | Evaluated, not replaced |
| 9 | Missing foreign-key indexes; restated values only noted in text | 11 indexes; `financial_facts.history` | Fixed |

## 2. Details per requirement

### 2.1 Unified storage (#1)
`DATA_DIR` (default `data`) is the only setting. Layout: `documents/<aa>/<sha256>.pdf`, `cache/`,
`inbox/<SYMBOL>/`. The database stores only the relative path, so the same rows work locally and in
Docker. `docker-compose.yml` mounts `${DATA_HOST_DIR:-./data}:/app/data` (a host folder, so it
survives rebuilds and `down -v`); on Linux set `DATA_UID`/`DATA_GID`. Verified: the container sees
the same 4 documents, hashes match, extraction ran in Docker against the same database.
The old `equityos_appdata` volume only held an HTTP cache and is unused (`docker volume rm
equityos_appdata` is safe). Uploads are CLI-only (`ingest-document`, `ingest-inbox`): the API has no
authentication, so it deliberately has no upload endpoint.

### 2.2 Company-document association (#2)
`audit-associations` (read-only by default) classifies each document from its own first page:
CONFIRMED / MISMATCH / UNVERIFIED with a relation (SAME_ENTITY, SEPARATELY_LISTED,
POSSIBLE_GROUP_ENTITY, OTHER_ENTITY, or the relationship the company's **own filing** states:
SUBSIDIARY, ASSOCIATE, ...). Parent/subsidiary status is never inferred from names: ICICI Bank's
annual report (page 228) lists ICICI Prudential Asset Management Company Limited as "Subsidiary",
and that is what is reported. Letters addressed *to* an exchange (BSE/NSE) are not mistaken for the
issuer. On your approval the AMC deck was unlinked: file and document row kept, 0 validated values
withdrawn, 246 staged values detached, audit event written (see "Mistakes" below).

### 2.3 Extended extraction (#3)
Every value keeps its document and page. Disclosure tables are stored in `extracted_facts`
(`statement` = SEGMENT, RATIOS, NOTE, SHAREHOLDING; `dimension` = segment name or schedule title)
and are "validated" (`promoted = true`) only when their arithmetic holds and the column period
layout is proven. They are served by `GET /api/companies/{id}/disclosures`, not written to
`financial_facts`. Auditor's report, accounting policies and management discussion are stored as
sections (page range, excerpt, explicit quoted flags such as opinion type and Key Audit Matters).
The group roster is `group_entities`. Not extracted: related-party transaction amounts, contingent
liability narrative tables, EPS beyond the statements, "management commentary" other than the
Management Discussion section.

### 2.4 Periods (#4)
Standalone quarters (3 months), cumulative half-year (6 months to September) and nine-month
(9 months to December) figures, and years are separate period types; an April-March fiscal year is
assumed. `derive-quarters` computes Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M only for the same
company, statement, basis and line, with periods exactly one quarter apart, additive amounts only
(never EPS), and stores the result as provider `DERIVED_QUARTER` with the exact inputs in
`derived_from`. If the company also reported the quarter, nothing is derived and the two are
compared instead. Reported figures are never overwritten.

### 2.5 OCR (#5)
Born-digital pages are never OCR'd. Scanned pages use Tesseract (command line, TSV with per-word
confidence). Safeguards: look-alike characters (O/0, l/1, S/5) and confidence below 80 flag a value
for review; on scans with an embedded OCR layer a value is promoted only if (a) an arithmetic check
confirmed it **and** (b) an independent Tesseract reading of the same cell agrees (tolerating
border strokes and a thousands comma read as a point); anything else is `needs_review` and not
promoted. Header repairs fix only month names one edit away and the "30th" suffix read as 30%,
3074, 301h...; a date must still be a valid month-end, values are never repaired.

### 2.6 Missing companies (#6)
Measured 2026-11-08: tcs.com and infosys.com return HTTP 403 for every page (not bypassed).
HDFC Bank's investor-relations page is reachable and lists 67 documents (annual reports FY2023-
FY2026, results back to FY2024); discovery now tries standard investor-relations paths when the
homepage does not lead there. **But every PDF download from hdfcbank.com timed out** (3 attempts
each), so no HDFC document could be fetched automatically; documents must come through the inbox.
Coverage now says so explicitly ("automated retrieval from ... failed ... Missing documents do NOT
mean the company did not publish them"); a company is never "complete" because a site was down.
Inbox: save PDFs to `data/inbox/<SYMBOL>/`, run `python -m app.docintel.cli ingest-inbox --symbol X`.
Each file is checked for being a real PDF, typed from its name/content (refused if unsure), checked
for its issuer, stored, and moved to `done/` or `rejected/` (with a `.reason.txt`).

### 2.7 PDF library licence (#7)
PyMuPDF is **AGPL-3.0** (or commercial). For private research use there is no obligation; if
EquityOS is ever offered to others as a network service, its source must be offered under AGPL, or a
commercial licence bought, or the backend replaced. Tested alternatives, same real pages, full
statement extraction:

| Backend | Licence | ICICI results (9 pages) | ICICI annual report (11 pages) |
|---|---|---|---|
| PyMuPDF 1.26.5 | AGPL-3.0 | 904 values, 216/222 checks, 0.31 s | 286 values, 68/68 checks |
| pdfminer.six | MIT | 880 identical, 24 missing, **0 different**, 1.9 s | **286/286 identical**, 68/68 checks |
| pypdfium2 | Apache-2.0/BSD | 808 identical, 96 missing, 0 different | 93/286 identical (weak word boxes) |

Conclusion: pdfminer.six reproduces PyMuPDF's text and positions with no wrong values and at most a
few missed ones (about 6x slower on small PDFs). Switching also needs (i) embedded-OCR detection
(PyMuPDF reads the invisible-text render mode; pdfminer cannot, so a conservative rule "full-page
image + text = scan" would be needed) and (ii) page rendering for OCR (pypdfium2 can render). OCR
recognition is already independent (Tesseract, Apache-2.0). Not switched: no deployment decision
yet; the PDF access is confined to `app/docintel/pdf.py`.

### 2.8 Integrity audit (#8)
Verified by tests and real documents: original value, unit and period label are stored beside the
normalised value; restatements move the earlier value into `financial_facts.history`; consolidated
and standalone never share a key; the company and issuer are checked; nil cells ("-", "..") are
nil, never 0, and are not promoted; ambiguous/duplicate/failed values are refused; duplicate
documents are one row (content hash); in-document conflicts are withdrawn; same figure in two
documents is cross-recorded. Measured on the real documents: **correct statements pass with zero
relative slack**, so the tolerance was tightened (0.001%) plus a per-unit rounding allowance; share
counts need an exact match. One cross-document check found a real taxonomy error earlier (provisions
excluding vs including tax). Unit conversion is exact `Decimal` arithmetic.

### 2.9 Database / architecture audit (#9)
Migration `0006`: `documents` (issuer, association status/note), `document_link_events`,
`document_extractions` (`superseded_at`, `ocr_engine`), `document_pages.ocr_confidence`,
`extracted_facts` (`dimension`, `needs_review`, `ocr_confidence`), `document_sections`,
`group_entities`, `financial_facts` (`derived_from`, `history`, new period types) and 11 indexes
(FKs on the Phase 5 tables, `financial_facts.payload_id`, `dataset_verifications.company_id`,
`documents.source_id`). Left as is, on purpose: `companies.industry_id/sector_id` and
`securities.last_import_id` FK indexes (small tables). Extraction runs inside a savepoint; failures
are recorded as FAILED and never abort a batch. Performance: the 349-page annual report extracts in
about 10 s; OCR costs about 5-30 s per scanned page set.

## 3. Real-document evaluation (current extraction runs, 2026-11-08)

Validated = arithmetic holds, period layout proven, issuer confirmed (scans also need the second
reading). Numbers are "staged -> validated".

| Document | Primary statements | Segments | Ratios | Schedules | Shareholding |
|---|---|---|---|---|---|
| ICICI Bank Q2 FY2027 results (16 p.) | 442 -> 418 | 408 -> 408 | 84 -> 78 | - | - |
| ICICI Bank FY2026 annual report (349 p.) | 146 -> 81 | - | - | 730 -> 596 (10/13 schedules fully; Schedule 9 partly; Schedule 10 fails honestly, it is a roll-forward) | 24 -> 24 |
| Reliance Q2 FY2027 scanned filing (61 p.) | 334 -> 92 | 258 -> 128 | 84 -> 0 (columns unreadable) | - | - |

* Arithmetic checks on the ICICI results: 210/210 pass. ICICI annual report: 128 pass, 20 fail (all
  in fixed-asset roll-forward and one advances total; the failing values are withheld).
* Cross-period consistency: ICICI 36 and Reliance 15 half-year figures equal Q1 + Q2 exactly; 0
  inconsistent.
* Reliance with the Tesseract cross-check (in Docker): values withheld when the second reading
  disagreed included cases where **Tesseract was wrong** (`12,343` read as `42,343`) and cases where
  the embedded text may be; all are flagged `needs_review`, none promoted.
* Fully scanned test (embedded text stripped from three Reliance pages, then read by Tesseract
  alone): numbers were often right (5 of 6 checks passed on the income page) but column dates were
  garbled (`30% Jun'26`, `Ants September zrzs`), so most columns were refused. This is the honest
  limit of OCR on dense six-column statements.
* Group roster of ICICI Bank: 17 subsidiaries, 7 associates, 2 other related entities, 1 consolidated
  fund; 2 matched to companies in our universe.

## 4. Mistakes made during this work
* Regenerating migration 0006 required a `downgrade`, which dropped `document_link_events` and the
  association columns **including the audit row for the approved unlink**. The unlink itself (the
  empty `company_id`) was untouched; the audit row and statuses were re-created and the audit row is
  marked as a restoration. Rule adopted: never downgrade the live database; generate migrations
  against a scratch database.
* Several regular expressions written through the shell had `\b` turned into a backspace character
  (known tooling gotcha). A scan of all code found and fixed the occurrences; tests cover the
  affected rules.

## 5. Files changed / added (summary)
New: `app/core/storage.py`, `app/docintel/{association,derive,disclosures,inbox,ocr}.py`,
`migrations/versions/0006_phase_5_stabilization.py`, `tests/docintel_tables.py`,
`tests/test_docintel_stabilization.py`, docs. Changed: `pdf.py`, `statements.py`, `taxonomy.py`,
`validate.py`, `detect.py`, `pipeline.py`, `provider.py`, `cli.py`, `api/documents.py`,
`coverage.py`, `providers/{documents,manual,cli,company_ir,registry}.py`, models, `normalization.py`,
`Dockerfile`, `docker-compose.yml`, `.env.example`.

## 6. Manual actions for you
1. TCS / Infosys / HDFC Bank: download their annual reports and results in a browser into
   `data/inbox/TCS/` (etc.), then `ingest-inbox` and `extract`.
2. Windows OCR: run `winget install UB-Mannheim.TesseractOCR` in an **administrator** terminal if you
   want OCR outside Docker (`python -m app.docintel.cli ocr-status` should then show a version).
3. Upstox: put the token in `.env` as `UPSTOX_ANALYTICS_TOKEN` (never in chat) when it arrives.
4. Optional: `docker volume rm equityos_appdata`.
5. Decide whether to switch the PDF backend (section 2.7) before any public deployment.
