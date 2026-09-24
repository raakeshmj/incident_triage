# apps/api

The public HTTP surface: alert-ingestion's `POST /api/v1/alerts` (Phase 1)
and `POST /api/v1/alerts/alertmanager` (Phase 3), and incident-core's
`GET /api/v1/incidents/{id}`, served from one FastAPI process.

`POST /api/v1/alerts/alertmanager` (`apps/api/routers/alerts.py`) adapts
Alertmanager's own webhook payload shape into the same
`AlertReceivedCommand` the direct-POST endpoint builds -- same component,
same DB boundary, not a parallel ingestion path. See
`docs/architecture/14-observability-and-chaos.md`'s "Alertmanager payload
mapping" for the field-by-field translation and ADR-0017 for why it's
reachable via `host.docker.internal` from the containerized Alertmanager.

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
