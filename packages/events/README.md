# packages/events

Event delivery mechanics -- how an event travels once it leaves the
outbox, as opposed to `packages/domain`, which owns what an event means.
See `docs/architecture/05-event-model.md`.

- `envelope.py` -- `OutboxEventEnvelope`, mirroring the architecture doc's
  event envelope shape.
- `publisher.py` -- the `EventPublisher` protocol `apps/worker` depends
  on, plus `LoggingEventPublisher` (default/test) and
  `RedisStreamEventPublisher` (a minimal, working `XADD` to a single
  stream -- deliberately no consumer groups or per-incident sharding yet;
  see the module docstring and `docs/review/critical-review.md`, "Event
  ordering").
