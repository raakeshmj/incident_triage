# Incident Intelligence — Design Documentation

Status: **Architecture foundation, pre-implementation.**

## Reading order

1. [architecture/01-overview.md](architecture/01-overview.md) — system purpose, scope, guiding principles
2. [architecture/02-component-boundaries.md](architecture/02-component-boundaries.md) — services, ownership, APIs
3. [architecture/03-domain-model.md](architecture/03-domain-model.md) — entities, aggregates, invariants
4. [architecture/04-incident-state-machine.md](architecture/04-incident-state-machine.md) — states, transitions, guards
5. [architecture/05-event-model.md](architecture/05-event-model.md) — event envelope, bus, ordering, idempotency
6. [architecture/06-database-design.md](architecture/06-database-design.md) — schema, constraints, concurrency
7. [architecture/07-agent-tool-architecture.md](architecture/07-agent-tool-architecture.md) — how Claude is used safely
8. [architecture/08-evidence-model.md](architecture/08-evidence-model.md) — anti-hallucination design
9. [architecture/09-remediation-policy-boundaries.md](architecture/09-remediation-policy-boundaries.md) — policy engine, action catalog, approvals
10. [architecture/10-verification-design.md](architecture/10-verification-design.md) — did the fix work
11. [architecture/11-evaluation-architecture.md](architecture/11-evaluation-architecture.md) — offline eval / replay harness
12. [architecture/12-local-development.md](architecture/12-local-development.md) — dev environment, fixtures
13. [architecture/13-security-boundaries.md](architecture/13-security-boundaries.md) — trust zones, RBAC, secrets

Decisions: [adr/](adr/) — one ADR per significant, hard-to-reverse choice.

Self-critique: [review/critical-review.md](review/critical-review.md) — state
ownership, races, ordering, idempotency, unsafe permissions, hallucination,
observability gaps, eval gaps, circular dependencies.

Build sequence: [implementation-order.md](implementation-order.md).

## Non-negotiable design invariants

These are referenced throughout the docs and enforced by architecture, not
convention:

1. **Single writer per aggregate.** `incident-core` is the only process
   permitted to write `alerts`, `incidents`, `investigations`,
   `hypotheses`, `remediation_proposals`, `policy_decisions`, `approvals`,
   `executions`, `verifications`. Every other service sends it a command;
   it decides. This is enforced at the database level, not just in code —
   each service's Postgres role is scoped to only its own logical schema
   (see `architecture/06-database-design.md` and ADR-0013).
2. **The LLM never calls a tool that mutates production state.** Claude's
   only outputs are structured data (hypotheses, evidence citations, a
   remediation *proposal* referencing a catalog action by ID). It cannot
   invoke `remediation-executor`, cannot write to the database, and cannot
   skip the policy engine.
3. **No evidence without provenance.** Any fact used in a hypothesis or RCA
   must resolve to a stored, content-hashed, replayable record of a real
   query against a real system, captured *before* it reaches the model's
   context on the way back out. Free-text claims from the model that are
   not backed by an evidence ID are rejected at the schema boundary.
4. **Policy is deterministic and versioned.** The same proposal, the same
   `action_catalog` entry, the same policy version, and the same
   `PolicyEvaluationContext` always yield the same decision.
   `policy-engine` performs no I/O of its own — every dynamic fact it
   needs (environment, remediation rate, kill-switch state, …) is built by
   `incident-core` and passed in explicitly, then captured immutably
   alongside the decision for replay. No LLM call anywhere in the policy
   path.
5. **Approval defaults to required.** A remediation only skips human
   approval if a versioned policy explicitly allow-lists it for the given
   blast-radius tier and environment. Timeouts deny or escalate — they
   never auto-approve.
6. **Idempotency everywhere state changes.** Every command carries an
   idempotency key scoped by command type (`(command_type,
   idempotency_key)`), every event carries a stable `event_id`, and every
   execution carries a unique `idempotency_key`; retries and redeliveries
   must be safe by construction.
