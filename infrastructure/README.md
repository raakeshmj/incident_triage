# infrastructure

Local infrastructure provisioning, referenced by the root
`docker-compose.yml`.

- `postgres/init/` -- SQL run once by Postgres's own
  `docker-entrypoint-initdb.d` mechanism on first container start.
  Creates the `incident_core` and `evidence` logical schemas and their
  respective least-privilege roles (ADR-0013) -- local-dev-only
  credentials, never used in a real deployment (see
  `docs/architecture/13-security-boundaries.md`).

Kubernetes/kind manifests are reserved for a later phase (Phase 1 has no
Kubernetes dependency at all -- see the Phase 1 brief's explicit
exclusions).
