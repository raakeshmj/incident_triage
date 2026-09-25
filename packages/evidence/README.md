# packages/evidence

evidence-service: the controlled boundary between telemetry backends and
anything that investigates an incident. See
`docs/architecture/08-evidence-model.md` (the model and its Phase 4
implementation) and ADR-0018 (the store design).

```
telemetry backend -> adapter -> EvidenceService -> EvidenceRecord + EvidenceRef -> tool (packages/tools)
```

- `service.py` -- `EvidenceService`. Every operation: resolve the incident's
  scope -> validate/bound parameters -> adapter query -> persist an
  immutable, content-hashed `EvidenceRecord` -> register the `EvidenceRef`
  with incident-core -> return a compact `EvidenceItem`. Plus
  `get_incident_evidence` (deterministic replay), `get_evidence`, `verify`.
- `scope.py` -- `IncidentScope` (services = incident's service + direct
  dependency neighbors from `infrastructure/evidence/service-catalog.json`;
  environment = the incident's; bounded windows), `IncidentGateway` (the
  incident-core read API + `RegisterEvidenceRefCommand`, never SQL).
- `adapters/` -- one per source, all read-only and allow-listed:
  `prometheus.py` (metric templates, window + baseline + current value,
  service health), `loki.py` (structured filters, bounded fetch, grouped
  representative lines, counts), `tempo.py` (trace by id -> summarized span
  tree; templated TraceQL search), `changes.py` (deployment + config
  registries), `git.py` (fixed-argv git log/numstat over catalog paths),
  `history.py` (deterministic similar-incident scoring).
- `models.py` -- `Observation` (adapter output), `EvidenceRecord` (persisted),
  `EvidenceItem` (returned), `Provenance`.
- `hashing.py` -- `sha256` over canonical JSON of the stored raw response.
- `limits.py` -- every window/result bound, enforced server-side.
- `sanitize.py` -- strips escapes/control characters from untrusted text.
- `db/`, `migrations/`, `repository.py` -- the `evidence` schema, owned by
  `evidence_service_role`; insert-only (a trigger rejects UPDATE/DELETE).
  Migrate with `alembic -n evidence upgrade head` (`make migrate` runs both).

This package never imports `packages.incident` (enforced by
`tests/unit/test_boundaries.py`) and never reads the environment -- the
composition root (`apps/evidence/dependencies.py`) builds its clients.
