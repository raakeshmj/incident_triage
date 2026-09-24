# packages/incident

The sole writer of `Alert` and `Incident` state (incident-core). See
`docs/architecture/02-component-boundaries.md`,
`docs/architecture/03-domain-model.md`, and
`docs/architecture/06-database-design.md`.

## Layout

- `db/models.py` -- SQLAlchemy ORM models for the `incident_core` schema
  (Phase 1: `incidents`, `alerts`, `outbox_events`, `processed_commands`).
- `db/base.py` -- engine/session construction, using
  `INCIDENT_CORE_DATABASE_URL` (the `incident_core_role` connection, never
  the superuser -- see ADR-0013).
- `repository.py` -- persistence functions. Every write that has a
  documented race (new incident vs. concurrent alert, duplicate alert,
  duplicate command) uses Postgres `ON CONFLICT DO NOTHING` against the
  exact unique indexes from the migration, matching
  `docs/review/critical-review.md`'s documented mitigations.
- `service.py` -- `IncidentCoreService`, the only public entry point.
  `handle_alert_received` is one atomic transaction: persist Alert,
  correlate, persist Incident, write the outbox event(s), record the
  idempotency ledger entry -- all or nothing.
- `migrations/` -- Alembic, rooted at the repo's `alembic.ini`.

## Why other services don't import `db/*` directly

`apps/api`'s alert-ingestion router depends only on `IncidentCoreService`
(constructed once at process startup in `apps/api/dependencies.py`). It
never imports `packages.incident.db` -- this is what keeps alert-ingestion
without database credentials in code, not just in configuration (Phase 1
requirement 6).
