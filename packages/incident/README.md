# packages/incident

The sole writer of `Alert` and `Incident` state (incident-core). See
`docs/architecture/02-component-boundaries.md`,
`docs/architecture/03-domain-model.md`, and
`docs/architecture/06-database-design.md`.

## Layout

- `db/models.py` -- SQLAlchemy ORM models for the `incident_core` schema:
  `incidents`, `alerts`, `outbox_events` (with Phase 2's `producer`,
  `publish_attempts`, `last_publish_error` columns), `processed_commands`,
  `consumed_events` (Phase 2 consumer-side idempotency ledger).
- `db/base.py` -- engine/session construction, using
  `INCIDENT_CORE_DATABASE_URL` (the `incident_core_role` connection, never
  the superuser -- see ADR-0013).
- `repository.py` -- persistence functions. Every write that has a
  documented race (new incident vs. concurrent alert, duplicate alert,
  duplicate command) uses Postgres `ON CONFLICT DO NOTHING` against the
  exact unique indexes from the migration, matching
  `docs/review/critical-review.md`'s documented mitigations. Phase 2 adds
  `acquire_correlation_lock` (an advisory lock serializing correlation
  decisions per service+environment) and `find_open_incident_candidates`
  (the correlation engine's candidate query) -- see ADR-0015.
- `service.py` -- `IncidentCoreService`, the only public entry point.
  `handle_alert_received` is one atomic transaction: acquire the
  correlation lock, persist Alert, run the correlation engine
  (`packages/domain/correlation_engine.py`) against open candidates,
  persist/link the Incident, write the outbox event(s)
  (`AlertReceived` + `IncidentCreated` or `AlertCorrelated`), record the
  idempotency ledger entry -- all or nothing. Takes an injectable `clock`
  for deterministic testing of time-windowed correlation decisions.
  Phase 4 adds the resolved-alert path (`TRIAGING -> CANCELLED` once no
  linked alert is firing, under the same lock, with an optimistic
  `version` bump -- ADR-0019), `register_evidence_ref` (the one command
  evidence-service sends -- ADR-0018), and read queries evidence-service
  uses through its `IncidentGateway` (`list_incident_summaries`,
  `list_evidence_refs`).
- `migrations/` -- Alembic, rooted at the repo's `alembic.ini`.
  `0001_initial_schema` (Phase 1), `0002_events_correlation` (Phase 2),
  `0003_resolution_evidence` (Phase 4: `alerts.resolved_at`, immutable
  `evidence_refs`).

## Why other services don't import `db/*` directly

`apps/api`'s alert-ingestion router depends only on `IncidentCoreService`
(constructed once at process startup in `apps/api/dependencies.py`). It
never imports `packages.incident.db` -- this is what keeps alert-ingestion
without database credentials in code, not just in configuration (Phase 1
requirement 6).
