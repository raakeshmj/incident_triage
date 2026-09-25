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
  `test_alertmanager_webhook.py` covers the Phase 3 Alertmanager webhook
  adapter's payload mapping and correlation behavior (still in-process).
  `test_alertmanager_container.py` is different: it starts the *real*
  `prom/alertmanager` Docker container against the *real*
  `infrastructure/alertmanager/alertmanager.yml` and a real uvicorn server
  for the app, and fires a synthetic alert through Alertmanager's own API
  -- the one hop Phase 3 requires not to fake. It auto-skips (like the
  Postgres/Redis fixtures) if Docker isn't reachable. `make test-e2e`.
- `fakes.py` -- fault-injection helpers: `FlakyPublisher` /
  `AlwaysFailingPublisher` (outbox relay retry testing) and
  `FakeRedisStreams` (a minimal in-memory Redis Streams double for
  unit-testing the consumer without a live server).

Phase 4 additions:

- `unit/evidence/` -- hashing, sanitizing, scope/window/limit validation, and
  every adapter against canned backend responses (MockTransport).
- `unit/tools/` -- tool contracts, strict inputs, executor retries/budgets,
  `submit_findings`' exactly-one-outcome rule.
- `unit/test_boundaries.py` -- import-level enforcement of
  agent -> tools -> evidence service -> backends (and alert-ingestion's
  no-DB rule).
- `integration/test_evidence_store.py` -- persistence, refs, DB-level
  immutability, per-role schema confinement, replay order, verification.
- `integration/test_change_sources.py` -- deployment/config registries
  (isolated Redis DB 15), the Git adapter on this repository, historical
  incident search.
- `integration/test_alert_resolution.py` + `e2e/test_alertmanager_webhook.py`
  -- resolution semantics, including the concurrent resolve-vs-correlate case.
- `stack`-marked (`make test-stack`, needs `make infra-up-full`):
  `integration/test_telemetry_adapters.py` (live Prometheus/Loki/Tempo) and
  `e2e/test_evidence_live_incident.py` (real chaos incident -> evidence ->
  resolution). The Phase 3 `e2e/test_alertmanager_container.py` starts its
  own Alertmanager on :9093, so it runs under `make infra-up` and skips when
  the full stack already holds that port.

Integration and e2e tests auto-skip with a clear message if Postgres or
Redis isn't reachable, rather than failing opaquely.

Run everything: `make test` (equivalent to `pytest`).

Phase 5 additions (no test calls a model API; `FakeInvestigationModel`
scripts turns that react to the real tool results):

- `unit/domain/test_investigation.py` -- schemas, turn interpretation,
  hypothesis update rules, the deterministic stopping criteria.
- `unit/agents/` -- model config/profiles (switching `INVESTIGATION_MODEL`
  without code changes), the Claude adapter's request building and error
  mapping against a fake SDK client, toolset and transcript rebuilding.
- `unit/test_boundaries.py` -- the agent has no path to DB/Redis/HTTP/shell,
  and only `packages/agents/claude.py` imports the SDK.
- `integration/test_investigation_engine.py` -- the real engine, incident-core
  and EvidenceService: success, trace, invented/unseen evidence ids,
  contradictions, inconclusive, every budget, retryable/terminal model
  errors, malformed output, evidence outage, crash + resume, pending
  actions, duplicate start/run, lease loss.
- `e2e/test_investigation_event_flow.py` -- scheduler -> outbox -> Redis ->
  consumer -> engine -> `RCA_READY`, redelivery is a no-op.
- `e2e/test_investigation_live_stack.py` (`stack`) -- a real chaos incident
  investigated through the live evidence backends.
- `scripts/manual_investigation.py` (`make investigate-live`) is the one
  real-model run; it is a script, not a test, and is never collected.
