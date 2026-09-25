# apps/evidence

evidence-service's internal HTTP process (Phase 4). Internal only: its own
port (`make run-evidence`, default 8010), never behind the public edge
alert-ingestion sits on. It holds the evidence DB role and the read-only
telemetry URLs; nothing an agent can reach holds either.

| Route | Purpose |
|---|---|
| `GET /internal/v1/tools` | Tool definitions (name, description, JSON Schema) |
| `POST /internal/v1/incidents/{incident_id}/tools/{tool}` | Run one tool call (`{"arguments": {...}}`) -> `ToolSuccess` / `ToolFailure` |
| `GET /internal/v1/incidents/{incident_id}/evidence` | Every evidence record for the incident, in replay order, with provenance |
| `GET /internal/v1/evidence/{evidence_id}` | One full record, raw response included |
| `GET /internal/v1/evidence/{evidence_id}/verify` | Recompute the hash from storage; check incident-core's ref agrees |

There is no route that accepts a raw PromQL/LogQL/TraceQL query, a git
command, or SQL.

**V1 composition**: `dependencies.py` builds the `IncidentGateway` as an
in-process `IncidentCoreService` -- the same one-process, code-level-boundary
pattern `apps/api` uses for alert-ingestion. `packages/evidence` only sees the
gateway protocol, so splitting evidence-service into its own deployable turns
that into an HTTP client without touching the evidence code. See ADR-0018.

```bash
make run-evidence
curl -s localhost:8010/internal/v1/incidents/<id>/tools/get_metrics \
  -H 'Content-Type: application/json' -d '{"arguments": {"metric": "error_rate"}}'
curl -s localhost:8010/internal/v1/incidents/<id>/evidence
```
