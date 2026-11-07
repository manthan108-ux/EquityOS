# Document Intelligence (Phase 5)

EquityOS reads financial statements directly from the official PDFs companies publish (annual
reports, quarterly results, investor presentations). This is the route that works when no API
provider is available, and it is the most authoritative one: the numbers come from the company's
own filing, with the page they were printed on.

Everything here is deterministic Python. No LLM reads, interprets or "fixes" a number.

## Pipeline

```
stored PDF (data/documents/<sha256>.pdf, content-addressed, magic-byte checked)
   │  app/docintel/pdf.py         PyMuPDF: text + word boxes per page; scans/garbled fonts flagged
   ▼                               (OCR only if Tesseract is installed)
words ──► lines ──► value columns ──► table rows          app/docintel/layout.py
   │
   ├── statement titles split the page into blocks       app/docintel/detect.py
   ├── per block: unit (₹ crore / lakh / million …), consolidated vs standalone basis
   ├── per column: header text ──► period (end date, 3/6/9/12 months or "as at")   periods.py
   └── per row: printed label ──► canonical line item (Ind AS + bank formats)      taxonomy.py
   ▼
arithmetic checks per column (total income = revenue + other income, PBT − tax = PAT,
total assets = total equity and liabilities, closing cash = opening + net change, …)  validate.py
   ▼
extracted_facts  (EVERY value read, with page, label, header, raw text, position, check results)
   ▼  promotion rules (all must hold)
financial_facts  (provider DOCUMENT_EXTRACTION, document id and page on every row)
```

## What is extracted

| Statement | Formats | Examples of canonical items |
|---|---|---|
| Income statement / financial results | Ind AS Schedule III; bank Third Schedule | revenue_from_operations, interest_earned, other_income, total_income, total_expenses, operating_profit_before_provisions, provisions_and_contingencies, profit_before_tax, tax_expense, net_profit, net_profit_owners, total_comprehensive_income, eps_basic, eps_diluted |
| Balance sheet / statement of assets and liabilities | Ind AS; bank | total_assets, total_equity, equity_share_capital, other_equity, non_current_borrowings, current_borrowings, deposits, advances, investments, total_equity_and_liabilities, total_capital_and_liabilities |
| Cash flow statement | Ind AS; bank | net_cash_from_operating/investing/financing_activities, net_change_in_cash, cash_at_beginning, cash_at_end, capital_expenditure, dividends_paid |

Labels are normalised (enumeration such as "1." or "(a)", formula references such as "(I+II)",
"/(loss)" variants, currency markers) and must match a known pattern **in full**. Anything else is
left unmatched and counted, never forced onto the nearest item. Items that mean different things
in different places are resolved from context: "Borrowings" under "Non-current liabilities" is
`non_current_borrowings`; "Owners of the Company" under "Profit attributable to" is
`net_profit_owners`.

## Promotion rules

A value enters `financial_facts` only if **all** hold:

1. the document is linked to a company;
2. the cell holds a number (`-`, `–`, `Nil`, `NA` are *nil*, never zero, and are not promoted);
3. the column has a readable period: one month-end date and an explicit duration ("Quarter
   ended", "Year ended", "Q2 FY2027"), or "As at" for balance sheets. Two columns with the same
   period, a header that cannot be assigned to one column, two dates or a non-month-end date make
   the column unusable;
4. the basis (consolidated / standalone) is stated near the statement;
5. the unit is stated (per-share items such as EPS are per share regardless of the table unit);
6. no arithmetic check involving the value failed, and if no check covers the value, at least one
   check passed in its column and none failed (the column itself was read correctly);
7. the period is a quarter (3 months) or a year (12 months). Half-year and nine-month columns are
   kept in `extracted_facts` but are not stored as quarterly or annual figures;
8. the statement belongs to the company: its page header names the company, or the document's
   issuer line does (a subsidiary's document hosted on the parent's site is refused);
9. on scanned pages with an OCR text layer, the value itself passed a check.

Some checks are **confirm-only** (for example "total expenses = sum of the expense lines"): a pass
confirms the components, but a failure proves nothing (one unrecognised expense line breaks the
sum), so it never marks values as failed.

Not promoted values remain visible with their reason (`not_promoted_reason`), and every failed
check is listed with the numbers it used.

**Restatements and conflicts.** If two documents report the same line and period with different
values, the document whose own latest period is newer wins (later filings carry restated
comparatives) and the change is noted. Two tables in the *same* document that disagree are both
withdrawn. Values are never averaged.

**Tolerance.** Printed numbers are rounded, so a sum of *n* terms may differ by up to
(n + 1) / 2 units of the last printed decimal. A 0.05 % relative allowance covers statements that
round subtotals independently. Anything larger fails.

## Scanned filings (embedded OCR text)

Exchange filings are often signed scans carrying an invisible OCR text layer (Reliance's
integrated filing is one). The reader detects this from the page itself (a near full-page image
with text drawn invisibly over it, `text_method = embedded_ocr`). OCR can misread a digit, so on
such pages a value is promoted **only if an arithmetic check confirmed it** (PASSED); unconfirmed
values stay staged. Garbled dates ("Seo'25") or units ("fin crore") are refused, never guessed.

## Results on real documents (2026-11-07)

| Document | Pages | Statements found | Checks passed | Values read / promoted |
|---|---|---|---|---|
| ICICI Bank Q2 FY2027 results (born-digital) | 16 | 6 (standalone + consolidated income, balance sheet, cash flow) | 126 / 126 | 442 / 302 |
| ICICI Bank Annual Report FY2026 | 349 (read in ~5 s) | 6 (pages 145-148, 271-274, cash flows merged across 2 pages) | 36 / 36 | 146 / 81 |
| Reliance Industries Q2 FY2027 integrated filing (scanned, OCR layer) | 61 | 6 | strict mode | 334 / 72 |
| ICICI Prudential AMC presentation (a subsidiary, mislinked to ICICI Bank by IR discovery) | 37 | 3 | - | 82 / **0** (entity check) |

Cross-document check: the FY2026 figures appear in both ICICI Bank documents (in ₹ crore in the
results, ₹ thousand in the annual report). **65 of 67 agree exactly**; the 2 differences exposed
a real distinction (results print provisions *excluding* tax, the annual accounts' "Provisions
and contingencies" *include* tax), which is why those are now two separate items.

Reliance values were verified by hand against the page text (e.g. Q2 FY2027 consolidated revenue
332,861, total income 338,654, profit 29,550 = owners 25,548 + non-controlling 4,002).

### Lessons built into the rules

* Column dates split over two header lines; fiscal labels without "FY" ("Q2-2027") are read only
  when they agree with the column's own date (fiscal vs calendar reading).
* "30," is not the number 30; a header row of years under "Particulars" is not data.
* Upper-case labels wrap over three lines with serial numbers; joined readings are tried longest
  first, so "OPERATING PROFIT BEFORE / PROVISIONS AND / CONTINGENCIES" is never read as its tail.
* `..` means nil; ligatures (ﬁ, ﬂ) are normalised; "` in '000s" is a thousands unit.
* Statements continuing on the next page are merged (columns matched by period) and checked as one.
* Segment tables, prose lines and rows carrying amounts are never statement titles.
* A subsidiary's document hosted on the parent's website is detected from its issuer line.

## Safety

* PDFs are untrusted input: opened read-only from the content-addressed store (paths outside the
  data directory are refused), nothing in them is executed, page count (2,000) and words per page
  are capped, password-protected files are reported and never cracked.
* Document text is data, never instructions. Phase 9 agents will receive page text as quoted
  evidence with its document and page number.
* No LLM is involved in reading numbers.

## Commands

```bash
cd backend
python -m app.docintel.cli extract --symbol ICICIBANK          # process the company's new PDFs
python -m app.docintel.cli extract --symbol ICICIBANK --all    # re-run all of them
python -m app.docintel.cli show --document-id <uuid>           # statements, periods, checks, reasons
python -m app.docintel.cli facts --symbol ICICIBANK            # promoted values with page numbers
python -m app.docintel.cli ocr-status                          # is OCR available?
```

API (read-only): `GET /api/companies/{id}/documents`, `GET /api/documents/{id}/extraction`,
`GET /api/documents/{id}/pages/{n}`.

The router uses `DOCUMENT_EXTRACTION` first for income, balance-sheet and cash-flow datasets
(official filings outrank aggregators), then Upstox, then manual imports. Every attempt is still
reported.
