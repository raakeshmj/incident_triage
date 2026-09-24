# ADR-0003: Redis Streams for async fan-out only, sharded by incident_id

Status: Accepted

## Context

We need to notify humans, feed the eval harness's recording pipeline, and
warm timeline read models without making those consumers part of the
synchronous state-transition transaction (which would couple their uptime
and latency to the core state machine's).

## Decision

Use Redis Streams, populated by the outbox relay
(`architecture/05-event-model.md`), as the async fan-out bus. Each event's
routing key includes `incident_id`; consumer groups are structured so that
all events for a given incident are processed in order by a given consumer
(via per-incident-hashed stream keys or partition assignment), guaranteeing
per-aggregate ordering without requiring global ordering. Consumers use
Redis consumer groups (`XREADGROUP`/`XACK`) for at-least-once delivery and
crash recovery via the pending-entries list, and dedupe by the event's own
`event_id` (not the Redis message ID) since redelivery is possible.

## Alternatives considered

- **Kafka**: rejected for this stage — meaningfully more operational
  overhead (brokers, partitions, ZooKeeper/KRaft) than this system's
  current fan-out volume justifies. Revisit if consumer count or throughput
  grows enough that Redis Streams' single-node durability model becomes
  the bottleneck.
- **Postgres LISTEN/NOTIFY**: rejected — no persistence/replay for
  disconnected consumers, and no consumer-group semantics; fine for cheap
  pub/sub, not for "the eval harness must be able to reconstruct exactly
  what was published."
- **Redis Streams as the source of truth** (skip Postgres events table,
  treat the stream as canonical): rejected — Redis's durability guarantees
  are weaker than Postgres's, and we specifically want the audit log to
  live in the same transactional store as the state it describes (ADR-0002).

## Consequences

Consumers can be down and catch up later (bounded by stream retention,
configured generously). Ordering is guaranteed per incident, which is all
any consumer actually needs. Operational footprint stays small (Redis is
already in the stack for other uses). The relay process itself is a
single point that must be monitored (it's simple and stateless — restart
recovers by re-scanning `published_at IS NULL`).
