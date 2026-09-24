# remediation-executor

Status: not implemented — design only. See
`docs/architecture/09-remediation-policy-boundaries.md`,
`docs/adr/0008-action-catalog-whitelist.md`,
`docs/architecture/13-security-boundaries.md`.

## Responsibility

The only component with write credentials to production systems. Executes
one `action_catalog` entry per invocation, dispatched only by
`incident-core` after a persisted `ALLOW` or approved decision. One adapter
module per catalog entry; scoped, short-lived credentials per action type;
idempotent by construction via `executions.idempotency_key` and the target
system's own idempotency primitives.

## Owns

Nothing durable. Reports execution results back to `incident-core` via
command.

## Explicitly cannot do

- Decide whether an action should run (that decision — policy + approval —
  already happened before this service is invoked; it re-validates
  parameters against the catalog schema as defense in depth, but does not
  re-decide policy).
- Accept dispatch from anything other than `incident-core`.

## Talks to

- Inbound: `incident-core` only.
- Outbound (scoped, allow-listed credentials only): Kubernetes API,
  CI/CD API, feature-flag API.
