# Next steps and the documents EquityOS needs (2026-11-10)

How to supply a document: save the PDF (text-based, not a scan) in `data\inbox\<SYMBOL>\` (for
example `data\inbox\SBIN\`), then press **Resume research** for that company on the dashboard (or
start research). The DOCUMENTS stage picks the file up, checks who issued it, reads it and re-runs
only what changed. The home page's **readiness overview** (`GET /api/research/overview`) always shows
the live list of what is still missing per company.

## Where things stand
* Automatic research works today for **TCS** and **Axis Bank**; every other sample company is waiting
  for the documents below (Kotak currently holds only standalone figures).
* The three banks' Q2 FY27 releases are read (NIM, NPA, CAR, CASA, deposits, gross advances).
* SBI's and HDFC's results *filings* are scans with unreliable digits; they need a text version.

## Documents needed, in priority order
1. **SBI, HDFC Bank** - the Q2 FY27 audited/unaudited results as a *text-based* PDF (or XBRL), standalone
   and consolidated; the latest shareholding pattern (for the share count, which gives market cap and
   P/B). Settles: interest earned/expended, NII, net profit (HDFC consolidated), P/B, market cap.
2. **Kotak Mahindra Bank** - Q2 FY27 results filing + press release/presentation (its data is old and
   standalone-only), and **ICICI Bank** - the Q2 FY27 presentation page with NIM and CASA.
3. **Axis Bank** - only CASA is missing (important, not critical).
4. **IT services** (Infosys, Wipro, Tech Mahindra, HCLTech) - the quarterly and annual results with the
   depreciation-and-amortisation and finance-cost lines (settles operating margin, the one critical
   gap), and HCLTech's shareholding pattern. For attrition/utilisation: the quarterly fact sheet.
5. **FMCG** (ITC, HUL, Nestle, Britannia, Godrej Consumer) - quarterly and annual results with
   depreciation, finance costs, borrowings; HUL on a consolidated basis if it publishes one.
6. **Automobiles** (Maruti, M&M, TVS, Hyundai, Tata Motors PV) - the same, plus the monthly sales
   release (vehicle volumes).
7. **Energy and conglomerate** (ONGC, IOC, BPCL, Reliance) - annual report or results with borrowings,
   capex (cash-flow statement) and the segment note.
8. **Operating-reader samples** - one real monthly sales release each for Maruti, M&M and TVS, and one
   quarterly fact sheet each for TCS and Infosys, so the readers can be checked on real layouts.

## Decisions only you can make
* **Exchange XBRL.** A separate desk study recommends downloading exchange-filed XBRL by hand and
  reading it offline (it would fix the scanned-filing problem). Before building it, NSE's terms on
  automated retrieval should be confirmed in writing; say whether to proceed.
* **PyMuPDF licence (AGPL)** - acceptable while the repository is private; needs a decision before any
  public release (ReportLab, used for PDFs, is permissively licensed).
* **Should a share count carry an accounting basis?** Today a consolidated market cap and a standalone
  equity cannot be combined into P/B (the ledger refuses). Options: treat share counts as
  basis-neutral, or keep refusing.
* **Corporate actions for HDFC Bank (2025-08-26) and Reliance (2024-10-28).** The price series shows a
  halving on those dates; a confirming corporate-action record (from an exchange announcement you
  provide) would let the prices be adjusted.
* **GitHub Actions** - please confirm the latest run is green (it cannot be seen from here).

## Engineering that follows once documents arrive
1. Re-process each company's session; fix whatever the new layouts miss.
2. Let the absolute scoring methodology read the bank disclosure ratios (NPA, CAR, NIM).
3. Offline XBRL reader (after the decision above).
4. A browser end-to-end test of the research dashboard.
