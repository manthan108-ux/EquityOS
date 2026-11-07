# Data Acquisition Strategy (multi-source)

**Decision:** EquityOS does not depend on any single exchange API. Financial data comes from a
*provider router* over many sources, each with a declared scope, a documented access method and
recorded proof (a live retrieval) before it is trusted. We do not bypass protected exchange APIs,
spoof browsers, reverse engineer private endpoints or circumvent access controls.

## Architecture

```
research agents / jobs
        │  ask for a DATASET (e.g. BALANCE_SHEET) for a company
        ▼
  Data Provider Router ── tries providers in documented priority order
        │                  returns: chosen provider + EVERY attempt (no silent substitution)
        ▼
  Provider adapters (each declares its datasets; none is assumed to support everything)
   DocumentExtraction · UpstoxFundamentals · MarketData(NSE/BSE files) · NSE · BSE · CompanyIR
   · Regulator · News · Government · Manual (documents, CSV exports)
        │  all HTTP through one polite, audited client (robots.txt, pacing, retries, cache, log)
        ▼
  Normalized fact tables (+ raw payload archive)
   financial_facts · shareholding_facts · corporate_actions · ratio_snapshots · eod_prices
   · documents · raw_payloads · dataset_verifications
        ▼
  Data Coverage Engine ── per dataset: required vs available periods, source, freshness,
                          verification, confidence, limitations, affected research pillars
```

* **Declared vs verified.** A provider *declares* datasets in its spec. A dataset counts as
  supported only when `dataset_verifications` holds a successful **live** retrieval
  (`python -m app.providers.cli verify ...`). `GET /api/providers/status` shows both lists.
* **Registry.** Each spec records name, supported datasets, authentication, access method, rate
  limits, coverage, terms/licensing notes, redistribution policy, availability (configured?) and
  last successful retrieval. It is mirrored into the `sources` table.
* **Router rules.** Only providers that declare a dataset are asked. Unconfigured providers are
  skipped and reported. Priority order is fixed in `app/providers/router.py`. A fallback is always
  labelled; a provider crash is contained and reported without leaking details.
* **Normalization.** Every fact keeps the original value, original unit and original period label
  next to the normalized value (crore / lakh / million / billion to rupees; per-share values are
  never scaled). Consolidated and standalone are separate rows and are never blended. Unreadable
  periods or missing values are skipped and counted, never filled. Providers' restated values
  refresh the fact while every earlier raw response stays archived in `raw_payloads`.

## Provider evaluation (as of 2026-11-07)

| Provider | Datasets (declared) | Access | Live-verified now? |
|---|---|---|---|
| **UpstoxFundamentals** | profile, income (yearly + quarterly), balance sheet, cash flow, shareholding, key ratios, corporate actions, competitors | Documented REST API keyed by ISIN; Bearer token | **No.** Endpoints exist (an unauthenticated call returns a structured `401 UDAPI100050`), but no token is configured, so nothing has been retrieved. |
| MarketData (NSE + BSE static files) | end-of-day prices | Public daily files | **Yes**, all 5 test companies, 247 trading days |
| NSE archives | security master, name/symbol history | Public static files | **Yes** |
| BSE list | security master | Manual download | **Yes** (listing present for all 5) |
| Company IR | annual reports, results, presentations, transcripts (links) | Polite crawl of the company site | **Partly**: works for ICICI Bank and Reliance; TCS and Infosys return HTTP 403; HDFC Bank's pages exposed no links |
| Regulator feeds | RBI, SEBI publications | RSS | **Yes** |
| News feeds | headlines | Publisher RSS | **Yes** (6/6 feeds) |
| Government (data.gov.in) | statistics by resource id | Documented API, free key | No (no key configured) |
| Manual | documents, financial CSV, announcements | Local files | Yes (tested with fixtures; always available) |
| **Document extraction** (Phase 5) | income (quarterly + annual), balance sheet, cash flow, read from stored official PDFs | Local deterministic parsing; arithmetic checks before use | **Yes** for ICICI Bank (results + annual report) and Reliance (integrated filing); see [DOCUMENT_INTELLIGENCE.md](DOCUMENT_INTELLIGENCE.md) |

### Upstox Fundamentals API: what the documentation says

* Endpoints (GET, `https://api.upstox.com/v2/fundamentals/{isin}/…`): `profile`,
  `income-statement` (`type=consolidated|standalone`, `time_period=yearly|quarterly`, `fs=true`
  adds the annual full statement), `balance-sheet`, `cash-flow`, `share-holdings`, `key-ratios`,
  `corporate-actions`, `competitors`. Launched 2026-05-11. Values in Indian rupees, **crore**.
* Authentication: OAuth2 bearer token. A **free, read-only Analytics Token** (valid 1 year, no
  static IP, includes Fundamentals, cannot trade) is generated on the Upstox Developer Apps page,
  Analytics tab. Normal access tokens expire at 3:30 AM daily.
* Not documented (so not assumed): rate limits for fundamentals, how many companies and years are
  covered (examples show about 4 annual periods), eligibility beyond holding an app, cost, and
  data-use terms. Upstox's AI-licensing page restricts republishing its content as a competing
  product. EquityOS uses the data privately for analysis, attributes it, and never redistributes
  the raw feed.
* **To enable:** put your token in `.env` as `UPSTOX_ANALYTICS_TOKEN=...` (never commit it), then run
  `python -m app.providers.cli verify --provider UPSTOX_FUNDAMENTALS --symbols RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK`.
  Only datasets that pass are then reported as verified. Until then the adapter returns
  `UNCONFIGURED` and no Upstox data exists in the system.
* The adapter's parsing is tested against the documentation's example responses, which proves our
  logic only, not that Upstox serves the data.

## Coverage engine

`GET /api/companies/{id}/coverage` or `python -m app.providers.cli coverage --symbol X`.
Per dataset: required periods (5 fiscal years / 8 quarters / ~1 year of prices / 3 annual reports /
4 quarters of documents), available, missing, sources (with verified flag), freshness, verification
status (`VERIFIED`, `SINGLE_SOURCE`, `UNVERIFIED_SOURCE`, `USER_SUPPLIED`, `CONFLICT`,
`NOT_AVAILABLE`), confidence (`HIGH/MEDIUM/LOW/NONE` with written reasons, not a probability),
limitations, and the research pillars that become weaker. Research continues with partial coverage
(`can_proceed` is always true) but affected conclusions are flagged. The completeness score counts
only data that exists.

### Five-company comparison (2026-11-07, no Upstox token, no manual data)

| Dataset | Reliance | TCS | Infosys | HDFC Bank | ICICI Bank |
|---|---|---|---|---|---|
| Prices (end of day) | Market data 100% | 100% | 100% | 100% | 100% |
| Annual reports | unavailable | unavailable | unavailable | unavailable | Company IR 33% |
| Quarterly result documents | unavailable | unavailable | unavailable | unavailable | Company IR 25% |
| Investor presentations | unavailable | unavailable | unavailable | unavailable | Company IR 25% |
| Income, balance sheet, cash flow, shareholding, ratios, actions, peers, profile | unavailable | unavailable | unavailable | unavailable | unavailable |
| Earnings transcripts | unavailable | unavailable | unavailable | unavailable | unavailable |
| Corporate announcements | no permitted source | no permitted source | no permitted source | no permitted source | no permitted source |
| News | on demand | on demand | on demand | on demand | on demand |

Fundamentals will remain unavailable until either an Upstox token is configured and verified or
financial CSV exports / documents are supplied manually.

### After Phase 5 (2026-11-07, still no Upstox token)

| Dataset | Reliance | TCS | Infosys | HDFC Bank | ICICI Bank |
|---|---|---|---|---|---|
| Annual income statement | Documents 20% | unavailable | unavailable | unavailable | Documents 40% |
| Quarterly results | Documents 25% | unavailable | unavailable | unavailable | Documents 38% |
| Balance sheet | Documents 20% | unavailable | unavailable | unavailable | Documents 40% |
| Cash flow statement | unavailable (scanned page, unit unreadable) | unavailable | unavailable | unavailable | Documents 40% |
| Quarterly result documents | Company site 25% | unavailable | unavailable | unavailable | Company site 25% |

Router priority for statements is now: document extraction, then Upstox, then manual imports.

## Ways to supply fundamentals yourself (always available)

```bash
# Financial statements from an export you are entitled to use (strict, all-or-nothing CSV; see app/providers/manual.py)
python -m app.providers.cli import-financials --file my_financials.csv
# A report / filing / presentation / announcement as a document (type, period labels FY2025 or Q2FY2026)
python -m app.providers.cli ingest-document --symbol TCS --type annual_report --file AR.pdf --period FY2025
# Download documents the company website exposes (periods inferred from link text; confirm them)
python -m app.providers.cli fetch-documents --symbol ICICIBANK --kind annual_report --limit 3
```
