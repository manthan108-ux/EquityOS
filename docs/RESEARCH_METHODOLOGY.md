# Research Methodology

This document records the rules every number in an EquityOS report must follow. It describes
what is implemented; parts that later phases add are marked *planned*.

## 1. Evidence classes

Every claim in a report is one of:

| Class | Meaning | Produced by |
|---|---|---|
| FACT | A value or statement read from a source, with its provenance | acquisition (Phase 4), document intelligence (Phase 5) |
| CALCULATION | Deterministic arithmetic on FACTs (ratios, growth, valuation maths) | financial engine (Phase 7, *planned*) |
| ASSUMPTION | An explicit input chosen for a forecast or valuation, stated as such | forecasting (Phase 10, *planned*) |
| INTERPRETATION | A reasoned reading of FACTs and CALCULATIONs | research agents (Phase 9, *planned*) |

An INTERPRETATION may never introduce a number that is not a FACT, CALCULATION or ASSUMPTION.

## 2. Source authority

| Tier | Source type | Examples in EquityOS |
|---|---|---|
| 1 | Exchange files | NSE/BSE security lists, official end-of-day bhavcopy |
| 2 | Company disclosures | Annual reports, results PDFs, presentations (document intelligence) |
| 3 | Regulators | RBI, SEBI publications |
| 4 | Government | data.gov.in statistics |
| 5 | Reputable secondary | Upstox Fundamentals (aggregated), publisher news feeds |
| 6 | User-supplied and other sources | manual CSV imports and uploads (reported as USER_SUPPLIED, never as verified) |

For financial statements the router prefers company documents (tier 2) over aggregators. A
fallback is always labelled; sources are never blended silently.

## 3. Rules for financial data

* **Missing is not zero.** A blank, "-", "Nil" or "NA" cell is recorded as absent.
* **Bases are never mixed.** Consolidated and standalone figures are separate rows; a calculation
  uses one basis throughout.
* **Periods are never mixed.** Quarterly (3-month) and annual (12-month) figures are separate;
  half-year and nine-month figures are kept but not used as quarters or years.
* **Units are explicit.** Every fact keeps its original value and unit next to the value in
  rupees; per-share values are never scaled.
* **Restatements.** When a later filing restates an earlier period, the later value is used and the
  replacement is noted; earlier values stay archived.
* **Conflicts.** Two sources that disagree by more than 1% make the dataset CONFLICT (coverage
  engine); two tables of the same document that disagree are both withheld.

## 4. Values read from documents (Phase 5)

A value read from a PDF becomes a FACT only after:

1. its unit, consolidated/standalone basis and reporting period are stated on the page;
2. the arithmetic identities of its statement hold in its column (for example
   total income = revenue + other income, total assets = total equity and liabilities);
3. it is a quarter or a full year.

Each such FACT carries the document, the page and the printed label, so the report can cite it.
See [DOCUMENT_INTELLIGENCE.md](DOCUMENT_INTELLIGENCE.md).

## 5. Coverage and confidence

Before analysis, the Coverage Engine reports for each dataset the required and available periods,
the sources, freshness and verification status. Confidence is a category (HIGH / MEDIUM / LOW /
NONE) with written reasons, never a probability. Research proceeds with partial data, but the
report sections that depend on missing datasets are marked as weaker.

## 6. The evidence store (Phase 6)

The four evidence classes of section 1 are now enforced in code and in the database, not by
convention. See [EVIDENCE_ENGINE.md](EVIDENCE_ENGINE.md).

* Every FACT carries provider, source URL and (for documents) document and page.
* Source authority (section 2) is applied by one resolver: a stronger source beats a weaker one;
  equal authority that disagrees by more than 1% is a CONFLICT and no value is used.
* A CALCULATION is computed by the engine from stored evidence; consolidated and standalone, or
  quarterly and annual, inputs are refused; units are checked.
* An ASSUMPTION states what it is, why, and who made it.
* An INTERPRETATION cites evidence and may contain only numbers found in it.
* Validation errors block the figures involved, and everything built on them.

## 7. Calculations (Phase 7)

See [CALCULATION_ENGINE.md](CALCULATION_ENGINE.md). Rules in force:

* Ratios use one basis and one period type; consolidated and standalone are calculated separately.
* A ratio is annual (returns) or period-matched; a quarter is never annualised silently.
* Growth is refused when the base period is zero or a loss.
* Trailing twelve months = four reported quarters, or last year + year-to-date - prior year-to-date;
  the two methods must agree.
* Valuation multiples state the statement date and the price date they combine; a split or bonus
  between them is applied through a visible adjustment factor.
* Prices are adjusted for splits, bonuses and consolidations only. Technical indicators use the
  adjusted series and say when history is too short.
* Averages use opening and closing balances (the opening balance must exist).

## 8. Sector and peer comparison (Phase 8)

See [SECTOR_ENGINE.md](SECTOR_ENGINE.md). Peers share the statement profile and sector; comparisons use
one basis and the same period for company and peers; sector weights are curated, versioned judgement
with stated reasons; scores are relative standing (not quality, not a forecast); thin peer groups are
marked and graded LOW; no overall score is published unless 60% of the sector's weight has data.

## 9. Planned

Research agents (Phase 9), forecasting, valuation and risk (Phase 10), Investment
Committee review and the PDF report (Phase 11).
