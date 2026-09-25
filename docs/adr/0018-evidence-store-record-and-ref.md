# ADR-0018: Evidence store -- immutable records in `evidence`, references in `incident_core`

Status: Accepted

## Context

docs/architecture/08-evidence-model.md requires that every observation an
investigation can cite is persisted, content-hashed and immutable *before*
it is returned, and that incident-core can validate citations against it.
ADR-0013 separates the two services' data into schemas owned by distinct
roles with no cross-grants. Phase 4 has to decide where each part lives,
how immutability is enforced, what exactly is hashed, and how the two
sides stay consistent without a shared transaction.

## Decision

1. **Record** (`evidence.evidence_records`, owned by `evidence_service_role`):
   the full observation -- query spec, source reference, source and
   collection timestamps, requester, bounded raw response (inline JSONB for
   v1), normalized payload, summary, `content_hash`. Insert-only, enforced
   by a `BEFORE UPDATE OR DELETE` trigger that raises. A retried query
   writes a new record.
2. **Reference** (`incident_core.evidence_refs`, owned by `incident_core_role`):
   id, incident, optional investigation, type, source, `content_hash`,
   collected_at. Written only by incident-core, through a
   `RegisterEvidenceRefCommand` idempotent on `evidence_id`; re-registering
   an id with different content or incident is rejected. Same trigger-based
   immutability.
3. **Hash**: `sha256` over canonical JSON (sorted keys, compact) of the raw
   response *as persisted* -- after NUL stripping and a JSON round trip --
   so the hash is recomputable from storage alone (`EvidenceService.verify`).
4. **Order**: record first, then ref. A ref-registration failure leaves an
   unreferenced record, which is never returned and so can never be cited;
   it's visible via the `evidence.ref_registration_failed` metric/log.
5. **Gateway**: evidence-service reaches incident-core only through the
   `IncidentGateway` protocol (read API + the one command). V1 composes it
   in-process (`apps/evidence/dependencies.py`), the same pattern `apps/api`
   uses for alert-ingestion; a split deployment makes it an HTTP client.

## Deviations from the documented sketch

- `evidence_refs.investigation_id` is nullable and has no FK yet, and the
  table carries a required `incident_id` FK: `investigations` doesn't exist
  until the agent (Phase 5). The FK is added with that table.
- Evidence type names follow the Phase 4 brief (`deployment`, `git_change`,
  `configuration`, `incident_history`); 08-evidence-model.md maps them.
- Raw payloads are inline JSONB, not object storage -- `raw_response` stays a
  single column so moving it behind a reference later changes storage, not
  the model (as 06-database-design.md already anticipates).

## Consequences

- Neither service can alter the other's rows (grants), and neither can alter
  its own evidence rows through normal statements (triggers). The owning
  role can still drop a trigger; that's a migration-review concern, not
  something application code can do by accident.
- TRUNCATE is not blocked (statement-level) -- test cleanup uses it; no
  application code path does.
