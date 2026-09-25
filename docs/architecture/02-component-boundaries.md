# 02 — Component Boundaries

## Why these boundaries and not others

Splits are drawn along **trust boundary**, **credential scope**, and
**scaling axis** — not along "one microservice per noun." See ADR-0001.
Two things never share a boundary: (a) anything that writes production
state, and (b) anything that talks to Claude with untrusted content in
context.

## Service inventory

| Service | Type | Talks to Claude? | Writes prod DB? | Writes external systems? | Why it's separate |
|---|---|---|---|---|---|
| `alert-ingestion` | FastAPI, stateless | No | No (hands off via command) | No | Public-facing trust boundary; different auth/rate-limit posture than everything internal |
| `incident-core` | FastAPI + Postgres | No (calls investigation-agent, doesn't embed the LLM) | **Yes — sole writer** | No | Owns the state machine; must be transactionally consistent |
| `investigation-agent` | Python worker | **Yes** | No | No | Token-cost/latency scaling is different from everything else; isolated blast radius if Claude output is bad |
| `evidence-service` | FastAPI, read-only aggregator | No | Writes only its own append-only evidence store | Read-only calls out (Prometheus/Loki/Tempo/Git/k8s API/history) | The *only* place allowed to query external systems for facts — this is the anti-hallucination boundary |
| `policy-engine` | Library + thin FastAPI wrapper | No | No | No | Security-critical, must be independently testable/auditable; pure function of (proposal, policy version) → decision |
| `remediation-executor` | Python worker | No | No (reports results back via command) | **Yes — sole writer to prod systems** | Holds privileged, scoped credentials; blast-radius isolation demands its own namespace/network policy |
| `notification-service` | Python worker, stateless | No | No | Yes (Slack/email/webhook, non-production) | Fire-and-forget side effect, must never block the state machine |
| `eval-harness` | Offline CLI/pipeline (pytest-based) | Yes (in replay mode) | No (separate eval store) | No (fixtures only) | Runs in CI, not in the runtime path; needs to run without live systems |
| `web-ui` (`apps/web`) | Next.js + TypeScript | No | No | No | BFF pattern — talks only to incident-core's public API |

`incident-core` internally contains the modules that must share a
transaction: **correlation**, **the state machine/orchestrator**,
**approvals**, and **timeline/audit read API**. These are not separate
services (see ADR-0001) — splitting them would force distributed
transactions across an aggregate that must be strongly consistent, for no
scaling or trust benefit.

## Ownership table (who may write what)

| Data | Sole writer | Everyone else |
|---|---|---|
| `alerts` | `incident-core` (persists, correlates, and links to an incident, all in one transaction) | `alert-ingestion` never writes this table — it sends an `AlertReceivedCommand` and incident-core decides what to persist |
| `incidents`, `investigations`, `hypotheses`, `rca_reports` | `incident-core` | read-only; `investigation-agent` submits *proposed* content via command, incident-core validates and persists (Phase 5: per-step checkpoint commands, ADR-0020) |
| `evidence`, `evidence_blobs` | `evidence-service` | read-only; referenced by ID only |
| `remediation_proposals`, `policy_decisions` | `incident-core` (persists), `policy-engine` (computes decision, stateless) | investigation-agent proposes; nothing else writes |
| `approvals` | `incident-core` | web-ui/Slack submit a command; incident-core records it |
| `executions` | `incident-core` (creates the record with idempotency key before dispatch), `remediation-executor` (updates result via command) | read-only |
| `verifications`, `verification_evidence` | `incident-core` | evidence-service supplies the data it re-checks against, referenced by ID only |
| `events` (outbox) | whichever service owns the aggregate that changed | append-only, never mutated |
| `action_catalog`, `policies` | Configuration, deployed via CI/CD from a reviewed repo path, not runtime-writable by any service | read-only at runtime |
| `kill_switches` | `incident-core` (the write path is a `platform_admin`-only admin action, still routed through incident-core, never a direct DB edit) | read-only; unlike `action_catalog`/`policies` this *is* runtime-writable, deliberately — see ADR-0009. `policy-engine` never queries it directly, only via the `PolicyEvaluationContext` incident-core builds (see `09-remediation-policy-boundaries.md`) |

## Communication patterns

- **Commands** (synchronous, request/response, HTTP+Pydantic schemas):
  `alert-ingestion → incident-core` (`AlertReceivedCommand`),
  `investigation-agent → incident-core` (`InvestigationCompletedCommand` /
  `InvestigationFailedCommand`), `remediation-executor → incident-core`
  (`ExecutionCompletedCommand` / `ExecutionFailedCommand`),
  `web-ui → incident-core` (everything the UI does). Every command is
  idempotent, keyed by `(command_type, idempotency_key)` — see
  `05-event-model.md` and ADR-0011.
- **Queries** (synchronous, read-only): `web-ui → incident-core`,
  `investigation-agent → evidence-service` (via tool calls),
  `incident-core → policy-engine` (evaluate proposal).
- **Events** (asynchronous, at-least-once, via Redis Streams, fanned out
  from incident-core's outbox): drive `notification-service`, feed
  `eval-harness` recording, feed the timeline read model.
- **No service calls `remediation-executor` except `incident-core`**, and
  only after a persisted `ALLOW` or `APPROVED` decision. This is checked in
  code (executor verifies the execution record's approval status itself
  before acting — see `09-remediation-policy-boundaries.md`), not only by
  convention.

## Repository layout mapping

```
services/
  alert-ingestion/
  incident-core/
  investigation-agent/
  evidence-service/
  policy-engine/
  remediation-executor/
  notification-service/
  eval-harness/
apps/
  web/
libs/
  schemas/        # shared Pydantic models: events, commands, evidence, action catalog contracts
  otel/           # shared OpenTelemetry setup
infra/
  docker-compose/ # local dev
  kind/           # local k8s
  k8s/            # base manifests (kustomize overlays per env, later)
```

Each `services/*` directory currently contains only a `README.md`
describing its API surface and data ownership (see repo root). No code yet.
