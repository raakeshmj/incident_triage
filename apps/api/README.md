# apps/api

The public HTTP surface for Phase 1: alert-ingestion's
`POST /api/v1/alerts` and incident-core's `GET /api/v1/incidents/{id}`,
served from one FastAPI process.

## Why one process for two architectural components

`docs/architecture/02-component-boundaries.md` describes alert-ingestion
and incident-core as separate trust boundaries. For Phase 1 they run in
the same OS process to avoid standing up a network hop neither the
architecture nor this phase's requirements call for yet -- but the
boundary is enforced at the **code** level, not just documented:
`apps/api/routers/alerts.py` never imports `packages.incident.db` and
holds no database credentials; it only depends on the
`IncidentCoreService` abstraction from `packages/incident/service.py`,
constructed in `apps/api/dependencies.py` (the composition root). If
alert-ingestion is later split into its own deployable, that router
becomes an HTTP client of the same interface without changing its
request-handling logic.

## Running

See the repository root README for the full local-dev flow. Short
version: `make infra-up && make migrate && make run-api`.
