# Incident Intelligence

Autonomous Production Incident Triage & Response platform.

**Status: Phase 1 + Phase 2 implemented.** A working vertical slice, now
with a production-shaped event transport and a real correlation engine:

```
external alert -> alert-ingestion -> AlertReceivedCommand -> incident-core
    -> [correlation engine decides: new incident, or join an existing one]
    -> PostgreSQL -> AlertReceived / IncidentCreated / AlertCorrelated
    -> transactional outbox -> sharded Redis Streams -> durable consumer
```

See [`docs/`](docs/) for the full architecture,
[`docs/implementation-order.md`](docs/implementation-order.md) for what
this maps to and what's next, and ADR-0014/ADR-0015 for the two biggest
Phase 2 decisions (event transport, correlation engine). Nothing beyond
this scope is implemented yet: no Claude/LLM agent, no evidence service,
no policy engine, no remediation, no Kubernetes/Prometheus/Loki, no
dashboard.

## Repository layout

```
apps/
  api/          FastAPI process: alert-ingestion's POST /api/v1/alerts
                and incident-core's GET /api/v1/incidents/{id}
  worker/       the transactional outbox relay (Postgres -> Redis Stream)
  dashboard/    reserved for the Next.js UI (not implemented yet)

packages/
  domain/       pure domain models -- Alert, Incident, commands, events,
                the correlation engine, idempotency (no FastAPI/DB imports)
  events/       outbox envelope, sharded stream topology, publisher, and
                the reusable RedisStreamConsumer abstraction
  incident/     incident-core: the sole writer of Alert/Incident state
                (DB models, repository, service, Alembic migrations)
  telemetry/    structured logging, request context, tracing + metrics stubs
  agents/       reserved (Phase 5 -- investigation agent)
  tools/        reserved (Phase 5 -- evidence-service tool proxies)
  policy/       reserved (Phase 3 -- policy engine)
  evaluation/   reserved (Phase 5+ -- offline eval harness)

infrastructure/ docker-compose init scripts (Postgres schemas/roles)
simulator/      send_alert.py -- CLI to POST a synthetic alert
evals/          reserved for the eval harness's golden dataset
scripts/        dev-workflow helpers (wait_for_services.py)
tests/          unit / integration / e2e (see tests/README.md)
```

Each `packages/*` and `apps/*` directory's own README explains its
specific responsibility and links back to the architecture doc it
implements.

## Quickstart

Requires Docker, Python 3.11+.

```bash
cp .env.example .env
make install          # pip install -e ".[dev]"
make infra-up         # docker compose up -d (Postgres + Redis), waits for both
make migrate          # alembic upgrade head
make run-api          # uvicorn, in one terminal
```

In another terminal:

```bash
make send-alert       # POST a synthetic alert, then GET the resulting incident
```

You should see something like:

```json
POST /api/v1/alerts -> {"alert_id": "...", "incident_id": "...", "incident_created": true}
GET /api/v1/incidents/{id} -> {"id": "...", "status": "TRIAGING", "service": "checkout", ..., "alerts": [...]}
```

Or by hand, with `curl`:

```bash
curl -s -X POST localhost:8000/api/v1/alerts \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "prometheus",
    "labels": {"service": "checkout", "environment": "production", "alertname": "HighErrorRate"},
    "severity": "critical",
    "status": "firing"
  }'
# -> {"alert_id": "...", "incident_id": "<id>", "incident_created": true}

curl -s localhost:8000/api/v1/incidents/<id>
```

Run the outbox relay (publishes persisted events to sharded Redis Streams)
and the durable event consumer (records correlation/incident metrics) in
two more terminals:

```bash
make run-worker      # outbox relay: Postgres -> Redis
make run-consumer    # event consumer: Redis -> metrics, with DLQ + dedup
```

## Tests

```bash
make test              # everything
make test-unit         # packages/domain, packages/events -- no infrastructure needed
make test-integration  # packages/incident + packages/events against real Postgres/Redis
make test-e2e          # the FastAPI app in-process against real Postgres/Redis
```

Integration and e2e tests auto-skip with a clear message if
`make infra-up && make migrate` hasn't been run first.

## Lint / format / types

```bash
make lint        # ruff check + ruff format --check
make fmt          # ruff format + ruff check --fix
make typecheck    # mypy
```

## Tearing down

```bash
make infra-down   # docker compose down -v (drops the Postgres volume too)
```

## Documentation

- [`docs/README.md`](docs/README.md) — documentation index
- [`docs/architecture/`](docs/architecture/) — component-by-component design
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`docs/review/critical-review.md`](docs/review/critical-review.md) — self-critique
- [`docs/implementation-order.md`](docs/implementation-order.md) — build sequence

## Core rule

**The LLM must never own system state or bypass deterministic controls.**
Claude reasons and proposes. Deterministic code (state machine, policy
engine, action catalog) decides, enforces, and executes. Every claim the
model makes about the world must be backed by a stored, replayable
evidence record — never by the model's own assertion. Phase 1 has no LLM
in it at all yet; this rule shapes every phase from here on.
