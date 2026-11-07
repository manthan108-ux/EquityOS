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

## Current layout (Phase 1)

```
backend/app/
  main.py            FastAPI app factory, CORS
  api/health.py      /health (liveness), /health/ready (DB check)
  core/config.py     Settings from environment variables (pydantic-settings)
  core/database.py   SQLAlchemy engine + session dependency
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
| 7 | Alembic and SQLAlchemy models arrive in Phase 2 | Phase 1 only proves DB connectivity. |
