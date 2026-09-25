# 08 — Evidence Model

**Status**: implemented in Phase 4 -- see "Phase 4 implementation" at the end
of this document and ADR-0018.

## The problem this exists to solve

An LLM can state anything fluently, including things that are false or
unverifiable. If "evidence" in an RCA is just the model's paraphrase of
what it thinks a dashboard showed, the RCA is unauditable and untrustable
the moment the model is wrong — and there is no way to tell from the
report alone whether it's wrong. The fix is architectural, not a prompting
technique: **the model is structurally incapable of citing evidence that
isn't already a durable, independently-fetched record.**

## How that's enforced

1. `investigation-agent` never queries Prometheus/Loki/Tempo/Git/k8s/history
   directly. Every tool call goes to `evidence-service`.
2. `evidence-service`, on every call:
   - Executes the real query against the real backing system.
   - Persists the request (query spec) and the raw response **verbatim**
     as an immutable `evidence` record, computing `content_hash =
     sha256(raw_response)`, **before** returning anything to the caller.
   - Returns the caller a summarized/truncated view **plus** the
     `evidence_id` and `content_hash` — the model is expected to cite the
     ID, and the full raw record remains available for a human (or the
     eval harness) to inspect later.
3. `incident-core` rejects (see `07-agent-tool-architecture.md`) any
   hypothesis or proposal citing an `evidence_id` that doesn't exist or
   doesn't belong to the current investigation. There is no code path by
   which a citation can be accepted without a backing record.
4. Evidence is **immutable and content-addressed** once written — no
   service ever updates an evidence row. If the same query is run again
   (e.g. a retry), it produces a new evidence record with a new
   `collected_at`; the old one is untouched. This makes evidence safely
   replayable and makes staleness explicit rather than silently
   overwritten.

## Evidence types

| Type | Source | Query shape | Notes |
|---|---|---|---|
| `metric` | Prometheus | Allow-listed PromQL templates + parameters (not arbitrary PromQL) | Bounds the blast radius of a bad query; templates cover the alert's own metric plus standard RED/USE queries for the service |
| `log` | Loki | LogQL with mandatory service/time scoping | Truncated to N lines / M KB in the raw record too — a "raw dump" is still bounded |
| `trace` | Tracing backend (Tempo) | Trace ID or service+time window | Summarized span tree stored, not every attribute |
| `deploy` | CI/CD or deployment system API | Service + time range | Who, when, what changed (version/commit refs) |
| `git_diff` | Git provider (GitHub) | Commit range or PR reference | Diff + commit metadata, read-only token scoped to specific repos |
| `config_change` | Config store / feature-flag system | Service + time range | Before/after values |
| `historical_incident` | incident-core read API | Free-text/service query over past `rca_reports` | Returns past incidents' *evidence-backed* summaries, not raw historical evidence |

## Evidence record shape

```json
{
  "id": "uuid",
  "investigation_id": "uuid",
  "type": "metric",
  "source_system": "prometheus",
  "query_spec": { "template": "error_rate_by_service", "params": {"service": "checkout", "range": "30m"} },
  "raw_response_ref": "s3://.../<hash>.json  (or inline for small payloads)",
  "content_hash": "sha256:...",
  "summary": "error_rate rose from 0.2% to 8.4% at 10:03Z",
  "collected_at": "2026-09-24T10:14:02Z",
  "expires_at": "2026-10-24T10:14:02Z"
}
```

`incident-core.evidence_refs` stores everything except `raw_response_ref`'s
target payload and the full raw blob — it holds the metadata and
`content_hash` needed to validate citations and render the RCA, keeping
`incident-core`'s schema free of large blobs. Large raw payloads live in
object storage or within `evidence-service`'s own `evidence` Postgres
schema — for v1, that schema lives in the same shared Postgres instance as
`incident-core`'s `incident_core` schema, under a separate database role
with no cross-schema grants (see `06-database-design.md` and ADR-0013).
Physical separation onto its own instance is deferred until scale requires
it.

## Confidence, staleness, and contradiction

- `confidence` on a `Hypothesis` is model-reported and **advisory only** —
  it is displayed to humans and used for ranking, but it never drives
  policy decisions (`09-remediation-policy-boundaries.md` — policy only
  ever looks at the deterministic blast-radius tier of the *proposed
  action*, never at how confident the model claims to be).
- Evidence has a `collected_at` timestamp; `evidence-service` exposes an
  `expires_at` for time-sensitive types (metrics/logs) — an RCA reviewer or
  the verification step can tell whether a citation is fresh relative to
  the incident timeline.
- `hypothesis_evidence_links.relation ∈ {supports, refutes}` — the model is
  explicitly asked to search for *disconfirming* evidence for its top
  hypothesis, not just confirming evidence, and both are stored. An RCA
  report that shows zero refuting evidence ever considered is itself a
  signal (surfaced in the eval harness's groundedness scoring, see
  `11-evaluation-architecture.md`).

## RCA report rendering

`rca_reports.summary` is generated by deterministic rendering code that
walks `root_cause_hypothesis_id` → its `hypothesis_evidence_links` → the
underlying `evidence` records, and produces a document where every claim
is a hyperlink/reference to a stored evidence ID. The model does not write
the final RCA prose freeform; it selects and ranks structured hypotheses,
and rendering is a template, not a generation step. (A future iteration
may use Claude to improve the prose *of an already-fully-cited* report,
strictly as a text-polish pass with no ability to add new claims — out of
scope for v1.)

## Phase 4 implementation

Implemented in `packages/evidence` (service + adapters + store),
`packages/tools` (tool contracts), `apps/evidence` (internal API). Store
design: ADR-0018.

### The path

```
telemetry backend            Prometheus | Loki | Tempo | deployment/config registry | Git | incident-core history
      |  (read-only; allow-listed query templates; bounded; validated params)
adapter                      packages/evidence/adapters/*  ->  Observation (raw + normalized + how obtained)
      |
EvidenceService              scope check (incident's service + dependency neighbors, its environment, bounded window)
      |                      persist EvidenceRecord  (evidence schema, insert-only, content_hash)
      |                      register EvidenceRef    (incident_core schema, via RegisterEvidenceRefCommand)
      v
EvidenceItem                 evidence_id + content_hash + summary + normalized data (never the raw response)
      |
investigation tool           packages/tools: strict schema, context-bound incident, retries, budgets
```

### Types (names as implemented)

| Implemented type | Earlier sketch name | Source | Operations |
|---|---|---|---|
| `metric` | `metric` | Prometheus | `get_metric_window` (+ `get_error_rate` / `get_latency` / `get_request_rate`), `get_service_health` |
| `log` | `log` | Loki | `get_logs` (severity / trace id / request id filters, counts, grouped representative lines) |
| `trace` | `trace` | Tempo | `get_trace` (summarized span tree), `get_traces` (recent / errors / slow) |
| `deployment` | `deploy` | simulated CI/CD registry | `get_recent_deployments` |
| `configuration` | `config_change` | simulated config registry | `get_config_changes` |
| `git_change` | `git_diff` | Git (this repository) | `get_recent_commits`, `get_code_changes` |
| `incident_history` | `historical_incident` | incident-core read API | `search_similar_incidents` (deterministic structured scoring) |

Metric queries are allow-listed templates (`adapters/prometheus.py::METRICS`);
logs, traces and git are built from structured parameters with validated
formats -- no operation accepts a raw query string.

### Record shape (as implemented)

`evidence_id`, `sequence` (insertion order), `incident_id`,
`investigation_id` (nullable until Phase 5), `evidence_type`,
`source_system`, `operation`, `subject_service`, `query_spec` (semantic op +
validated params + the exact rendered backend query or git argv),
`source_reference` (endpoint + query/window, trace id, commit shas, registry
key + record ids), `observed_at` (source time: newest sample/line/span/change
covered), `window_start`/`window_end`, `collected_at`, `expires_at`
(metric/log/trace only, +30 days), `requested_by` (e.g. `tool:get_logs`),
`content_hash`, `raw_response` (bounded), `raw_truncated`,
`normalized_payload`, `summary`, `result_count`.

### Provenance

`EvidenceRecord.provenance` answers, per record: where it came from
(`source_system`, `source_reference`), when it was observed (`observed_at`,
window) and collected (`collected_at`), what produced it (`operation`,
`query_spec`), who asked (`incident_id`, `investigation_id`, `requested_by`),
and exactly what came back (`content_hash` over the stored raw response;
`EvidenceService.verify` recomputes it from storage and checks
incident-core's ref agrees).

### Bounds (`packages/evidence/limits.py`)

Telemetry windows 1 minute - 3 hours; change windows up to 48 hours; no
window may start more than 24 hours before the incident opened, or end in
the future. At most 10 series x 60 points, 500 log lines fetched (each
<=2,000 chars, also in the raw record) and 20 representative groups
returned, 20 traces, 50 spans per summary, 20 deployments/config changes/
commits, 50 files per commit, 10 similar incidents from a pool of 200.

### Replay

`EvidenceService.get_incident_evidence(incident_id)` returns every record
for an incident ordered by `(collected_at, sequence)` -- identical on every
call, the input a future investigation replay or eval run consumes.

### What isn't evidence

A query rejected for scope, parameters, or a backend failure persists
nothing and returns a tool error. A trace-by-id lookup is scope-checked on
the trace's *content* (the services in it) before anything is stored.
