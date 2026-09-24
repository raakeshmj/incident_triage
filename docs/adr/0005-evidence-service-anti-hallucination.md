# ADR-0005: All external facts flow through evidence-service, which persists before returning

Status: Accepted

## Context

An LLM-based investigator is only trustworthy if every factual claim it
makes can be checked against something real. The naive approach — give the
model direct API access to Prometheus/Loki/etc. — makes that impossible to
enforce: nothing stops the model from paraphrasing, misremembering, or
inventing what a query "showed" once the raw response is just tokens in
its context.

## Decision

No component other than `evidence-service` is allowed network access to
observability/Git/deployment/history systems. Every tool the
investigation agent can call proxies through `evidence-service`, which:
persists the query and the verbatim raw response as an immutable,
content-hashed record; assigns it an `evidence_id`; and only then returns
a (possibly summarized) view to the caller. `incident-core` subsequently
rejects any hypothesis or proposal citing an `evidence_id` that doesn't
exist. See `architecture/08-evidence-model.md`.

## Alternatives considered

- **Trust the model's citations without verification**: rejected outright
  — this is precisely the hallucination risk in question, and there is no
  reliable way to detect a fabricated citation after the fact if nothing
  forces citations to reference real stored records in the first place.
- **Log tool calls for audit but don't enforce citation validity**:
  rejected — audit-only logging tells you *after the fact* that the model
  hallucinated; it doesn't prevent a hallucinated RCA from being written,
  approved, or acted on before anyone reviews the log.

## Consequences

Every RCA is mechanically traceable to real queries against real systems,
which is what makes the eval harness's groundedness metric meaningful and
what makes an RCA defensible in a post-mortem review. The cost: an extra
service hop and storage cost for every tool call, and `evidence-service`
becomes a component that must be highly available during investigations
(mitigated by keeping it read-only and horizontally scalable, with no
state of its own beyond the evidence store).
