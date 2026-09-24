# tests

- `unit/` -- pure logic, no infrastructure. `packages/domain` (including
  the correlation engine) and `packages/events` (envelope validation,
  retry classification, and the consumer's orchestration logic against
  `tests/fakes.FakeRedisStreams`, an in-memory Redis Streams double).
  `make test-unit` / `pytest tests/unit`.
- `integration/` -- exercises `packages/incident` and `packages/events`
  against real local Postgres and Redis: alert-to-incident persistence,
  outbox atomicity, command idempotency, alert dedup, outbox relay
  retries, Redis consumer + dead-letter behavior, and the correlation
  engine's concurrency (Case E) and late-arrival (Case F) guarantees.
  Requires `make infra-up && make migrate` first. `make test-integration`.
- `e2e/` -- drives the FastAPI app in-process (`TestClient`) against real
  Postgres (and, for the crash-recovery scenario, the outbox relay
  directly): the six Phase 2 correlation scenarios in
  `test_correlation_scenarios.py`, plus the original Phase 1 alert flow.
  `make test-e2e`.
- `fakes.py` -- fault-injection helpers: `FlakyPublisher` /
  `AlwaysFailingPublisher` (outbox relay retry testing) and
  `FakeRedisStreams` (a minimal in-memory Redis Streams double for
  unit-testing the consumer without a live server).

Integration and e2e tests auto-skip with a clear message if Postgres or
Redis isn't reachable, rather than failing opaquely.

Run everything: `make test` (equivalent to `pytest`).
