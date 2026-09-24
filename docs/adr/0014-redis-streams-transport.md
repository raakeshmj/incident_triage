# ADR-0014: Redis Streams transport — sharding, consumer groups, dead-letter, delivery semantics

Status: Accepted

## Context

Phase 1 shipped a single flat Redis Stream with no consumer groups, no
sharding, and no consumer-side durability story — explicitly deferred, per
its own docstrings, until something actually needed to consume the stream
reliably. `docs/review/critical-review.md` (§3, "Event ordering") flagged
the open question directly: Redis Streams has no native partitioning, so
per-incident ordering across multiple consumers needs a concrete scheme,
and none had been chosen. Phase 2 needs a real, durable consumer (the
metrics/audit consumer), which forces the question.

## Decision

**Sharding.** A fixed number of streams (`DEFAULT_SHARD_COUNT = 8`),
named `stream:events:{shard}`. Every event's shard is
`hash(correlation_id or aggregate_id) % shard_count`
(`packages/events/streams.py`). Since every domain event we emit sets
`correlation_id` to the owning incident's id, every event for one incident
always lands on the same shard stream, and a consumer reading that stream
sequentially sees that incident's events in emission order. Cross-incident
ordering across different shards is not guaranteed and is not needed — the
same position `05-event-model.md` already took for the outbox itself.

**Consumer groups.** One Redis consumer group per logical consumer
*purpose* (`cg:{purpose}`, e.g. `cg:metrics-consumer`), created against
every shard stream. Each running process is one consumer *identity*
within that group (`hostname:pid` — `consumer_identity()`), which is what
Redis uses to track per-consumer pending entries for crash recovery.

**Dead-letter stream.** One shared stream, `stream:events:dlq`, regardless
of which shard a poison message came from. No ordering requirement across
dead letters; a single stream is simpler to monitor and drain than one per
shard.

**Delivery semantics — stated explicitly, not implied:**

- The outbox relay (Postgres → Redis) is at-least-once. A crash between a
  successful `XADD` and the Postgres commit that marks the event published
  produces a genuine duplicate on the stream. This is not prevented — doing
  so would require an atomic cross-system transaction that doesn't exist —
  it is handled downstream.
- The consumer side (Redis → handler) is also at-least-once:
  `RedisStreamConsumer` only ACKs after the handler succeeds and
  `mark_processed` has durably recorded it, so a crash between "handler
  succeeded" and "ACK sent" causes Redis to redeliver the message.
- **Exactly-once is never claimed anywhere in this design.** Both hops are
  at-least-once; correctness comes from idempotent processing at the
  consumer (ADR handled in `consumed_events`, see below), not from
  eliminating duplicates in transit.
- Poison detection uses Redis's own delivery counter (`XPENDING`'s
  `times_delivered`), not a counter this code keeps itself — that counter
  survives a consumer crash and a new process taking over the same
  consumer group, whereas an in-memory counter would not. Once it exceeds
  `max_deliveries`, the message moves to the dead-letter stream with its
  original `event_id` preserved in the payload, and is ACKed off the
  original stream.

**Consumer-side idempotency is a database ledger, not a Redis fact.**
`consumed_events(consumer_name, event_id)` — see ADR-0015's sibling
decision in `06-database-design.md` — is checked before a handler runs and
written after it succeeds, in the same spirit as `processed_commands`
(ADR-0011). This is what makes the accepted at-least-once duplication
above harmless: a handler is never invoked twice for the same `event_id`
by a given consumer, even though the message itself can arrive twice.

## Alternatives considered

- **Kafka**: explicitly out of scope per the Phase 2 brief and consistent
  with ADR-0003's original reasoning — this system's throughput doesn't
  yet justify the operational cost of a second distributed log.
- **One stream per incident** (rather than a fixed shard count): rejected
  — unbounded stream count with no natural reaping point while an incident
  is open, and Redis Streams don't offer cheap "does this stream still
  matter" introspection at scale. A fixed shard count is bounded and
  simple to reason about.
- **Consumer-group-native dedup only** (rely on the fact that a group only
  delivers a given message to one consumer at a time): rejected as
  insufficient — that property holds only until a crash-after-success/
  before-ack, or a relay-side duplicate publish, both of which are
  accepted realities of at-least-once delivery on *both* hops. A database
  ledger is required regardless of how carefully Redis's own guarantees are
  used.

## Consequences

Per-incident ordering is real and testable without needing Kafka. Adding a
new durable consumer means writing a handler and calling
`RedisStreamConsumer` against `all_stream_names()` — the sharding,
grouping, and DLQ mechanics are already handled. The cost: any process
that wants to read "all events" must iterate every shard stream itself (no
single stream holds a global order), and a consumer that reads all shards
in one thread must keep its `XREADGROUP` block time short per shard or an
idle shard stalls the whole cycle (see `apps/worker/consumer_main.py`'s
`block_ms` handling) — a real operational detail this ADR flags explicitly
so it isn't rediscovered the hard way later.
