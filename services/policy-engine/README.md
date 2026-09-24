# policy-engine

Status: not implemented — design only. See
`docs/architecture/09-remediation-policy-boundaries.md`,
`docs/adr/0007-deterministic-versioned-policy-engine.md`.

## Responsibility

Deterministic, versioned evaluation of a `RemediationProposal` against the
active `Policy`: `ALLOW` / `DENY` / `REQUIRE_APPROVAL(+roles)`. Pure
function — no LLM call, no network I/O, no side effects. Includes the
global/per-service kill switch and remediation-rate limiting.

## Owns

Nothing durable itself; `incident-core` persists the `PolicyDecision` this
service computes. `policies` and `action_catalog` are reference data
deployed via CI/CD, not runtime-writable by this or any service.

## Talks to

- Inbound: `incident-core` (synchronous evaluation call) only.
- Outbound: nothing.
