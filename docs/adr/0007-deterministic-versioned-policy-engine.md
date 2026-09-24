# ADR-0007: Policy engine is deterministic, versioned, and has veto power over every remediation

Status: Accepted

## Context

Remediation is the highest-stakes action in the system — it changes
production. The decision of whether an action may run cannot depend on the
LLM's self-reported confidence or on any non-reproducible computation,
because policy decisions need to be explainable, auditable, and testable
in isolation (including adversarial testing — "does this proposal get
denied?" must be a stable, checkable fact).

## Decision

`policy-engine` is a pure function of `(RemediationProposal,
ActionCatalogEntry, active Policy version, PolicyEvaluationContext)`, with
no LLM call and no network I/O in its evaluation path — including no
database or Redis reads, and no queries against ambient incident state.
Every dynamic fact the rules need (incident environment/severity/service,
remediation rate, kill-switch state, etc.) is assembled by `incident-core`
into an explicit `PolicyEvaluationContext` *before* the call, and that
exact context is captured immutably alongside the decision (see ADR-0012
for the detailed design). Every decision records the exact `policy_version`
used and is immutable once recorded. Policy is authored as a small rule DSL
(see `architecture/09-remediation-policy-boundaries.md`) reviewed and
merged like code, deployed via CI/CD, never runtime-editable through an
API.

## Alternatives considered

- **Full OPA/Rego from day one**: deferred, not rejected — OPA is a
  reasonable evolution if the rule surface grows complex enough to need
  its full feature set (bundles, external data, etc.), but starting with a
  small in-repo Python rule DSL keeps the initial system's dependency
  surface smaller and the rules easier to unit test directly. Revisit if
  policy complexity outgrows this.
- **Let the model's confidence score influence the policy decision**:
  rejected — confidence is self-reported by the same model whose output
  we don't fully trust; using it as a safety-relevant input undermines the
  entire deterministic-policy premise. Policy conditions only on
  deterministic properties of the action and environment.
- **Runtime-editable policy via an admin API**: rejected — bypasses code
  review for changes to the most safety-critical logic in the system.
  Policy changes go through the same PR/CI/CD path as everything else.
- **Implicit ambient lookups inside `policy-engine`** (rules querying
  incident/environment/remediation-rate data directly at evaluation time):
  this was actually present in an earlier draft of this design — rule
  examples referenced `incident.environment` and
  `remediation_rate(incident.service, …)` as if they were free ambient
  lookups the engine could just make. Rejected on review: that would have
  made `policy-engine` not actually pure (it would need database/Redis
  access to evaluate) and not actually replayable from stored state alone
  (replaying a decision would silently use *today's* remediation rate
  instead of the rate at the time the decision was made). Corrected by
  introducing the explicit `PolicyEvaluationContext`, built and captured
  by `incident-core` before each call (ADR-0012).

## Consequences

Policy decisions are fully reproducible and testable without any live
model or external system — a pure unit-test suite can enumerate every
"must always deny" and "must always require approval" case and assert on
it directly (this is also how the eval harness's policy-safety gate works),
by constructing a `PolicyEvaluationContext` directly rather than needing a
live database. Every stored decision is independently replayable using its
own captured context, without needing the live system's current state to
agree with what it was at decision time.
The cost: policy changes require a deploy rather than a runtime toggle,
which is an intentional friction for changes to a safety-critical system
(the kill switch, by contrast, is deliberately a fast runtime toggle for
emergencies — see ADR-0009).
