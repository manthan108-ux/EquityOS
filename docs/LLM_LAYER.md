# LLM layer (Phase 9)

The language models **read and interpret; they never supply a number**. Everything they return is
checked by code, and everything that reaches the evidence store still passes the deterministic
arithmetic, issuer and cross-period checks of Phases 5-6.

## Providers (`app/llm/`)

| File | Role |
|---|---|
| `base.py` | Request/response types, error classes, `scrub()` (removes API keys from any message), strict JSON parsing |
| `providers.py` | Groq (OpenAI-compatible) and Gemini adapters over `httpx`; `LlmRouter` (retry, bounded backoff, fallback); `default_router()` |
| `headers.py` | Column-heading interpretation (period of each column) |
| `tables.py` | Reading income-statement tables whose layout the reader cannot parse (mirrored tables) |
| `cli.py` | `python -m app.llm.cli check` tests each provider alone |

* Keys come from `.env` (`GROQ_API_KEY`, `GEMINI_API_KEY`) and travel in HTTP **headers**, never in URLs.
  Error text passes through `scrub()`; a test proves a key cannot appear in an exception.
* Model names are settings (`GROQ_MODEL`, `GEMINI_MODEL`), not code. Defaults: `openai/gpt-oss-120b`
  and `gemini-3.5-flash` (Gemini `2.5-flash` is closed to new users; `3.8-flash` answered "high demand" during
  testing). `LLM_PRIMARY` chooses the first provider; the other is the fallback.
* Rate limits (429) and transient errors (timeouts, 5xx) are retried with a bounded wait; authentication
  errors and malformed answers are not retried on the same provider but do fall through to the other one.
  A cut-off answer (output limit), an empty answer, or non-JSON is refused.
* Run with `docintel extract --symbol X --all --llm`. Without `--llm` nothing changes and no call is made.

## What the model is asked to do, and how each answer is verified

**Headings (`headers.py`).** For columns the deterministic reader could not assign (no date, no duration,
two columns with one period) the model is shown the heading words and the top of the page with **all printed
amounts removed**. It returns the period and a *quote*. Code accepts it only if: the quote is literally in
the text; the stated year and month occur in the quote; the duration is 3, 6, 9 or 12 and fits the
April-March fiscal year. Refusals are recorded on the column.

**Tables (`tables.py`).** Only for quarterly-result documents, on pages that look like an income statement
but yielded fewer than 8 rows. **Code**, not the model, finds the amount columns (numbers that line up) and
reads every value from the PDF's own words, on either side of the label. The model says which period each
amount column covers (quote required; for headings stacked in separate cells every quoted word must be on
the page and the date must be on the page) and which numbered line each label sits on (the label must be the
line's own text). Basis and unit come from the page text. The block then goes through the normal checks.
Before that, `withhold_inconsistent_periods` rejects every column whose quarters do not add up to the
half-year printed beside them.

## What was found on real filings

* The first table attempt let the model place values; it mixed the left and right amount groups. The
  arithmetic checks rejected it. Column assignment was then taken away from the model (geometry).
* The model gave the heading "Revenue from operations" the line of "Sale of products". A stricter
  label-to-line rule and the identity checks caught it.
* For HUL standalone, a column labelled "half year" actually held the June quarter. The evidence engine's
  quarters-vs-half-year check flagged it as a warning only, so the same check now runs **inside** the
  table reader and withholds the columns.
* Deterministic fixes were preferred where the cause was understood: a serial-number column that borrowed
  its neighbour's heading (ITC), fused dates ("30June 2026", HCL), garbled rupee units ("(Uncrores)").

## Limits

* The model's choice of period for a column cannot be proven by one column's own arithmetic. It is
  protected by the quote rule and by the cross-column identities; a document with only one period cannot be
  cross-checked and its LLM-labelled columns rely on the quote alone.
* LLM output is not perfectly repeatable between runs (HUL promoted 12 to 109 values across runs while the
  prompts were being improved). Each run records provider, model and the quote on the column.
* Free tiers have rate limits and daily quotas; a batch over many pages may be slow (a 35-page HUL document took
  up to six minutes).
