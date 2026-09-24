# ADR-0011: Idempotency keys and optimistic concurrency are mandatory at every write boundary

Status: Accepted

## Context

The system has many places where at-least-once delivery or concurrent
writers are possible by construction: retried commands after a timeout,
redelivered bus messages, two alerts correlating at once, verification
completing while a human is mid-approval, a crashed executor retrying an
in-flight action. Any of these, handled naively, can double-apply a state
change or double-execute a production action — the latter being
unacceptable.

## Decision

Every inbound command to `incident-core` carries a caller-supplied
idempotency key, checked against a `processed_commands` ledger before
applying. Every mutation of the `incidents` row uses optimistic
concurrency (`WHERE version = :expected`). `executions.idempotency_key` is
a database-level unique constraint, and executor adapters are required to
use the target system's own idempotency primitives (resource versions, CI
dedup keys) keyed by it. See `architecture/05-event-model.md` and
`architecture/06-database-design.md` for the specific mechanisms.

## Alternatives considered

- **Rely on "at most once" delivery assumptions**: rejected — nothing in
  the chosen stack (HTTP retries, Redis Streams consumer groups) actually
  guarantees at-most-once delivery; assuming it would be building on a
  false premise.
- **Application-level locking only (e.g. a mutex per incident in process
  memory)**: rejected — doesn't work across multiple instances of
  `incident-core` running behind a load balancer, which is required for
  availability.

## Consequences

Retries, redeliveries, and concurrent requests are safe by construction
and can be tested directly (send the same command twice, assert one
effect). The cost is upfront: every command handler must be written with
its idempotency key and concurrency check from the start, not bolted on
later — called out explicitly here so it's part of the definition of done
for every write path during implementation, not an afterthought.
