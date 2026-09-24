# ADR-0012: Dynamic policy facts are passed explicitly via PolicyEvaluationContext

Status: Accepted

## Context

ADR-0007 requires `policy-engine` to be a pure, deterministic,
side-effect-free function. An earlier draft of the design satisfied that
in its stated signature — `(RemediationProposal, ActionCatalogEntry,
active Policy version) → PolicyDecision` — but its own example rules
contradicted it, referencing things like `incident.environment` and
`remediation_rate(incident.service, window="1h")` as if the engine could
just look them up. It can't, without either (a) giving `policy-engine`
database/Redis access, which breaks purity and adds a runtime dependency
to the most security-critical component in the system, or (b) making the
rules lie about what they condition on. Neither is acceptable, and the gap
between the stated contract and the example rules was a real internal
inconsistency in the design, not a stylistic issue — it meant "policy
decisions are replayable" wasn't actually true, because replaying a
decision would silently substitute *live* ambient state (today's
remediation rate, today's kill-switch setting) for the state that actually
existed at decision time.

## Decision

Introduce an explicit, typed `PolicyEvaluationContext` value object
containing every dynamic fact any policy rule is allowed to condition on:
incident environment, severity, service, the proposed action's blast-radius
tier, the trailing-window remediation count for the service, the incident's
attempt count so far, and both kill-switch flags (global and per-service).
`incident-core` — never `policy-engine` — builds this object immediately
before calling `evaluate(proposal, action_catalog_entry, policy,
policy_context)`, doing whatever database/Redis reads are needed to
populate it. `policy-engine`'s `evaluate()` then performs strictly zero
I/O; it only branches on the fields of the context object it was handed.

The exact `PolicyEvaluationContext` used for a given evaluation is
serialized and stored verbatim in `policy_decisions.policy_context`
(JSONB), in the same transaction as the decision, and is never updated
afterward. See `architecture/09-remediation-policy-boundaries.md` and
`architecture/06-database-design.md`.

## Alternatives considered

- **Give `policy-engine` direct read access to Postgres/Redis**: rejected
  — this is exactly the impurity the design is trying to avoid. It also
  adds an availability dependency (policy evaluation now requires the
  database to be reachable from a second service) and a second place that
  needs the same query logic as `incident-core` already has, with the two
  copies free to drift.
- **Keep the ambient-lookup rule syntax and treat it as pseudocode, not a
  literal contract**: rejected — a design document's example rules are
  read as the intended interface by whoever implements this; leaving the
  inconsistency in place would just relocate the bug from "caught in
  review" to "discovered during implementation," or worse, "discovered in
  an incident postmortem when a replayed decision didn't match the
  original."
- **Recompute the context at replay time instead of storing it**: rejected
  — this is the literal failure mode the whole ADR exists to prevent.
  Recomputing "the remediation rate" at replay time answers a different
  question ("what is it now") than the one that matters for audit ("what
  was it when this decision was made").

## Consequences

`policy-engine` is now genuinely a pure function, testable with plain
Python objects and no fixtures beyond a `PolicyEvaluationContext` literal
— which is also exactly what the eval harness's policy-safety suite needs
(`11-evaluation-architecture.md`). `incident-core` takes on the
responsibility of building this context correctly and completely: every
new dynamic fact a future policy rule needs must be added to the
`PolicyEvaluationContext` schema and the context-builder function, in a
reviewed change — it cannot simply be added as a new ambient lookup inside
a rule. This is accepted as the right friction: it keeps the set of facts
policy can ever depend on fully enumerable and auditable.
