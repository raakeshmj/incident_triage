# apps/worker

The transactional outbox relay. Polls `incident_core.outbox_events` for
rows with `published_at IS NULL`, publishes each to Redis (a single
stream, `XADD`, no consumer groups yet), and marks it published.

See `docs/architecture/05-event-model.md` ("Transactional outbox
pattern") and `packages/events/publisher.py`'s docstring for what's
deliberately not built yet (per-incident sharding, consumer groups,
replay/dedup on the consumer side) -- those are open design decisions
tracked in `docs/review/critical-review.md`, not oversights.

## Running

`make run-worker` (requires `make infra-up` and `make migrate` first).
