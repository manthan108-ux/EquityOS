# Evidence Store and Validation Engine (Phase 6)

Every number or statement that can reach a report passes through this layer. It answers four
questions for each one: *what is it, where did it come from, can it be trusted, and what was it
built from?*

Code: `backend/app/evidence/`. Tables: migrations 0007 and 0008. Read-only API: `/api/.../evidence`,
`/resolved`, `/validation`. Commands: `python -m app.evidence.cli` (from `./backend`).

## 1. The four classes

| Class | What it is | How it gets in | Rules enforced |
|---|---|---|---|
| FACT | A value (or, for narrative evidence, a statement) read from a source | Mirrored automatically from the Phase 4/5 tables; narrative via `register_text_fact` | Provider and source URL required (database constraint). Document figures need document and page |
| CALCULATION | Arithmetic on other evidence | `register_calculation` only | The engine computes the value; caller supplies a formula and input ids. Units tracked; one basis; one period type |
| ASSUMPTION | An explicit input | `register_assumption` | Must state what is assumed, why (rationale of 15+ characters) and who made it |
| INTERPRETATION | A reasoned statement | `register_interpretation` | Must cite evidence; **every number in the text must be found in the cited evidence** |

Evidence is never edited. If a source value changes, the old record becomes `SUPERSEDED`, a new one
is created, and anything built on the old one is reported (section 6).

## 2. Where facts come from (sync)

`sync_company` mirrors, idempotently, with a natural key per source row (not the row id, which changes
whenever a document is re-processed):

| Source table | Becomes |
|---|---|
| `financial_facts` | FACT per statement line item (derived quarters become CALCULATION, linked to their two cumulative inputs) |
| `extracted_facts` (promoted disclosures) | FACT per segment/ratio/schedule/shareholding value, with its dimension. A shareholding category appears once in shares and once in percent, so the unit is part of its metric name |
| `shareholding_facts` | FACT in percent |
| `document_sections` | Text FACT (heading, pages, auditor flags); the full text stays in `document_sections` |

Same key and content: nothing. Changed content: old `SUPERSEDED`, new created. Key gone: `WITHDRAWN`.
An earlier version coming back is reactivated, not duplicated. Rows whose unit cannot be converted
are reported (`SOURCE_ROWS_NOT_USED`), never turned into evidence. Prices are not bulk-mirrored (700k
rows); `price_evidence()` creates a FACT on demand and marks it **unadjusted**.

Each record keeps quality inputs in `details`: printed decimals, OCR confidence, and how the same
cell reads in other documents (agree / differ), taken from Phase 5's extraction.

## 3. Source authority and conflicts

Authority tiers come from the `sources` table (1 exchange, 2 company filings, 3 regulators,
4 government, 5 secondary such as Upstox, 6 user-supplied). For each (metric, dimension, period,
period type, basis):

| Situation | Status | Value chosen |
|---|---|---|
| One source | `SINGLE_SOURCE` | it |
| Several sources within 1% or within printed rounding | `AGREED` | strongest, then newest |
| Sources differ; one has strictly stronger authority | `RESOLVED_BY_AUTHORITY` | the strongest (disagreement kept in `alternatives`, WARNING) |
| Sources of **equal** authority differ beyond 1% | `CONFLICT` | **none** (ERROR) |
| Only a calculation exists | `CALCULATED` | it |
| Every candidate failed a provenance check | `UNUSABLE` | none (ERROR) |

A reported figure beats a calculated one. A document linked to another company, or whose issuer check
failed (`MISMATCH`), is excluded and reported. "Within rounding" means the difference does not exceed
half of the last printed digit of both numbers (a figure printed as 1,234 crore may be 1,233.5 to
1,234.5), so two correct filings are not called conflicting.

## 4. Quality (HIGH / MEDIUM / LOW, with reasons, never a probability)

* Primary source, arithmetic checks passed: **MEDIUM**.
* HIGH only if corroborated: another independent source agrees, or the same figure appears in
  another document.
* Capped at MEDIUM: scanned (OCR) pages, calculations, a disagreeing weaker source, or other
  documents printing a different figure (restatement?).
* LOW: secondary sources (tier 5+), user-supplied data, unit not stated, conflict.

## 5. Validation rules (version 6.0)

Run on the *resolved* values of one company. ERROR means the values involved are **blocked**: later
phases cannot use them.

| Rule | Severity | Checks |
|---|---|---|
| `CROSS_SOURCE_CONFLICT`, `NO_USABLE_SOURCE`, `PROVENANCE` | ERROR | conflicts, unusable candidates, document figure without document/page, wrong-company document |
| `SOURCE_DISAGREEMENT` | WARNING | a weaker source disagrees with the chosen one |
| `BALANCE_SHEET_IDENTITY` | ERROR | total assets = total equity and liabilities |
| `INCOME_TOTAL_IDENTITY` | ERROR | total income = revenue (or interest earned) + other income |
| `CASH_FLOW_IDENTITY`, `CASH_ROLL_FORWARD` | ERROR (INFO if the exchange-rate line is absent) | net change = operating + investing + financing (with or without the exchange-rate line); closing = opening + net change |
| `QUARTERS_VS_CUMULATIVE` | WARNING | reported quarters add up to the reported half-year, nine-month and annual figures |
| `NEGATIVE_VALUE`, `EPS_SIGN` | ERROR for balance-sheet totals, else WARNING | impossible signs |
| `LARGE_YEAR_CHANGE` | WARNING | annual total income or assets changing more than 5x (units, mergers, restatements) |
| `STALE_DATA` | WARNING | newest annual statement older than 550 days, quarterly older than 200 |
| `DOCUMENT_DISCREPANCY`, `RESTATED`, `UNIT_UNSTATED` | WARNING / INFO / WARNING | other documents print a different figure; a restatement; no unit in the source |
| `NEEDS_REVIEW_PENDING`, `SOURCE_ROWS_NOT_USED` | INFO | scanned values withheld for a person; rows not turned into evidence |
| `CALC_*`, `CLAIM_*` | ERROR | see section 6 |

Identities use the rounding allowance of the printed figures, not a percentage. The run records, for
every rule, how many checks were **performed and passed**, so "no findings" can be told apart from
"nothing to check".

## 6. Lineage and staleness

`evidence_links` connect a calculation to its inputs (with the name used in the formula), an
interpretation to what it cites, an assumption to its support. On every validation run each active
calculation is **recomputed** from its current inputs and compared with its stored value, and each
interpretation's numbers are re-checked. Reported as ERROR (and the calculation or claim is
blocked): `CALC_INPUT_NOT_CURRENT` (an input was superseded or withdrawn), `CALC_RECOMPUTE_MISMATCH`
(a stored value no longer follows from its inputs), `CALC_MIXED_BASIS`, `CALC_MIXED_PERIOD`,
`CALC_NOT_REPRODUCIBLE`, `CALC_NO_INPUTS`, `CLAIM_NO_EVIDENCE`, `CLAIM_CITES_STALE`,
`CLAIM_UNTRACED_NUMBER`, `CLAIM_CITES_BLOCKED`. Blocking propagates to everything built on blocked
evidence. Blocking is narrow: a broken calculation blocks itself, not the sound facts it used;
an identity failure blocks all the figures it involves because it cannot say which side is wrong.

## 7. The ledger (how later phases use this)

```python
from app.evidence.ledger import (resolved_value, register_calculation, register_assumption,
                                 register_interpretation, register_text_fact, price_evidence)

profit = resolved_value(session, company_id, "INCOME.net_profit", date(2026, 3, 31),
                        period_type="ANNUAL", basis="CONSOLIDATED")   # raises NoUsableValue with the reason
roe = register_calculation(session, company_id, "RATIO.roe", "profit / equity * 100",
                           {"profit": profit.evidence.id, "equity": equity.evidence.id},
                           created_by="calc-engine")
register_interpretation(session, company_id, "THESIS.returns",
                        "Return on equity was 15.0%.", [roe.id], created_by="agent")
```

* `resolved_value` returns the one usable value, or raises `NoUsableValue` saying why (missing,
  conflict, blocked, stale).
* `register_calculation` refuses: inputs that are unvalidated, blocked, or not the resolved value of
  their key (a losing source); consolidated mixed with standalone; quarterly with annual; different
  period ends unless `spans_periods=True`; unit errors (rupees x rupees, rupees + per-share);
  division by zero; names that do not match the formula. A recalculation of the same quantity
  supersedes the earlier one.
* Formula language (`expression.py`): numbers, input names, `+ - * / ^`, parentheses; exact Decimal
  arithmetic; no `eval`. Units: `inr`, `inr_per_share`, `shares`, `ratio`, `percent`; `ratio * 100` is
  `percent`; a ratio may be shown as `x` or `days` when declared.
* `register_interpretation` figure matching: "₹23,450 crore" matches 234,500,000,000 rupees;
  "18.2%" matches 18.2 percent or a 0.182 ratio; matching allows exactly the rounding the writer used;
  years, FY/quarter tokens, dates, ordinals, list numbers and "page 12"/"Note 5" are not figures.

## 8. Commands

```
python -m app.evidence.cli validate --symbol ICICIBANK     # sync + resolve + validate + record
python -m app.evidence.cli validate-all                    # every company with stored data
python -m app.evidence.cli summary  --symbol ICICIBANK
python -m app.evidence.cli findings --symbol ICICIBANK --severity ERROR --details
python -m app.evidence.cli resolved --symbol ICICIBANK --metric INCOME.net_profit
python -m app.evidence.cli show --id 123                   # source, page, lineage tree, dependents
python -m app.evidence.cli check-claim "Profit rose 12%" --company ICICIBANK --cite 1,2
```

API (read-only): `GET /api/companies/{id}/evidence`, `/evidence/summary`, `/resolved`, `/validation`;
`GET /api/evidence/{id}` (record, lineage, dependents).

## 9. Real-document results (2026-11-08)

| Company | Values checked | Result |
|---|---|---|
| ICICI Bank | 1,605 (FY2025-26 and Q/H1 periods, both bases) + 6 narrative sections | 0 errors, 0 warnings. 59 values HIGH (same figure in two documents), 1,546 MEDIUM. Checks performed: balance-sheet identity 10/10, income total 14/14, cash-flow identity 8/8, cash roll-forward 8/8, quarters vs cumulative 36/36, year change 4/4 |
| Reliance Industries | 220 | 0 errors, 0 warnings. Balance sheet 2/2, income 8/8, quarters vs cumulative 15/15 (all MEDIUM: scanned filing) |
| TCS, Infosys, HDFC Bank | 0 | no stored documents (Phase 4/5 limits), nothing to validate |

Two defects were found only by running on real data and fixed: shareholding categories printed in
shares and percent were reported as a conflict (the unit is now part of the metric), and the
cash-flow identity ignored that statements may show the exchange-rate line separately (both forms are
now accepted, as in Phase 5). A third was found by tests: the evidence hash depended on how a decimal
was printed (`100` vs `100.0000`), so a value re-read from the database looked changed.

Dry run of the ledger on real ICICI evidence (rolled back, nothing kept): FY2026 consolidated net
profit ₹54,208 crore (page 11 of the Q2 FY2027 filing) and shareholders' funds ₹3,60,378 crore gave a
return on closing equity of 15.04%; profit growth 6.23%; a claim "Net profit grew 40%" was refused; a
calculation mixing FY2025 and FY2026 without `spans_periods` was refused.
