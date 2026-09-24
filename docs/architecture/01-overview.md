# 01 — Overview

## Purpose

Incident Intelligence ingests production alerts, correlates them into
incidents, investigates using Claude as a reasoning layer over real
telemetry, produces an evidence-backed root-cause analysis (RCA), proposes
remediation, and — under strict deterministic policy and human approval —
executes and verifies bounded fixes. Everything is recorded for replay and
offline evaluation.

## Scope of this document set

This is the architecture and repository design produced *before* any
service is implemented. It fixes: component boundaries, the domain model,
the incident state machine, the event model, the database schema, the
agent/tool architecture, the evidence model, remediation/policy boundaries,
verification design, evaluation architecture, local dev architecture, and
security boundaries. A critical review and implementation order close out
the document set.

## Guiding principles

- **Determinism owns state; the LLM owns reasoning.** State transitions,
  policy decisions, and execution are plain code with unit tests. Claude
  proposes hypotheses and remediations as structured data that deterministic
  code validates before it can affect anything.
- **Every fact is a record, not an assertion.** If it isn't stored with a
  timestamp, a source, a query, and a content hash, it isn't evidence — it's
  the model's opinion, and the schema won't accept it as a citation.
- **Bounded, whitelisted actions only.** Remediation is never "run this
  command the model wrote." It is always "invoke catalog action X with
  validated parameters Y," where X was reviewed and merged by a human ahead
  of time.
- **Small number of services, clear trust boundaries.** We do not split
  into microservices for their own sake. We split where trust, blast
  radius, credentials, or scaling characteristics genuinely differ (see
  ADR-0001).
- **Replayable by design.** The event log, evidence store, and policy
  decisions are append-only and versioned specifically so that any incident
  can be replayed — for debugging, for audit, and for the eval harness.
- **Boring technology.** Postgres for durable state and transactions, Redis
  Streams for async fan-out, no bespoke consensus, no unnecessary queues,
  no framework magic. See the stack list in the task brief; deviations are
  called out explicitly in ADRs with a reason.

## What "production-grade" means here

- Every write path has an idempotency key.
- Every state transition has a single owner and is guarded by optimistic
  concurrency control.
- Every external call (Claude, Prometheus, Loki, GitHub, k8s) has a
  timeout, a retry policy, and a circuit breaker at the boundary that owns
  it.
- Every privileged action (remediation execution) runs with scoped,
  short-lived credentials and is independently auditable.
- Nothing about correctness depends on the LLM being well-behaved. The
  system must be safe even if Claude's output is wrong, malformed, or
  adversarially manipulated via prompt injection in log/alert content.

## High-level flow

```
Alert sources ──▶ alert-ingestion ──▶ incident-core (correlation, state machine)
                                            │
                                            ├─▶ investigation-agent (Claude)
                                            │        │
                                            │        └─▶ evidence-service ──▶ Prometheus / Loki / Tempo / Git / k8s / historical incidents
                                            │
                                            ├─▶ policy-engine (deterministic)
                                            │
                                            ├─▶ approval (human, via web-ui / Slack)
                                            │
                                            ├─▶ remediation-executor ──▶ k8s / CI / feature flags
                                            │
                                            └─▶ verification (re-checks evidence-service)

All state transitions ──▶ outbox events ──▶ Redis Streams ──▶ notification-service, timeline/audit, eval-harness (offline, replay)
```

See `02-component-boundaries.md` for the authoritative service list and
ownership table.
