# Architecture

EquityOS is a modular monolith: one FastAPI backend, one Next.js frontend, one PostgreSQL database. The project ends at PDF report generation; there is no post-report monitoring.

```
Next.js UI ──► FastAPI API ──► Orchestrator ──► research agents (parallel where independent)
                                  │                 │ request missing data
                                  │                 ▼
                                  │       Acquisition layer ─► Providers (NSE, BSE, IR sites, regulators, market data)
                                  │                 ▼
                                  │       Document intelligence ─► Evidence DB (provenance on every value)
                                  ▼                 ▼
                      Financial engine (deterministic) · Sector weights · Peers · Forecast/Valuation/Risk
                                  ▼
                      Red-Team ─► Investment Committee ─► PDF report
```

## Non-negotiable principles

1. **Deterministic where it matters.** Financial arithmetic, company resolution and sector weights are plain, tested Python. LLMs interpret evidence and return typed (Pydantic) output only.
2. **Central evidence.** Agents never fetch the web themselves; they request data through the acquisition layer so every fact is stored with provenance (source, document, page, period, unit, method, confidence).
3. **No fabrication.** Missing data is reported as missing, never as zero or an invented value.
4. **Visible failure.** A failed module marks the report sections that depend on it; its output is never invented.
5. **Standalone vs consolidated and reporting periods are never mixed** without an explicit methodology.

## Current layout (through Phase 2)

```
backend/app/
  main.py            FastAPI app factory, CORS
  api/health.py      /health (liveness), /health/ready (DB check)
  core/config.py     Settings from environment variables (pydantic-settings)
  core/database.py   SQLAlchemy engine + session dependency
  core/identifiers.py ISIN validation (ISO 6166)
  core/text.py       Name normalisation shared by aliases and the resolver
  models/            SQLAlchemy models (company master data)
backend/migrations/  Alembic migrations (schema changes only happen here)
backend/app/seeds/   Idempotent reference + test-universe seed data
frontend/src/
  app/               Next.js routes
  features/health/   System status panel
  services/api.ts    Single place for backend calls
```

Folders for later phases (providers, evidence, agents, ...) are created when a phase needs them, not as empty placeholders.

## Decision log

| # | Decision | Reason |
|---|---|---|
| 1 | PostgreSQL via Docker Compose, host port **5433** | Reproducible; avoids clashing with a locally installed Postgres on 5432. |
| 2 | Health endpoints split into liveness and readiness | Liveness must not fail when the DB is down; readiness reports 503 and exposes only the exception class name, never connection details. |
| 3 | Frontend status panel is a client component | The browser calls the API directly, which also proves CORS. Next 16 `cacheComponents` requires uncached server fetches to be wrapped in Suspense. |
| 4 | Backend dependencies pinned in `requirements*.txt` | Reproducible builds and CI. |
| 5 | LLM layer: provider-independent interface, Groq primary, Gemini fallback, **free tiers only**, no automatic switch to paid models; pause and notify when limits are exhausted; track tokens per report. Introduced in Phase 9. Phases 1-8 do not use an LLM. | Budget constraint of INR 0 for inference. |
| 6 | PDF engine: HTML/CSS templates (Jinja2) rendered to PDF with headless Chromium (Playwright); Plotly charts embedded as images/SVG. Final confirmation in Phase 11. | Best typographic and layout control (running headers/footers, page numbers, tables) with maintainable templates; WeasyPrint needs native GTK libraries on Windows and ReportLab is laborious for table-heavy layouts. |
| 7 | Companies and securities are separate tables; canonical company ID is a UUID | One company, many listings (NSE/BSE); no duplicate companies; IDs survive ticker changes. |
| 8 | DB tests use a real throwaway PostgreSQL database built from the real migrations | The schema relies on Postgres features (regex CHECKs, UUID defaults); testing migrations is testing the schema. |
| 9 | Synchronous httpx client behind one PoliteClient | Simple and testable with a mock transport and fake clock; per-host concurrency is deliberately limited anyway. |
| 10 | Robots.txt obeyed strictly (RFC 9309); 401/403/418 are final; honest User-Agent | Project rule. A source that forbids automated access is dropped, not worked around (Yahoo, Google News RSS and Wikidata were dropped for this reason). |
| 11 | Content validation on every fetch | Some sites answer HTTP 200 with an error page when throttled; without validation that would be stored as data. |
| 12 | Official static exchange files are the primary market-data source | Public, authoritative (tier 1) and fetchable without defeating protections. Prices are stored unadjusted with file hash and URL. |
| 13 | Providers return proposals; writes are explicit | Discovery (IR links, name history) never silently overwrites verified data. Name history only adds aliases, each with a recorded source. |
| 14 | Provider router + registry; declared vs verified support; every attempt returned | No single source is assumed to supply everything; a fallback is never silent; support is claimed only after a recorded live retrieval. |
| 15 | Authenticated documented APIs are called with `documented_api=True` (robots.txt skipped), credentials only in headers or redacted query parameters | robots.txt governs crawlers, not contractual APIs; access denials stay final; tokens never reach logs, cache keys, stored URLs or payload archives. |
| 16 | Coverage engine counts only data that exists; confidence is a category with reasons | Completeness must not be inflated; a quality category must not be read as a probability. |
| 17 | Manual routes (CSV, documents) are first-class, all-or-nothing and recorded as USER_SUPPLIED | Coverage can be completed legitimately where no API reaches, without pretending the data is provider-verified. |
