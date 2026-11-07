# Agent Design

Research agents are introduced in **Phase 9** together with the LLM layer. Phases 1 to 8 use no LLM.
This document fixes the constraints agents will be built under, and lists what earlier phases
already provide to them.

## Constraints (decided)

* **LLM providers:** Groq (primary) and Google Gemini (fallback), free tiers only, behind a
  provider-independent interface. No automatic switch to a paid model; when free limits are
  exhausted the job pauses and the user is notified. Tokens are tracked per report (budget ₹0).
* **Agents never fetch the web.** They request data through the acquisition layer and receive
  stored evidence with provenance.
* **Agents never do financial arithmetic** and never read numbers out of documents themselves.
  Numbers come from deterministic code (document intelligence, financial engine).
* **Typed output only.** Every agent returns a Pydantic model; free text is never parsed for
  numbers.
* **Retrieved text is untrusted data.** Page text from documents and news is passed as quoted
  evidence with its source; instructions inside it are ignored.

## Inputs already available to agents

| Input | Phase | Where |
|---|---|---|
| Resolved company identity (ISIN issuer, listings, aliases) | 3 | `companies`, `securities`, `company_aliases` |
| Official prices | 4 | `eod_prices` |
| Coverage report (what is missing and which pillars are weaker) | 4b | `GET /api/companies/{id}/coverage` |
| Validated statement values with document and page | 5 | `financial_facts` (provider `DOCUMENT_EXTRACTION`) |
| Page text of every processed document, for citation | 5 | `document_pages`, `GET /api/documents/{id}/pages/{n}` |

## Planned

The agent roster, orchestration and Investment Committee review are designed in Phase 9 and
Phase 11 and documented here then.
