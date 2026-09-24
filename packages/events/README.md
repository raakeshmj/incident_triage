# packages/events

Event delivery mechanics -- how an event travels once it leaves the
outbox, as opposed to `packages/domain`, which owns what an event means.
See `docs/architecture/05-event-model.md` and, for the Phase 2 transport
design, `docs/adr/0014-redis-streams-transport.md`.

- `envelope.py` -- `OutboxEventEnvelope`, mirroring the architecture doc's
  event envelope shape (includes the `producer` field as of Phase 2).
- `streams.py` -- stream naming/sharding topology: `stream:events:{0..N-1}`
  keyed by a consistent hash of `correlation_id`, `stream:events:dlq`, and
  consumer group/identity naming helpers.
- `publisher.py` -- `EventPublisher` protocol, `LoggingEventPublisher`
  (default/test), and `RedisStreamEventPublisher` (publishes into the
  sharded topology; raises on failure so the relay's own retry/backoff
  policy decides what to do, rather than swallowing errors here).
- `consumer.py` -- `RedisStreamConsumer`: the reusable consumer-group
  abstraction. Reclaims stale pending entries (`XAUTOCLAIM`), reads new
  entries, checks a caller-supplied `is_duplicate` predicate before
  invoking the handler, ACKs only after `mark_processed` succeeds, and
  dead-letters messages whose Redis-tracked delivery count exceeds
  `max_deliveries` (or that raise `PermanentProcessingError`). See its
  module docstring for the exact delivery-semantics contract.

Delivery is at-least-once on every hop, never exactly-once -- see
ADR-0014 and `docs/architecture/05-event-model.md`'s "Delivery semantics"
section. Consumer-side idempotency is enforced via a database ledger
(`packages.incident.repository.has_consumed_event` /
`mark_event_consumed`, backing `consumer.py`'s `is_duplicate`/
`mark_processed` parameters), not Redis's own bookkeeping alone.
