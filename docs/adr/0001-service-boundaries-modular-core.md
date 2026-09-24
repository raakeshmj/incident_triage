# ADR-0001: Service boundaries drawn by trust/credential/scaling, not by domain noun

Status: Accepted

## Context

A naive design turns every noun in the domain model (correlation,
orchestration, approvals, timeline) into its own microservice. That
multiplies distributed-transaction problems for state that must be
strongly consistent (an incident's status, its approvals, its execution
record) without buying any real isolation, since those all need the same
trust level and the same database anyway.

## Decision

Draw service boundaries only where trust, credential scope, or scaling
characteristics genuinely differ:

- `incident-core`: sole writer of the Incident aggregate and everything
  transactionally coupled to it (correlation, state machine, approvals,
  timeline read model). One database, one set of transactions.
- `alert-ingestion`: separate because it's the public-facing trust
  boundary with different auth/rate-limiting needs.
- `investigation-agent`: separate because it's the only component that
  calls Claude, with very different latency/cost/scaling behavior, and
  because isolating it limits the blast radius of a bad model response to
  "a rejected command," not "corrupted core state."
- `evidence-service`: separate because it's the only component with
  read-only egress to observability/Git systems, and centralizing that is
  what makes the anti-hallucination evidence model enforceable.
- `policy-engine`: separate (or at minimum a clearly isolated library)
  because it's security-critical and must be independently testable with
  no dependency on the LLM or the database.
- `remediation-executor`: separate because it's the only component with
  write credentials to production systems — the highest-value target to
  isolate.

## Alternatives considered

- **One service per domain entity** (correlation-service,
  orchestrator-service, approval-service, timeline-service, …): rejected —
  forces distributed transactions across what is really one consistency
  boundary, for no trust or scaling benefit.
- **One monolith for everything, including the LLM call**: rejected —
  couples the highest-variance, least-trusted component (the model call)
  to the same failure domain and deploy cadence as core state management,
  and makes it impossible to give the model call a distinct, narrower
  network egress policy.

## Consequences

Fewer services to operate than a "microservice per noun" design; the
Incident aggregate's consistency is easy to reason about (single database,
single writer). The cost: `incident-core` is the biggest single service
and needs care to keep its internal modules (correlation, state machine,
approvals, timeline) cleanly separated in code even though they share a
process and database, so a future split remains possible if it's ever
justified.
