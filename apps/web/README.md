# web (Next.js + TypeScript)

Status: not implemented — design only. See
`docs/architecture/02-component-boundaries.md`,
`docs/architecture/13-security-boundaries.md` (RBAC).

## Responsibility

Human-facing UI: incident list/detail, RCA and evidence viewer, approval
flow, timeline view. Talks only to `incident-core`'s public read/command
API (BFF pattern) — never directly to any other service.

## Owns

Nothing durable. Per-viewer UI state only.

## Talks to

- `incident-core` public API only, authenticated, role-checked
  server-side by `incident-core` on every approval action.
