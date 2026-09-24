# policy-engine

Status: not implemented — design only. See
`docs/architecture/09-remediation-policy-boundaries.md`,
`docs/adr/0007-deterministic-versioned-policy-engine.md`,
`docs/adr/0012-policy-evaluation-context.md`.

## Responsibility

Deterministic, versioned evaluation:
`evaluate(proposal, action_catalog_entry, policy, policy_context) ->
PolicyDecision` (`ALLOW` / `DENY` / `REQUIRE_APPROVAL(+roles)`). A true
pure function — no LLM call, no network I/O, no database or Redis reads,
no side effects. Every dynamic fact it needs (environment, remediation
rate, kill-switch state, blast-radius tier, …) arrives pre-computed in
`policy_context`, built by `incident-core` immediately before the call —
this service never looks any of that up itself.

## Owns

Nothing durable itself; `incident-core` persists the `PolicyDecision` this
service computes. `policies` and `action_catalog` are reference data
deployed via CI/CD, not runtime-writable by this or any service.

## Talks to

- Inbound: `incident-core` (synchronous evaluation call) only.
- Outbound: nothing.
