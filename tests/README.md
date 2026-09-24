# tests

- `unit/` -- pure domain logic, no infrastructure. `make test-unit` /
  `pytest tests/unit`.
- `integration/` -- exercises `packages/incident` against a real local
  Postgres (and Redis, for the one publisher test). Requires
  `make infra-up && make migrate` first. `make test-integration`.
- `e2e/` -- drives the FastAPI app in-process (`TestClient`) against the
  same real Postgres: POST an alert, GET the incident, verify the outbox
  event. `make test-e2e`.

Integration and e2e tests auto-skip with a clear message if Postgres
isn't reachable, rather than failing opaquely.

Run everything: `make test` (equivalent to `pytest`).
