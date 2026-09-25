-- Local development only. A separate, disposable database for Phase 6's
-- evaluation harness and replay verification (packages/evaluation). The
-- harness resets it before every run, so it must never be the main
-- database. Same schemas, roles and grants as 01-schemas-and-roles.sql
-- (roles are cluster-wide; schemas and grants are per database).
-- Idempotent: `make eval-db` re-runs it against an existing volume.

SELECT 'CREATE DATABASE incident_intelligence_eval'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'incident_intelligence_eval')\gexec

\connect incident_intelligence_eval

CREATE SCHEMA IF NOT EXISTS incident_core;
CREATE SCHEMA IF NOT EXISTS evidence;

GRANT CONNECT ON DATABASE incident_intelligence_eval TO incident_core_role, evidence_service_role;

GRANT USAGE, CREATE ON SCHEMA incident_core TO incident_core_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA incident_core GRANT ALL ON TABLES TO incident_core_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA incident_core GRANT ALL ON SEQUENCES TO incident_core_role;

GRANT USAGE, CREATE ON SCHEMA evidence TO evidence_service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence GRANT ALL ON TABLES TO evidence_service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence GRANT ALL ON SEQUENCES TO evidence_service_role;
