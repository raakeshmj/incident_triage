# 05 — Event Model

## Two different things people call "events" — kept separate on purpose

1. **Domain events** (the outbox / audit log): the durable, ordered,
   append-only record of everything that happened to an aggregate. Source
   of truth. Live in Postgres (`events` table), one row per event, forever.
2. **Bus messages** (Redis Streams): a *fan-out mechanism* for domain
   events, used by consumers that don't need transactional consistency
   with the aggregate (notifications, eval-harness recording, timeline
   cache warming). At-least-once, replayable, but **not** the source of
   truth and not required for correctness of the state machine itself.

This split exists because Redis Streams alone cannot give us atomic
"commit the state change and the event in the same transaction" —
publishing to Redis and writing to Postgres are two different systems, and
a crash between them would either lose the event or double-publish it. The
transactional outbox pattern removes that hazard entirely (ADR-0002,
ADR-0003).

## Transactional outbox pattern

Every state-changing command handler in `incident-core`, in one DB
transaction:

1. Validates the guard for the transition.
2. Applies the state change (with optimistic-concurrency `WHERE version = …`).
3. Inserts one or more rows into `events` (the outbox), including a
   monotonic `sequence` (bigserial) and the full event payload.
4. Commits.

A single **outbox relay** process (or a small poller inside incident-core;
no separate service needed at this scale — see ADR-0003) then:

1. Reads `events` rows with `published_at IS NULL`, ordered by `sequence`.
2. `XADD`s each to the appropriate Redis Stream (`stream:incidents`,
   `stream:alerts`, etc.), keyed so that all events for the same
   `incident_id` land in the same stream partition/consumer-group shard.
3. Marks `published_at` on success.

If the relay crashes after `XADD` but before marking `published_at`, it
re-publishes on restart — **consumers must dedupe by `event_id`**, which is
why every event carries a stable UUID independent of its Redis Stream
message ID.

## Event envelope

```json
{
  "event_id": "uuid",
  "event_type": "IncidentStatusChanged",
  "schema_version": 1,
  "aggregate_type": "Incident",
  "aggregate_id": "uuid",
  "sequence": 10482,
  "occurred_at": "2026-09-24T10:15:00Z",
  "produced_by": "incident-core",
  "correlation_id": "uuid (= incident_id, for tracing)",
  "causation_id": "uuid (event_id or command_id that caused this)",
  "payload": { "...": "event-type-specific fields" }
}
```

## Event catalog (initial)

| Event | Emitted by | Payload highlights |
|---|---|---|
| `AlertReceived` | alert-ingestion (via command to incident-core) | alert id, fingerprint, source |
| `AlertLinked` | incident-core | alert id, incident id |
| `IncidentCreated` | incident-core | incident id, correlation key, initial severity |
| `IncidentStatusChanged` | incident-core | from, to, reason |
| `IncidentSeverityChanged` | incident-core | old, new |
| `InvestigationStarted` / `InvestigationCompleted` / `InvestigationFailed` | incident-core | investigation id, attempt number |
| `HypothesisProposed` / `HypothesisSelected` | incident-core | hypothesis id, evidence ids |
| `RemediationProposed` | incident-core | proposal id, action_catalog_id |
| `PolicyDecisionRecorded` | incident-core | decision, policy version, reasons |
| `ApprovalRequested` / `ApprovalDecided` | incident-core | approver, decision |
| `ExecutionStarted` / `ExecutionCompleted` / `ExecutionFailed` | incident-core | execution id, idempotency key |
| `VerificationStarted` / `VerificationCompleted` | incident-core | result, evidence ids |
| `IncidentClosed` | incident-core | resolution type |

## Ordering guarantees

- **Per-aggregate (per `incident_id`) ordering is guaranteed.** The outbox
  `sequence` is global and monotonic; the relay publishes in sequence
  order; the Redis Stream consumer group is sharded by `incident_id` (via a
  consistent hash on the stream key or a per-incident stream — see
  ADR-0003 for the chosen scheme) so a single incident's events are never
  processed out of order by a given consumer.
- **Cross-aggregate (global) ordering is explicitly not guaranteed** and
  nothing in the design needs it. Two different incidents' events may be
  processed in any relative order.

## Idempotency

- **Commands** (`alert-ingestion → incident-core`,
  `investigation-agent → incident-core`,
  `remediation-executor → incident-core`) each carry an `idempotency_key`
  supplied by the caller (e.g. `alert.fingerprint + debounce_bucket`,
  `investigation_id`, `execution_id + attempt`). `incident-core` keeps a
  `processed_commands (idempotency_key, result, processed_at)` table; a
  repeated command with a seen key returns the stored result without
  re-applying the transition. This is what makes retries after a timeout
  safe.
- **Bus consumers** (notification-service, eval-harness recorder) keep a
  `(consumer_name, event_id)` dedup table (or rely on Redis consumer-group
  `XACK` plus a short-lived dedupe cache) so at-least-once delivery cannot
  double-notify or double-record.
- **Executions** are the highest-stakes idempotency case: `executions.idempotency_key`
  is a unique constraint on `(remediation_proposal_id, attempt_number)`,
  and `remediation-executor` is required to accept an idempotency key as
  an argument to every underlying adapter call (e.g. Kubernetes resource
  version checks, CI job dedup keys) so that a retried execution command
  cannot run the action twice.

## What is explicitly NOT event-sourced

The `Incident` aggregate's current state is **not** rebuilt by replaying
events at read time — that's extra complexity with no payoff here, since
Postgres already holds current state transactionally consistent with the
event that describes the change. The `events` table is for audit, replay,
and downstream fan-out, not for reconstructing aggregate state on every
read. (Reassessed in ADR-0002.)
