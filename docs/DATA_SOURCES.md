# Data Sources

Principles: official sources first; respect each site's terms, robots rules and rate limits; never bypass authentication, paywalls or bot protection; cache downloads; report any data we could not obtain.

## Company universe (all NSE and BSE listed companies)

EquityOS supports **any** NSE/BSE listed company. The universe is loaded from the exchanges' own public security lists. The five-company seed (Reliance, TCS, Infosys, HDFC Bank, ICICI Bank) is only a test fixture.

| File | Where to get it | Contains |
|---|---|---|
| NSE `EQUITY_L.csv` | nseindia.com > Market Data > "Securities available for trading" (equity list CSV) | symbol, company name, series, listing date, ISIN, face value |
| BSE list of scrips (Equity, CSV) | bseindia.com > Market Data > Equity > "List of Scrips" (download) | security code, scrip id, issuer name, status, group, ISIN, industry |

**Why a manual download?** On 2026-11-07 neither exchange could be fetched programmatically from this project: NSE's archive host drops non-browser connections and BSE's API answers HTTP 403. We do not spoof a browser or otherwise work around this. Downloading the file in your browser is ordinary use of the exchange's public website; importing the file is a local operation. If an exchange later offers a permitted automated feed, a provider can replace this step (Phase 4 provider interface).

### Where to save the files

Save them in `data/master/` at the repository root (git-ignored, so the exchange data is never committed):

```
EquityOS/data/master/nse_EQUITY_L.csv
EquityOS/data/master/bse_Equity.csv
```

### Import

Always preview first: `--dry-run` runs everything, prints the report and rolls back.

```bash
# from ./backend (database running; schema migrated; `python -m app.seeds.loader` run once for exchanges/sectors)
python -m app.master_data.cli --nse ../data/master/nse_EQUITY_L.csv --bse ../data/master/bse_Equity.csv --dry-run   # preview
python -m app.master_data.cli --nse ../data/master/nse_EQUITY_L.csv --bse ../data/master/bse_Equity.csv             # import
```

Either flag may be used alone. The importer:

- merges NSE and BSE rows of the same company using the **ISIN issuer key** (ISIN characters 1-7; `IN9...` ISINs are mapped onto the same issuer's `INE...` key, an assumption documented in `app/core/identifiers.py`);
- is idempotent, never deletes (securities missing from a newer file are only reported), never overwrites an existing company name or verified field;
- keeps only company securities (ISIN prefix INE/IN9); funds/ETFs (INF) and non-equity instruments are skipped, and rights-entitlement rows never name a company;
- validates every ISIN (ISO 6166 check digit) and BSE code, and skips and counts bad rows with reasons;
- fails loudly, changing nothing, if required columns are missing (layout change) instead of guessing;
- records each file (name, SHA-256, row counts, skip reasons) in `master_data_imports`, and links each security to its last import.

Sector and industry are **not** in the NSE file and BSE's industry label is stored raw (`companies.exchange_industry`). Assigning the research sector is Phase 8.

## Other sources (Phase 4 onward)

Company IR websites, annual reports, quarterly results, exchange filings, XBRL, SEBI/RBI/IRDAI/TRAI/CERC/PNGRB/PPAC/CDSCO publications, MoSPI, Economic Survey, public market-data providers, reputable news. Each will be a replaceable provider adapter with caching, retries, rate limiting and source attribution.
