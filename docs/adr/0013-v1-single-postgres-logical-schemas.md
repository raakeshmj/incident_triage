# ADR-0013: V1 uses a single PostgreSQL instance with logical schema separation

Status: Accepted

## Context

`02-component-boundaries.md` requires `incident-core` and `evidence-service`
to be the sole writers of their respective data, and the critical review
(`review/critical-review.md`, §1) flagged that this was only enforced by
code discipline, not by anything that would stop a bug (or a compromised
process) from writing across the boundary. Running two fully separate
Postgres instances from day one would enforce the boundary at the
infrastructure level, but it's real operational overhead — two instances
to provision, back up, monitor, and manage connections to — before there
is any actual scale or performance reason to pay for it.

## Decision

For V1 and local development, run a **single PostgreSQL instance** with
two logical schemas: `incident_core` (owned by `incident-core`) and
`evidence` (owned by `evidence-service`). Each service connects with its
own Postgres role, granted privileges only on its own schema (`GRANT ALL
ON SCHEMA incident_core TO incident_core_role`, with no grant to
`evidence`, and symmetrically for `evidence_service_role`). This gets the
database-level enforcement the critical review asked for — a bug in one
service's code cannot write the other's tables, because its role has no
grant to — without paying for two physical instances.

`evidence-service`'s `raw_response_ref` column already treats large raw
payloads as a pointer/reference rather than assuming they live inline in
its own schema, specifically so that a later move to object storage (S3-
compatible blob storage, say) or to a physically separate database instance
changes only where the referenced bytes live, not the evidence model
itself, the schema's table shapes, or any calling code's contract.

## Alternatives considered

- **Two separate Postgres instances from day one**: deferred, not
  rejected — the right choice once evidence payload volume, backup/restore
  cadence, or independent scaling needs actually diverge enough to justify
  it. Premature at V1, where it would mean twice the operational surface
  for a boundary that role-based grants already enforce.
- **One schema, no separation, rely entirely on code discipline**: rejected
  — this is the status quo the critical review specifically flagged as a
  gap; it does nothing to stop a bug from crossing the ownership boundary.
- **Row-level security instead of schema separation**: rejected as
  unnecessary complexity here — the ownership boundary in this system is
  along whole tables (one service's tables vs. another's), not rows within
  a shared table, so schema-level `GRANT`s are the right-sized mechanism.

## Consequences

Local development and V1 production both run one Postgres instance,
keeping operational overhead low while still getting a real,
database-enforced ownership boundary — not just a documented convention.
Migrating `evidence` to its own instance later is a deployment/connection-
string change plus a data migration, not a schema redesign, because the
schema was already designed as if it were a separate boundary. The
tradeoff being accepted now: both schemas share the same instance's
resource limits (connections, I/O, failure domain) until that migration
happens, so a runaway query or an outage in one schema's workload can
still affect the other's availability, even though it can't affect its
data integrity. This is judged acceptable at V1 scale and is exactly the
condition ("scale requires it") that should trigger revisiting this ADR.
