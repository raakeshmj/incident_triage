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
ActionCatalogEntry, active Policy version)`, with no LLM call and no
network I/O in its evaluation path. Every decision records the exact
`policy_version` used and is immutable once recorded. Policy is authored
as a small rule DSL (see `architecture/09-remediation-policy-boundaries.md`)
reviewed and merged like code, deployed via CI/CD, never runtime-editable
through an API.

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

## Consequences

Policy decisions are fully reproducible and testable without any live
model or external system — a pure unit-test suite can enumerate every
"must always deny" and "must always require approval" case and assert on
it directly (this is also how the eval harness's policy-safety gate works).
The cost: policy changes require a deploy rather than a runtime toggle,
which is an intentional friction for changes to a safety-critical system
(the kill switch, by contrast, is deliberately a fast runtime toggle for
emergencies — see ADR-0009).
