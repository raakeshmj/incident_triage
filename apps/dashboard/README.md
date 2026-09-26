# apps/dashboard — operations console

The Incident Intelligence operations console (Phase 8): Vite + React 19 +
TypeScript + IBM Carbon (`@carbon/react`), following `DESIGN.md`.

## Rules

- **The API is the only backend.** Everything comes from the typed read API
  (`/api/v1/incidents`, `/incidents/{id}/detail`, `/overview`, `/metrics`,
  `/evidence/{id}`) through `src/api/client.ts`. No database access, no
  domain logic: statuses, verdicts and metrics are rendered as returned.
- **One mutation:** approving or rejecting a remediation
  (`POST /api/v1/remediations/{id}/approval`), bound to the proposal hash and
  policy decision id, with the operator token (kept in `sessionStorage`
  only).

## Views

| Route | View |
|---|---|
| `/` | incident list: filters (status, severity, service, opened) kept in the URL; table → compact table (< 1056 px) → stacked list (< 672 px) |
| `/incidents/:id` | header, lifecycle, "Approval needed" + approval dialog, timeline (condensable), impact, root cause, investigation attempts and hypotheses, remediation, verification (checks: baseline → latest → expected; observations) — every evidence id opens its evidence record |
| `/operations` | counters (active, running, awaiting approval, dead letters, failed outbox), worker heartbeats, recent escalations, incidents by status, lifecycle metrics (from recorded timestamps only; "no data" when none) |

## Run

```bash
export IBM_TELEMETRY_DISABLED=true      # Carbon's install telemetry
npm ci
API_PORT=8000 npm run dev               # http://localhost:5173, /api proxied to the API
npm run typecheck && npm test && npm run build
npx playwright install chromium && npm run test:e2e
DASHBOARD_LIVE=1 API_PORT=8000 npx playwright test e2e/live.spec.ts   # against the real API
```

`make seed-demo` (repo root) fills the dev database with incidents in every
lifecycle state.

## Tests

- `src/**/*.test.ts(x)` (vitest + Testing Library): API mapping and query
  serialisation, list (rows, filters, URL state, empty / error / loading),
  detail (resolved and failed verification, timeline + condensing, evidence
  dialog, approval binding and refusal), operations. The fake API serves
  `tests/fixtures/*.json`, captured from the real API.
- `e2e/*.spec.ts` (Playwright, desktop 1440 / laptop 1024 / mobile 390×844):
  navigation, filters, detail, evidence, approval → state change, overflow,
  keyboard, axe (WCAG 2 A/AA: no serious or critical violations), loading /
  error / empty / unreachable; `live.spec.ts` against the real API.

## Design

`DESIGN.md` is the visual source of truth (IBM / Carbon analysis from
VoltAgent/awesome-design-md, MIT). `design/references/` holds the reference
compositions and their screenshots; `design/screenshots/` the Playwright
validation captures (`iter*` = before fixes, `final-*` = shipped). Workflow
and deviations: `docs/frontend/design-workflow.md`.
