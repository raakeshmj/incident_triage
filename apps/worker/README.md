# apps/worker

Three entrypoints:

- **`main.py`** — the transactional outbox relay. Polls
  `incident_core.outbox_events` for rows with `published_at IS NULL`,
  publishes each to its sharded Redis Stream (retrying with backoff on
  failure, recording `publish_attempts`/`last_publish_error` for
  diagnosis), and marks it published only after a successful publish. See
  its module docstring for the exact crash-safety guarantees (three named
  crash points and what happens at each) and
  `docs/architecture/05-event-model.md`'s "Delivery semantics" section.
- **`consumer_main.py`** — a concrete, durable event consumer built on
  `packages.events.consumer.RedisStreamConsumer`. Records observability
  metrics (correlation decisions, incident creation rate) for every event
  it sees; never mutates Incident/Alert state — Postgres remains
  authoritative regardless of what any consumer does. Reads every shard
  stream in one process with a short per-shard block time (see the
  module's `build_consumers` docstring for why).

- **`investigation_main.py`** (Phase 5) — the investigation worker. Each
  loop: the debounce scheduler (`TRIAGING` incidents past
  `INVESTIGATION_DEBOUNCE_SECONDS` with a firing alert →
  `request_investigation`), the `InvestigationStarted` consumer (group
  `cg:investigation-worker`, `consumed_events` dedup) that runs the engine,
  and a resume sweep for investigations whose lease expired (crashed
  worker, lost event). Calls the model configured by `INVESTIGATION_MODEL`.
  See `docs/architecture/15-investigation-engine.md`.

See `docs/adr/0014-redis-streams-transport.md` for the stream
sharding/consumer-group/dead-letter design both entrypoints share.

## Running

```
make run-worker      # the outbox relay
make run-consumer    # the metrics/audit consumer
make run-investigation-worker   # Phase 5 investigations (needs ANTHROPIC_API_KEY)
```

Both require `make infra-up` and `make migrate` first.
