# Frontend (Next.js 16, App Router, Tailwind 4)

Light theme only. No chart or UI library: the design system and the SVG charts are in-repo.

## Design system

Tokens live in `src/app/globals.css` (`@theme`). Colour carries meaning: green = validated/positive,
amber = pending/cautionary, red = blocked/failed, blue = primary action and selection, grey = secondary.

| Token | Value | Use |
|---|---|---|
| canvas / surface / surface-2 | `#F5F7FA` / `#FFFFFF` / `#F8FAFC` | page, cards, table headers |
| ink / muted | `#172033` / `#64748B` | text |
| brand / brand-2 | `#2457C5` / `#4778D5` | primary actions, selected tabs |
| line | `#E2E8F0` | borders |
| ok / warn / bad / info | `#16865C` / `#B87410` / `#D64545` / `#2878B8` | status |

Primitives (`src/components/ui.tsx`): `Button` (primary, secondary, tertiary, danger; loading; `disabledReason`
shown on hover and announced), `ButtonLink`, `Badge`/`StatusBadge`, `Card`, `PageHeader`, `Stat`, `Tabs`
(arrow-key navigation), `ProgressBar`, `InfoTip`, `Skeleton`, `EmptyState`, `ErrorState` (technical details
kept in an expander), `Notice`, `Field`. Charts (`src/components/charts.tsx`): `BarChart`, `LineChart`,
`HBarChart`, `Radar`. A missing value is a gap, never drawn as zero. Financial figures use tabular numerals
(`.tnum`) and `src/lib/format.ts` (crore with Indian grouping; `n/a` for missing).

## Routes

| Route | Purpose |
|---|---|
| `/` | Landing: search, recent research, activity, readiness summary, system status |
| `/companies` | Search and the directory of companies with evidence |
| `/companies/[id]` | One company's workspace, tabs: Overview, Financials, Peer comparison, Scorecard, Research readiness, Documents, Reports (`?tab=`, `?session=`) |
| `/research` | Sessions grouped by company, filtered by status, history per company |
| `/readiness` | Global readiness table: search, filters, sorting |
| `/documents` | Uploads per company with processing status |
| `/reports` | Every report version, PDF download |
| `/settings` | Access key, defaults, system information |

A company workspace never embeds the global readiness table. Any listed company can be opened; the 24-company
pilot is not hard-coded anywhere in the frontend.

## Data flow

`useResearch(companyId)` (`features/research/useResearch.ts`) owns the company's sessions, the open session's
progress, readiness, missing documents, uploads and reports, and the actions on them (start, resume,
continue, generate, upload, preferences). Financials, peers and the report viewer read the stored report's
`content.analysis`; they show only what the report holds. No backend contract was changed.

## Tests

* `npm test` (Vitest): primitives, formatting, charts, pipeline, document requirements, upload status
  vocabulary, readiness panel, peer comparison, preferences, access key, services.
* `npm run e2e` (Playwright, mocked backend): the full user journeys including failures.
* `npm run build` type-checks.

## Known limitations

* The readiness overview API has no last-updated date, and it includes companies whose only evidence is a
  price; the UI hides those by default (readiness below 10).
* Upload progress is indeterminate (the API reports processing status, not byte progress); the backend
  classifies documents itself, so there is no document-category selector.
* There is no company-list-by-sector API, so the directory browses the companies that have evidence.
* Report comparison across versions is not available.
* The Financials tab and report viewer depend on a report generated with the analytical layer; older versions
  say so and offer to generate a new one.
