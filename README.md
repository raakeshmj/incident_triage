# Incident Intelligence

Autonomous Production Incident Triage & Response platform.

**Status: Phase 1 + Phase 2 + Phase 3 implemented.** A working vertical
slice with a production-shaped event transport, a real correlation
engine, and now a realistic local production environment feeding it real
alerts:

```
checkout/payment/inventory services (simulated, chaos-injectable)
    -> OpenTelemetry traces/metrics/logs -> Prometheus / Loki / Grafana
    -> Prometheus alert rules -> Alertmanager
    -> alert-ingestion (POST /api/v1/alerts/alertmanager) -> AlertReceivedCommand
    -> incident-core
    -> [correlation engine decides: new incident, or join an existing one]
    -> PostgreSQL -> AlertReceived / IncidentCreated / AlertCorrelated
    -> transactional outbox -> sharded Redis Streams -> durable consumer
```

See [`docs/`](docs/) for the full architecture,
[`docs/implementation-order.md`](docs/implementation-order.md) for what
this maps to and what's next, ADR-0014/ADR-0015 for the two biggest
Phase 2 decisions (event transport, correlation engine), and
[`docs/architecture/14-observability-and-chaos.md`](docs/architecture/14-observability-and-chaos.md)
(+ ADR-0016/ADR-0017) for Phase 3's simulated services, telemetry, alert
rules, and chaos scenarios. Nothing beyond this scope is implemented yet:
no Claude/LLM agent, no evidence service, no policy engine, no
remediation, no Kubernetes, no Incident Intelligence dashboard.

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

infrastructure/ docker-compose configs: Postgres schemas/roles, and
                Phase 3's otel-collector/prometheus/loki+promtail/grafana/
                alertmanager
simulator/      send_alert.py (synthetic alert CLI), services/ (3
                simulated production services + load-generator, Phase 3),
                chaos/ (7 chaos scenarios + CLI), scenarios.md (5+
                documented incidents)
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

## Phase 3: the local production environment

Three simulated services (checkout -> payment -> inventory), a load
generator, and a full observability/alerting stack, so Incident
Intelligence receives *real* Alertmanager alerts instead of only
hand-sent synthetic ones. See
[`docs/architecture/14-observability-and-chaos.md`](docs/architecture/14-observability-and-chaos.md)
for the full design and [`simulator/scenarios.md`](simulator/scenarios.md)
for 6 worked incident scenarios.

```bash
make run-api           # the API must be running -- Alertmanager delivers to it
make infra-up-full     # docker compose up -d: adds the simulated services,
                        # load generator, otel-collector, prometheus, loki,
                        # promtail, grafana, alertmanager to postgres/redis
```

Then, in a few minutes (the load generator needs to produce enough
traffic for the alert rules' `rate()` windows):

- Prometheus: http://localhost:9090 (Alerts tab shows pending/firing rules)
- Alertmanager: http://localhost:9093
- Grafana: http://localhost:3000 (anonymous admin access, local dev only)
- checkout/payment/inventory: http://localhost:8001/8002/8003 (`/health`, `/metrics`)

Trigger a chaos scenario and watch it become a real incident:

```bash
python -m simulator.chaos.cli list                                       # see all 7 scenarios
python -m simulator.chaos.cli start high-latency --service checkout-service
# ...wait ~45s for HighP95Latency to fire in Prometheus, then Alertmanager
# delivers it to POST /api/v1/alerts/alertmanager...
curl -s localhost:8000/api/v1/incidents/<id>   # the incident it created
python -m simulator.chaos.cli stop --service checkout-service
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
- [`simulator/scenarios.md`](simulator/scenarios.md) — Phase 3's 6 worked incident scenarios

## Core rule

**The LLM must never own system state or bypass deterministic controls.**
Claude reasons and proposes. Deterministic code (state machine, policy
engine, action catalog) decides, enforces, and executes. Every claim the
model makes about the world must be backed by a stored, replayable
evidence record — never by the model's own assertion. Phase 1 has no LLM
in it at all yet; this rule shapes every phase from here on.
