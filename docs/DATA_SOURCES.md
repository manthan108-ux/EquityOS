# Data Sources

Principles (from the project specification): official sources first; respect each site's terms, robots rules and rate limits; never bypass authentication, paywalls, bot protection or access denials; cache downloads; report any data we could not obtain; never present old data as current.

Everything goes through one audited client (`app/acquisition/http.py`) which: identifies itself honestly (`EquityOS-research/0.1 (+repo URL)`, never a browser), obeys robots.txt (RFC 9309), spaces requests per host, retries only transient failures with backoff and `Retry-After`, validates content (some sites answer HTTP 200 with an error page), caches on disk, and writes every fetch to the `acquisition_log` table. `GET /api/providers/status` shows per-source reliability from that log.

## What works (checked 2026-11-07)

| Source | What we get | Tier | Notes |
|---|---|---|---|
| NSE archive: `EQUITY_L.csv`, `SME_EQUITY_L.csv` | Every NSE-listed equity (main board + SME): symbol, name, ISIN, listing date, face value | 1 | Automated: `acquisition.cli nse-equity-list` |
| NSE archive: `namechange.csv`, `symbolchange.csv` | Official former names and former tickers (e.g. Zomato to Eternal) | 1 | Automated: `acquisition.cli name-history` |
| NSE archive: `sec_bhavdata_full_DDMMYYYY.csv` | Official end-of-day OHLC, volume, turnover, delivery % for every NSE security, history back to at least 2020 | 1 | Automated: `acquisition.cli prices --exchange NSE` |
| BSE static: `BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV` | Official end-of-day OHLC, volume, turnover for every BSE security | 1 | Automated: `... prices --exchange BSE`. BSE throttles with HTTP 406 (clears in about 30 s) and serves HTTP 200 error pages under bursts; the client spaces requests, backs off 30 s and validates content. Older days are often unavailable. |
| BSE list of scrips (equity) | Every BSE-listed equity: code, name, ISIN, group | 1 | **Manual download** in a browser, then `master_data.cli` (BSE's API is blocked, see below). Can be re-derived from the BSE bhavcopy if needed. |
| RBI press-release RSS, SEBI announcements RSS | Regulator publications | 3 | `RbiPressReleases`, `SebiAnnouncements` |
| Mint, Economic Times, Moneycontrol, BusinessLine RSS | Recent headlines and links, filtered locally for a company name | 5 | Headlines are leads, never proof. Feeds hold only recent items: a window, not history. |
| Company websites | Investor-relations / annual-report / results link discovery from the official site | 2 | Per-site: `ril.com` works, `tcs.com` returns 403 |
| Company documents (PDF) | Financial statements read from annual reports and results filings (Phase 5 document intelligence) | 2 | ICICI Bank results + annual report from icicibank.com; Reliance's exchange integrated filing (financial) from its official static host `rilstaticasset.akamaized.net`, linked from ril.com. Subsidiary documents hosted on a parent's site are skipped. |

## What does NOT work, and why we did not force it

| Source | Result | Decision |
|---|---|---|
| NSE interactive APIs and `nsearchives.nseindia.com` | Connection dropped for non-browser clients | Not used. The `archives.nseindia.com` static host is used instead. |
| BSE `api.bseindia.com` | HTTP 403 | Not used. Static bhavcopy files are used instead. |
| Yahoo Finance | `robots.txt`: `Disallow: /` for all agents | Not used. Official bhavcopy replaces it. |
| Google News RSS search | `robots.txt` disallows | Not used. Publisher RSS feeds replace it. |
| Wikidata (SPARQL and API) | `robots.txt` disallows `/sparql` and `/w/` | Not used. NSE name-history replaces it. |
| Business Standard RSS, PIB RSS | HTTP 403 | Skipped. |
| IRDAI feeds | `robots.txt` disallows | Skipped. |
| `tcs.com` | HTTP 403 | IR discovery fails for this site; documents must come from another permitted route. |

> The multi-source router, Upstox evaluation and coverage engine are described in [DATA_ACQUISITION_STRATEGY.md](DATA_ACQUISITION_STRATEGY.md).

## Known gaps (open risks for later phases)

- **Shareholding patterns, corporate announcements and XBRL** are served by the exchanges' protected APIs, which we do not use. **Financial statements** are now read from company documents (Phase 5); coverage varies by company with what each site exposes (TCS and Infosys return 403, HDFC Bank exposed no document links). Where data cannot be obtained, reports say so.
- **Corporate actions** (splits, bonuses, dividends) are not yet acquired. Stored prices are **unadjusted**; anything comparing prices across such events must adjust first (Phase 7).
- BSE price history is patchy because BSE removes older files. NSE history is deep.
- Exchange data is for our own research use only and must not be redistributed.

## Setup and commands

Run from `./backend` with the database up and migrated.

```bash
python -m app.acquisition.cli probe                    # what each source returns right now
python -m app.acquisition.cli nse-equity-list          # refresh NSE main + SME lists (add --dry-run to preview)
python -m app.acquisition.cli name-history             # preview former names/symbols; add --apply to save
python -m app.acquisition.cli prices --exchange NSE --from 2026-11-03 --to 2026-11-06
python -m app.acquisition.cli prices --exchange BSE --from 2026-11-03 --to 2026-11-06
```

### BSE company list (manual)

Save the BSE equity list as `data/master/bse_Equity.csv` (git-ignored) and import:

```bash
python -m app.master_data.cli --bse ../data/master/bse_Equity.csv --dry-run   # preview
python -m app.master_data.cli --bse ../data/master/bse_Equity.csv             # import
```

The importer merges NSE and BSE listings by ISIN issuer, keeps only company securities (ISIN prefix INE/IN9; funds and ETFs are skipped), never deletes or overwrites names, records every file (SHA-256, row counts) in `master_data_imports`, and reports skipped rows with examples.
