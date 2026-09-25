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
  "occurred_at": "2026-09-24T10:15:00Z",
  "correlation_id": "uuid (= incident_id, for tracing)",
  "causation_id": "uuid (event_id or command_id that caused this)",
  "producer": "incident-core",
  "payload": { "...": "event-type-specific fields" }
}
```

(`sequence` lives on the outbox row, not the envelope itself — it's an
artifact of Postgres ordering, not something a consumer needs; the field
was renamed from an earlier `produced_by` to `producer` for brevity when
implemented — see `packages/events/envelope.py`.)

## Commands vs. domain events — naming

Easy to conflate, so named to keep them apart: `AlertReceivedCommand` (sent
by `alert-ingestion` to `incident-core`) is a **command** — a request that
`incident-core` may accept or reject, carrying an idempotency key.
`AlertReceived` (no `Command` suffix, emitted by `incident-core`) is the
**domain event** written to the outbox once that command has been accepted
and the `Alert` persisted, in the same transaction. Every domain event in
the catalog below is produced by whichever service owns the aggregate it
describes — for `Alert`, that is always `incident-core`, never
`alert-ingestion`, even though `alert-ingestion` is what triggered it via
its command. The same pattern holds for `InvestigationCompleted`/
`InvestigationFailed` (triggered by a command from `investigation-agent`,
but the domain event is `incident-core`'s to emit) and `ExecutionCompleted`/
`ExecutionFailed` (triggered by a command from `remediation-executor`,
domain event owned by `incident-core`).

## Event catalog (initial)

| Event | Emitted by | Payload highlights |
|---|---|---|
| `AlertReceived` | **incident-core** (in the same transaction that persists the `Alert` and runs correlation) | alert id, fingerprint, source |
| `AlertCorrelated` | incident-core | alert id, incident id, matched signals, score (Phase 2 — see ADR-0015; renamed from the Phase 1 placeholder `AlertLinked` now that the correlation decision is explainable and worth carrying in the event itself) |
| `IncidentCreated` | incident-core | incident id, correlation key, initial severity, best candidate score + matched signals considered (Phase 2 addition — explains *why* no correlation happened, not just that it didn't) |
| `IncidentStatusChanged` | incident-core | from, to, reason, version (Phase 4: emitted for `TRIAGING -> CANCELLED` with reason `all_linked_alerts_resolved`) |
| `AlertResolved` | incident-core | alert id, incident id, firing alerts remaining (Phase 4, ADR-0019) |
| `EvidenceRefRegistered` | incident-core | evidence id, incident id, type, source, content hash (Phase 4, ADR-0018) |
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
  order. As of Phase 2 (ADR-0014), the Redis side is a fixed number of
  streams (`stream:events:0` .. `stream:events:{N-1}`, `packages/events/streams.py`),
  and every event is sharded by a consistent hash of `correlation_id`
  (which we always set to the owning incident's id) — so a single
  incident's events always land on the same shard stream and are read in
  emission order by whichever consumer owns that shard.
- **Cross-aggregate (global) ordering is explicitly not guaranteed** and
  nothing in the design needs it. Two different incidents' events may be
  processed in any relative order, including across different shards.

## Delivery semantics (Phase 2, ADR-0014)

Stated plainly because it's easy to accidentally assume otherwise:
**both hops — the outbox relay (Postgres → Redis) and every consumer
(Redis → handler) — are at-least-once. Neither hop, nor the combination,
is exactly-once, anywhere in this design.**

- The relay retries a failed publish a bounded number of times within one
  pass (`outbox_max_publish_attempts`, linear backoff), then leaves the
  event unpublished for the next pass — unbounded retries *across* passes,
  since there's no safe "give up" state for an internally-generated event
  that must eventually reach the stream. See `apps/worker/main.py`'s
  module docstring for the three crash points and what happens at each.
- A consumer only ACKs a message after its handler succeeds *and*
  `mark_processed` has durably recorded it (a database write, not a Redis
  one — see `consumed_events` in `06-database-design.md`). A crash between
  those two things causes Redis to redeliver the message once it's been
  idle past `claim_min_idle_ms`.
- Poison messages (delivery count past `consumer_max_deliveries`) are
  moved to `stream:events:dlq` with the original `event_id` preserved in
  the payload, and ACKed off the source stream so they stop being
  redelivered there.
- **Why exactly-once isn't attempted**: it would require a distributed
  transaction spanning Postgres and Redis (for the relay) and Redis and
  Postgres again (for the consumer), which neither system provides and
  which this design deliberately does not try to fake. Correctness instead
  comes from every consumer being idempotent by construction (checked
  against a database ledger keyed by the event's own `event_id`, never
  Redis's transport-specific message id) — see "Idempotency" below.

## Idempotency

- **Commands** (`alert-ingestion → incident-core`,
  `investigation-agent → incident-core`,
  `remediation-executor → incident-core`) each carry a caller-supplied
  `idempotency_key`. `incident-core` keeps a `processed_commands` table
  keyed by the **composite** `(command_type, idempotency_key)` — not the
  idempotency key alone — so a key can never collide across different
  command types (nothing otherwise stops two unrelated commands from
  independently choosing to key off the same UUID, e.g. an
  `investigation_id` and an `execution_id` that happen to coincide). A
  repeated command with a seen `(command_type, idempotency_key)` pair
  returns the stored result without re-applying the transition. This is
  what makes retries after a timeout safe.

  Every command handler follows the same shape: begin transaction → look
  up `(command_type, idempotency_key)` → if found, return the stored
  result and stop → otherwise apply the state change, insert the outbox
  event(s), insert the `processed_commands` row → commit. Concretely:

  | Command type | Idempotency key |
  |---|---|
  | `AlertReceivedCommand` | source-provided `external_id` when present; otherwise a content hash of the normalized payload plus a debounce time bucket |
  | `InvestigationCompletedCommand` / `InvestigationFailedCommand` | `investigation_id` |
  | `ApprovalDecisionCommand` | `approval_id` |
  | `ExecutionCompletedCommand` / `ExecutionFailedCommand` | `execution_id` (attempt-scoped via `executions.idempotency_key`, see `06-database-design.md`) |
- **Bus consumers** keep a `(consumer_name, event_id)` dedup table so
  at-least-once delivery cannot double-process. Implemented in Phase 2 as
  `consumed_events` (`06-database-design.md`), checked via
  `RedisStreamConsumer`'s injected `is_duplicate`/`mark_processed`
  collaborators (`packages/events/consumer.py`) — not an in-memory cache,
  which would not survive a consumer restart, and not reliance on Redis
  `XACK` alone, which only prevents double-delivery *within* a consumer
  group's own bookkeeping, not across a relay-side duplicate publish or a
  consumer restart against an already-processed message. See ADR-0014.
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

## Phase 5: investigation events

Written by incident-core in the same transaction as the state change, via
the outbox (aggregate type `Investigation`, aggregate id = investigation id):

| Event | When | Payload |
|---|---|---|
| `InvestigationStarted` | `request_investigation` (incident `TRIAGING → INVESTIGATING`, alongside `IncidentStatusChanged`) | `investigation_id`, `incident_id`, `attempt_number`, `model_provider`, `model_name` |
| `InvestigationCompleted` | conclusion accepted (incident → `RCA_READY`) | `investigation_id`, `incident_id`, `selected_hypothesis_id`, `rca_report_id` |
| `InvestigationFailed` | escalated or failed (incident → `ESCALATED`) | `investigation_id`, `incident_id`, `outcome` (`ESCALATED`/`FAILED`), `reason_code` |

The investigation worker consumes `InvestigationStarted` in consumer group
`cg:investigation-worker` with the `consumed_events` ledger; a redelivered
event finds the investigation already claimed or finished and does nothing.
The event is the fast path only: the worker's resume sweep picks up any
investigation whose event was lost or whose worker died.
