# 03 — Domain Model

## Aggregates

An **aggregate** here means: one consistency boundary, one owner, one
optimistic-concurrency version column, mutated only through `incident-core`.

### Incident (aggregate root)

The unit of work. Owns its lifecycle via the state machine
(`04-incident-state-machine.md`).

| Field | Notes |
|---|---|
| `id` | UUID |
| `status` | enum, see state machine |
| `severity` | derived from linked alerts, may be overridden by human |
| `service` / `environment` | primary affected service, for policy scoping |
| `correlation_key` | deterministic fingerprint used to attach new alerts (see below) |
| `attempt_count` | number of investigation→remediation→verification loops so far |
| `version` | optimistic concurrency token |
| `created_at`, `updated_at`, `closed_at` | |

Invariant: an `Incident` always has ≥1 linked `Alert`. An `Alert` belongs to
at most one open `Incident` at a time (enforced by a partial unique index —
see `06-database-design.md`).

Invariant: for sources that provide a stable `external_id`, `(source,
external_id)` is unique — a retried or duplicate webhook delivery resolves
to the same `Alert` row rather than creating a second one (see
`06-database-design.md`, "Alert deduplication and retries").

### Alert

Raw normalized signal from a source system (Alertmanager, PagerDuty,
generic webhook). **Persisted exclusively by `incident-core`**, inside the
same transaction that correlates it — `alert-ingestion` authenticates,
validates, and normalizes the inbound payload, then sends an
`AlertReceivedCommand`; it never writes this table itself (see
`02-component-boundaries.md` and `05-event-model.md`).

| Field | Notes |
|---|---|
| `id` | UUID |
| `external_id` | source system's alert ID, for dedup |
| `fingerprint` | stable hash of (source, labels) used for correlation |
| `source` | `prometheus` \| `pagerduty` \| `generic` |
| `labels`, `annotations` | jsonb, as received |
| `severity` | normalized enum |
| `status` | `firing` \| `resolved` (source-reported) |
| `incident_id` | nullable FK, set once correlated |
| `raw_payload` | jsonb, verbatim, for replay |

### Investigation

One bounded attempt by `investigation-agent` to explain and propose a fix
for an incident. An incident may have multiple investigations (retries
after failed verification).

| Field | Notes |
|---|---|
| `id` | UUID |
| `incident_id` | FK |
| `attempt_number` | 1-based, monotonic per incident |
| `status` | `running` \| `completed` \| `inconclusive` \| `failed` (budget exceeded, schema validation failed, etc.) |
| `agent_version`, `model_id` | for replay/eval provenance |
| `token_usage`, `tool_call_count`, `wall_clock_ms` | for budget enforcement and cost tracking |

### Hypothesis

A candidate root cause, always scoped to one `Investigation`.

| Field | Notes |
|---|---|
| `id` | UUID |
| `investigation_id` | FK |
| `statement` | short structured claim (not freeform prose — see `07-agent-tool-architecture.md`) |
| `confidence` | 0–1, model-reported, advisory only — never drives policy |
| `status` | `proposed` \| `supported` \| `refuted` \| `selected_root_cause` |
| `rank` | ordering among competing hypotheses |

### Evidence

Owned and written exclusively by `evidence-service` (see
`08-evidence-model.md`). `incident-core` stores only a reference
(`evidence_id`, `content_hash`) — never a copy of the payload — so there is
exactly one durable copy and no drift.

### HypothesisEvidenceLink

Many-to-many join: which evidence supports or refutes which hypothesis,
and how strongly. This join, not the hypothesis's prose, is what an RCA
report renders.

### RCAReport

Generated once a `Hypothesis` reaches `selected_root_cause`. Purely a
rendering of: the selected hypothesis, its statement, and the evidence
graph beneath it. Contains no claim that doesn't trace to an `evidence_id`.

### RemediationProposal

A reference to one `ActionCatalog` entry plus validated parameters —
**never** free-form text or code. Produced by `investigation-agent`,
persisted by `incident-core`.

| Field | Notes |
|---|---|
| `id` | UUID |
| `investigation_id` | FK |
| `action_catalog_id` | FK, must exist and be active |
| `parameters` | jsonb, validated against the catalog entry's JSON Schema before persist |
| `status` | `proposed` \| `policy_denied` \| `awaiting_approval` \| `approved` \| `denied` \| `executing` \| `executed` \| `verified` \| `verification_failed` |

### PolicyDecision

Immutable record of a deterministic evaluation. Never recomputed silently —
if policy changes, old decisions are not retroactively altered (see ADR-0007).

### Approval

A human decision on a `RemediationProposal` that policy marked
`REQUIRE_APPROVAL`.

### Execution

One attempt to run an approved `RemediationProposal`. Idempotency key =
`(remediation_proposal_id, attempt_number)`. `remediation-executor` reports
results back; it does not decide whether to run — `incident-core` already
decided by creating the `Execution` row.

### Verification

Post-execution automated check, defined by the `ActionCatalog` entry that
was executed (its success criteria), run against fresh `Evidence`.

### ActionCatalog (reference data, not incident data)

A whitelisted, versioned, human-reviewed remediation action. See
`09-remediation-policy-boundaries.md`.

### Policy (reference data)

A versioned deterministic rule set the `policy-engine` evaluates against.

## Aggregate relationship diagram

```
Incident 1──* Alert
Incident 1──* Investigation
Investigation 1──* Hypothesis
Investigation 1──* Evidence (via tool calls during that investigation)
Hypothesis *──* Evidence   (HypothesisEvidenceLink: supports/refutes, weight)
Investigation 0..1──* RemediationProposal
RemediationProposal 1──0..1 PolicyDecision (per policy evaluation attempt)
RemediationProposal 0..1──* Approval
RemediationProposal 0..1──1 Execution
Execution 0..1──1 Verification
RCAReport 1──1 Hypothesis (the selected root cause)
```

## Naming discipline

"Hypothesis" is always a *candidate*; only `selected_root_cause` status
promotes it into the RCA report. This distinction exists specifically so
the schema — not a prompt instruction — prevents an unverified guess from
being presented as a conclusion.
