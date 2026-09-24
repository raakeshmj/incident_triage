-- Local development only. Provisions the logical-schema separation from
-- ADR-0013: `incident_core` (owned by incident-core) and `evidence`
-- (owned by evidence-service — no tables yet in Phase 1, created here so
-- the ownership boundary exists from day one instead of being bolted on
-- later). Real deployments manage credentials via a secrets manager, not
-- a plaintext init script (see docs/architecture/13-security-boundaries.md).

CREATE SCHEMA IF NOT EXISTS incident_core;
CREATE SCHEMA IF NOT EXISTS evidence;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'incident_core_role') THEN
        CREATE ROLE incident_core_role LOGIN PASSWORD 'incident_core_dev_password';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'evidence_service_role') THEN
        CREATE ROLE evidence_service_role LOGIN PASSWORD 'evidence_service_dev_password';
    END IF;
END
$$;

-- incident_core_role owns and can create objects in its own schema only.
GRANT USAGE, CREATE ON SCHEMA incident_core TO incident_core_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA incident_core GRANT ALL ON TABLES TO incident_core_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA incident_core GRANT ALL ON SEQUENCES TO incident_core_role;

-- evidence_service_role owns and can create objects in its own schema only.
GRANT USAGE, CREATE ON SCHEMA evidence TO evidence_service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence GRANT ALL ON TABLES TO evidence_service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence GRANT ALL ON SEQUENCES TO evidence_service_role;

-- No cross-grants in either direction: incident_core_role has nothing on
-- `evidence`, evidence_service_role has nothing on `incident_core`. This
-- is the database-level enforcement of single-writer ownership from
-- ADR-0013 -- a bug in one service's code cannot write the other's
-- tables, because its role has no grant to do so.
