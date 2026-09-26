# Incident Intelligence

Autonomous Production Incident Triage & Response platform.

**Status: Phases 1-8 implemented (the final planned phase).** A working vertical slice with a
production-shaped event transport, a real correlation engine, a realistic
local production environment feeding it real alerts, (Phase 4) the trusted
evidence and investigation substrate, and (Phase 5) a bounded, read-only,
evidence-grounded investigation agent, and (Phase 6) an evaluation and
replay framework for it, and (Phase 7) human-approved, policy-gated,
bounded remediation against the simulator, and (Phase 8) deterministic,
evidence-based verification that closes the loop, plus an operations console:

```
checkout/payment/inventory services (simulated, chaos-injectable)
    -> OpenTelemetry traces/metrics/logs -> Prometheus / Loki / Grafana
    -> Prometheus alert rules -> Alertmanager
    -> alert-ingestion (POST /api/v1/alerts/alertmanager) -> AlertReceivedCommand
    -> incident-core
    -> [correlation engine decides: new incident, or join an existing one]
    -> PostgreSQL -> AlertReceived / IncidentCreated / AlertCorrelated
    -> transactional outbox -> sharded Redis Streams -> durable consumer
    <- resolved alerts: TRIAGING -> CANCELLED once no linked alert fires

incident
    -> investigation tool (packages/tools; strict contracts, not connected to any model)
    -> evidence-service (packages/evidence; scope-checked, bounded, allow-listed)
    -> adapter -> Prometheus | Loki | Tempo | deployments | config | Git | incident history
    -> immutable, content-hashed EvidenceRecord + incident-core EvidenceRef
    -> compact result carrying an evidence_id

incident TRIAGING (debounce elapsed) -> INVESTIGATING -> InvestigationStarted
    -> investigation worker -> InvestigationEngine (packages/agents)
    -> model (INVESTIGATION_MODEL, default claude-haiku-4-5) <-> read-only tools
    -> hypotheses validated + persisted by incident-core, every step checkpointed
    -> deterministic stopping criteria -> RCA_READY (evidence-backed RCA) | ESCALATED

golden scenario -> real evidence path (canned backends) -> engine -> recording
    -> structured grade -> eval-results/ ; recording -> replay (inspect | re-execute)

RCA_READY -> planner (deterministic) -> proposal -> policy (pure) -> AWAITING_APPROVAL
    -> human approval (API, bound to the exact proposal) -> runner -> catalog action
    on the simulator (idempotent, bounded, reconciled) -> EXECUTED -> VERIFYING

VERIFYING -> verification worker -> evidence-service observations vs. baseline
    -> N consecutive passing observations -> RESOLVED
    -> FAILED -> VERIFICATION_FAILED -> re-investigation (bounded) | ESCALATED
    -> no conclusive evidence by the deadline -> TIMED_OUT -> ESCALATED

operations console (apps/dashboard, Carbon) -> typed read API (/api/v1) only
```

See [`docs/`](docs/) for the full architecture,
[`docs/implementation-order.md`](docs/implementation-order.md) for what
this maps to and what's next, ADR-0014/ADR-0015 for the two biggest
Phase 2 decisions (event transport, correlation engine), and
[`docs/architecture/14-observability-and-chaos.md`](docs/architecture/14-observability-and-chaos.md)
(+ ADR-0016/ADR-0017) for Phase 3's simulated services, telemetry, alert
rules, and chaos scenarios, and
[`docs/architecture/08-evidence-model.md`](docs/architecture/08-evidence-model.md)
/ [`07-agent-tool-architecture.md`](docs/architecture/07-agent-tool-architecture.md)
(+ ADR-0018/ADR-0019) for Phase 4's evidence service, tool contracts, and
alert resolution, and
[`docs/architecture/15-investigation-engine.md`](docs/architecture/15-investigation-engine.md)
(+ ADR-0020/ADR-0021/ADR-0022) for Phase 5's investigation engine and
prompt caching, and
[`docs/architecture/11-evaluation-architecture.md`](docs/architecture/11-evaluation-architecture.md)
(+ ADR-0023) for Phase 6's evaluation and replay, and
[`docs/architecture/09-remediation-policy-boundaries.md`](docs/architecture/09-remediation-policy-boundaries.md)
("Phase 7: as built", + ADR-0024) for Phase 7's remediation, and
[`docs/architecture/10-verification-design.md`](docs/architecture/10-verification-design.md)
(+ ADR-0025) and [`docs/operations.md`](docs/operations.md) for Phase 8's
verification, closed loop and local operation. Not implemented: automatic
(pre-approved) remediation, real production executors, Kubernetes,
identity-provider auth, the `CLOSED`/`SUPPRESSED` states.

## Repository layout

```
apps/
  api/          FastAPI process: alert-ingestion's POST /api/v1/alerts
                and incident-core's GET /api/v1/incidents/{id}
  worker/       the transactional outbox relay (Postgres -> Redis Stream),
                the metrics consumer, and the investigation worker (Phase 5)
  evidence/     evidence-service's internal API: tools + evidence replay/audit (Phase 4)
  dashboard/    Phase 8: the operations console (Vite + React + Carbon)

packages/
  domain/       pure domain models -- Alert, Incident, commands, events,
                the correlation engine, idempotency (no FastAPI/DB imports)
  events/       outbox envelope, sharded stream topology, publisher, and
                the reusable RedisStreamConsumer abstraction
  incident/     incident-core: the sole writer of Alert/Incident state
                (DB models, repository, service, Alembic migrations)
  telemetry/    structured logging, request context, tracing + metrics stubs
  evidence/     evidence-service: adapters, immutable evidence store, scope (Phase 4)
  tools/        investigation tool contracts over evidence-service (Phase 4)
  agents/       the investigation engine, model abstraction, Claude adapter,
                read-only toolset, prompts (Phase 5)
  policy/       Phase 7: the pure policy engine
  remediation/  Phase 7: action catalog, planner, executor boundary, runner
  verification/ Phase 8: evidence observer + tick-based verification engine
  evaluation/   Phase 6: golden-scenario worlds, recordings, replay, grading,
                harness and the `evaluate` / `replay` CLI

infrastructure/ docker-compose configs: Postgres schemas/roles, Phase 3's
                otel-collector/prometheus/loki+promtail/grafana/alertmanager,
                Phase 4's tempo and evidence service catalog
simulator/      send_alert.py (synthetic alert CLI), services/ (3
                simulated production services + load-generator, Phase 3),
                chaos/ (7 chaos scenarios + CLI), changes/ (simulated
                deployment + config registries, Phase 4), scenarios.md
evals/          golden scenarios (evals/scenarios/*.json) + their service catalog
scripts/        dev-workflow helpers (wait_for_services.py) and the manual
                real-model investigation (manual_investigation.py)
tests/          unit / integration / e2e (see tests/README.md)
```

Each `packages/*` and `apps/*` directory's own README explains its
specific responsibility and links back to the architecture doc it
implements.

## Quickstart

Requires Docker, Python 3.11+. Every entrypoint reads `.env` itself
(pydantic-settings / python-dotenv) and every `make` target exports it, so
`KEY=value` lines there are all the configuration needed.

```bash
python3 -m venv .venv             # once
source .venv/bin/activate         # every new shell
cp .env.example .env              # then add ANTHROPIC_API_KEY=... for Phase 5/6 live runs
make install          # pip install -e ".[dev]"
make infra-up         # docker compose up -d (Postgres + Redis), waits for both
make migrate          # both schemas: incident_core, then evidence (its own role)
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

## Phase 4: evidence and investigation substrate

With the full environment up (`make infra-up-full` -- now also Tempo, and it
seeds the simulated deployment/config registries), run the API and the
internal evidence-service:

```bash
make run-api        # :8000 -- Alertmanager delivers here
make run-evidence   # :8010 -- internal only: tools + evidence replay/audit
```

Walk a real incident from alert to evidence to resolution:

```bash
python -m simulator.chaos.cli start bad-deployment --service checkout-service
# ~45s later: HighErrorRate fires -> Alertmanager -> a TRIAGING incident
docker compose exec postgres psql -U postgres -d incident_intelligence \
  -c "select id, status, service from incident_core.incidents order by created_at desc limit 3"

E=localhost:8010/internal/v1/incidents/<id>
curl -s $E/tools/get_metrics        -H 'Content-Type: application/json' -d '{"arguments": {"metric": "error_rate"}}'
curl -s $E/tools/get_logs           -H 'Content-Type: application/json' -d '{"arguments": {"severities": ["ERROR"]}}'
curl -s $E/tools/get_traces         -H 'Content-Type: application/json' -d '{"arguments": {"mode": "errors"}}'
curl -s $E/tools/get_trace          -H 'Content-Type: application/json' -d '{"arguments": {"trace_id": "<from get_traces>"}}'
curl -s $E/tools/get_deploys        -H 'Content-Type: application/json' -d '{}'
curl -s $E/tools/get_recent_commits -H 'Content-Type: application/json' -d '{}'
curl -s $E/evidence                                    # every record, replay order, with provenance
curl -s localhost:8010/internal/v1/evidence/<evidence_id>/verify

python -m simulator.chaos.cli stop --service checkout-service   # records the rollback
# ~1 min later the alert resolves and the incident goes TRIAGING -> CANCELLED
```

`GET localhost:8010/internal/v1/tools` lists every tool's JSON Schema.

## Phase 5: investigations

```bash
make infra-up-full && make migrate
make run-api                    # Alertmanager delivers here
# ANTHROPIC_API_KEY in .env (or the environment); never commit it
make run-investigation-worker   # scheduler + InvestigationStarted consumer + resume sweep
python -m simulator.chaos.cli start bad-deployment --service checkout-service
# ~45s: incident TRIAGING; +60s debounce: INVESTIGATING; then RCA_READY or ESCALATED
```

The runtime model is configuration: `INVESTIGATION_MODEL=claude-haiku-4-5`
(or any id; see `packages/agents/config.py`'s `MODEL_PROFILES`) with no code
change. Each investigation records the model it used, every model turn, tool
call, hypothesis change and the final RCA in `incident_core`
(`investigation_steps`, `hypotheses`, `rca_reports`).

`make investigate-live` runs the single manual real-model investigation
(`scripts/manual_investigation.py`) of a live bad-deployment incident and
writes the full trace to `investigation-traces/`. It spends real tokens and
is never part of `make test`, which calls no model API.

## Phase 6: evaluation and replay

```bash
make eval-db eval-migrate                  # once: the disposable evaluation database
evaluate --list                            # 17 golden scenarios (10 RCA, 7 must-escalate)
evaluate --all --mode fake                 # offline, deterministic, no credentials
evaluate --scenario bad-deployment --mode fake --runs 3
replay --trace <recording-id>              # what happened, from the file alone
replay --trace <recording-id> --verify     # re-execute: same decisions? (no model/telemetry)
replay --export <investigation-id>         # record a live investigation from the main DB
```

Each run writes a recording to `investigation-traces/` and a graded result
to `eval-results/`. `--mode live` uses the configured
`INVESTIGATION_PROVIDER` / `INVESTIGATION_MODEL` (placeholders; defaults
`anthropic` / `claude-haiku-4-5`), needs that provider's credential and
`--yes`, and is never part of `make test`. Live runs are deferred until a
credential is available.

## Phase 7: remediation

```bash
make run-api                     # operator endpoints need OPERATOR_API_TOKEN + REMEDIATION_APPROVERS
make run-remediation-worker      # plans on InvestigationCompleted, executes on RemediationApproved
curl -s localhost:8000/api/v1/incidents/<id>/remediations      # proposal, status, proposal_hash
curl -s localhost:8000/api/v1/remediations/<rid>               # decisions (with context), executions, timeline
curl -s -X POST localhost:8000/api/v1/remediations/<rid>/approval \
  -H "Authorization: Bearer $OPERATOR_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"approver": "alice", "decision": "approve", "proposal_hash": "<hash>", "policy_decision_id": "<id>"}'
curl -s -X PUT localhost:8000/api/v1/kill-switches/global \
  -H "Authorization: Bearer $OPERATOR_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"engaged": true, "actor": "carol", "reason": "freeze"}'
```

Every remediation needs a human: policy never allows automatic execution.
The executor acts only on the simulated environment.

## Phase 8: verification and the operations console

```bash
make run-verification-worker     # VerificationRequested -> observe via evidence -> verdict
make seed-demo                   # demo incidents in every lifecycle state (dev DB)
make dashboard-install && make dashboard-dev    # http://localhost:5173
curl -s localhost:8000/api/v1/incidents?status=RESOLVED
curl -s localhost:8000/api/v1/incidents/<id>/detail   # timeline, RCA, remediation, verification
curl -s localhost:8000/api/v1/overview                 # counts, DLQ length, worker heartbeats
curl -s localhost:8000/api/v1/metrics                  # durations/rates from recorded timestamps
```

RESOLVED only after a PASSED verification; a failed verification never
triggers another remediation automatically. Walkthrough:
[`docs/operations.md`](docs/operations.md).

## Tests

```bash
make test              # everything
make test-unit         # packages/domain, packages/events -- no infrastructure needed
make test-integration  # packages/incident + packages/events against real Postgres/Redis
make test-e2e          # the FastAPI app in-process against real Postgres/Redis
make test-stack        # needs `make infra-up-full`: live Prometheus/Loki/Tempo
                       # adapters + the live chaos-incident e2e (~2-4 min)
                       # and the live lifecycle scenarios (~8 min)
make dashboard-check   # dashboard typecheck + vitest + production build
make dashboard-e2e     # Playwright: desktop / 1024 / 390x844, axe checks
```

Integration and e2e tests auto-skip with a clear message if
`make infra-up && make migrate` hasn't been run first; `stack`-marked tests
skip unless the full environment is up.

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
- [`docs/operations.md`](docs/operations.md) — running the whole loop locally
- [`docs/frontend/design-workflow.md`](docs/frontend/design-workflow.md) — dashboard design workflow
- [`simulator/scenarios.md`](simulator/scenarios.md) — Phase 3's 6 worked incident scenarios

## Core rule

**The LLM must never own system state or bypass deterministic controls.**
Claude reasons and proposes. Deterministic code (state machine, policy
engine, action catalog) decides, enforces, and executes. Every claim the
model makes about the world must be backed by a stored, replayable
evidence record — never by the model's own assertion. Phase 5's agent is read-only:
it can gather evidence and propose hypotheses and conclusions, and
incident-core decides whether they stand.
