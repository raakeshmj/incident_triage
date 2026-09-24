# ADR-0002: Postgres as source of truth via transactional outbox, not full event sourcing

Status: Accepted

## Context

We need durable state for the Incident aggregate, an audit/replay log, and
async fan-out to consumers like notifications and the eval harness. Two
designs were on the table: (a) full event sourcing, where current state is
derived by replaying the event log, or (b) a conventional relational model
where current state lives in tables and an append-only event log is
produced alongside it as a side effect of each transaction.

## Decision

Use (b): Postgres tables hold current state directly, with optimistic
concurrency via a `version` column. Every state-changing transaction also
inserts into an `events` (outbox) table in the same commit. A relay process
publishes outbox rows to Redis Streams for async consumers. See
`architecture/05-event-model.md`.

## Alternatives considered

- **Full event sourcing** (state derived by folding events at read time,
  or via materialized projections): rejected for v1 — it adds real
  complexity (snapshotting, projection rebuilds, schema evolution of
  historical events) that buys nothing here, since we don't need temporal
  queries like "what did the aggregate look like at event N" for anything
  other than audit, which the outbox already provides by simply keeping
  every event forever next to the current-state tables.
- **Dual writes** (write to Postgres, then separately publish to Redis,
  no outbox): rejected — a crash between the two writes either loses the
  event or, if publish-then-write, can publish an event for a transaction
  that never committed. This is a well-known correctness hazard the
  outbox pattern specifically exists to remove.

## Consequences

Current-state queries are simple SQL against normal tables — cheap and
fast for the UI and the state machine's own guards. The event log is
"free" (a row insert in the same transaction) rather than the primary
mechanism, so replay/audit tooling reads `events` directly rather than
reconstructing state from it. The tradeoff: if a future requirement needs
true point-in-time aggregate reconstruction from events, that's a bigger
lift than if we'd event-sourced from day one — accepted, since nothing in
the current requirements needs it.
