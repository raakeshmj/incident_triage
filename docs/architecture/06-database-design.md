# 06 — Database Design

**V1 / local topology**: a single PostgreSQL instance hosts two logical
schemas — `incident_core` (owned exclusively by `incident-core`) and
`evidence` (owned exclusively by `evidence-service`) — each with its own
Postgres role holding privileges only on its own schema. This makes the
single-writer ownership rule in `02-component-boundaries.md` a
database-enforced guarantee, not just a code convention: a bug in one
service cannot write the other's tables, because its role has no grant to
do so. `eval-harness` uses its own separate store (files or a separate
database) — see `11-evaluation-architecture.md`.

Physical separation — moving the `evidence` schema to its own database
instance, or moving large raw payloads to object storage — is deferred
until scale actually requires it (large payload volume, an independent
backup/restore cadence, or independent read/write scaling).
`evidence-service`'s `raw_response_ref` column is already a
pointer/reference field specifically so that migration changes only where
the referenced bytes live, not the evidence model itself. See ADR-0013.

The schema below is `incident_core`. `evidence-service`'s `evidence` schema
is described in `08-evidence-model.md`.

## `incident-core` schema

```sql
-- Alerts: append-mostly, linked to at most one open incident
CREATE TABLE alerts (
    id              UUID PRIMARY KEY,
    external_id     TEXT,
    source          TEXT NOT NULL,               -- 'prometheus' | 'pagerduty' | 'generic'
    fingerprint     TEXT NOT NULL,
    labels          JSONB NOT NULL,
    annotations     JSONB NOT NULL DEFAULT '{}',
    severity        TEXT NOT NULL,
    status          TEXT NOT NULL,                -- 'firing' | 'resolved'
    incident_id     UUID REFERENCES incidents(id),
    raw_payload     JSONB NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON alerts (fingerprint);
CREATE INDEX ON alerts (incident_id);
-- Dedup for sources that provide a stable external id (e.g. a PagerDuty
-- incident id). Sources without one rely solely on command-level
-- idempotency (see "Alert deduplication and retries" below).
CREATE UNIQUE INDEX alerts_source_external_id
    ON alerts (source, external_id)
    WHERE external_id IS NOT NULL;

-- Incidents: the aggregate root
CREATE TABLE incidents (
    id               UUID PRIMARY KEY,
    status           TEXT NOT NULL,
    severity         TEXT NOT NULL,
    service          TEXT NOT NULL,
    environment      TEXT NOT NULL,
    correlation_key  TEXT NOT NULL,
    attempt_count    INT NOT NULL DEFAULT 0,
    version          BIGINT NOT NULL DEFAULT 0,   -- optimistic concurrency
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at        TIMESTAMPTZ
);
-- At most one OPEN incident per correlation_key at a time:
CREATE UNIQUE INDEX incidents_open_correlation_key
    ON incidents (correlation_key)
    WHERE status NOT IN ('CLOSED', 'CANCELLED', 'SUPPRESSED');

CREATE TABLE investigations (
    id               UUID PRIMARY KEY,
    incident_id      UUID NOT NULL REFERENCES incidents(id),
    attempt_number   INT NOT NULL,
    status           TEXT NOT NULL,               -- running|completed|inconclusive|failed
    agent_version    TEXT NOT NULL,
    model_id         TEXT NOT NULL,
    token_usage      INT,
    tool_call_count  INT,
    wall_clock_ms    INT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    UNIQUE (incident_id, attempt_number)
);

CREATE TABLE hypotheses (
    id                UUID PRIMARY KEY,
    investigation_id  UUID NOT NULL REFERENCES investigations(id),
    statement         JSONB NOT NULL,             -- structured claim, not prose
    confidence        NUMERIC(3,2),
    status            TEXT NOT NULL,              -- proposed|supported|refuted|selected_root_cause
    rank              INT NOT NULL
);

-- Evidence rows here are REFERENCES ONLY. Payload lives in evidence-service.
CREATE TABLE evidence_refs (
    id                UUID PRIMARY KEY,           -- = evidence-service's evidence.id
    investigation_id  UUID NOT NULL REFERENCES investigations(id),
    evidence_type     TEXT NOT NULL,              -- metric|log|trace|deploy|git|config|history
    content_hash      TEXT NOT NULL,              -- must match evidence-service record
    source_system     TEXT NOT NULL,
    collected_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE hypothesis_evidence_links (
    hypothesis_id  UUID NOT NULL REFERENCES hypotheses(id),
    evidence_id    UUID NOT NULL REFERENCES evidence_refs(id),
    relation       TEXT NOT NULL,                 -- supports|refutes
    weight         NUMERIC(3,2) NOT NULL,
    PRIMARY KEY (hypothesis_id, evidence_id)
);

CREATE TABLE rca_reports (
    id                        UUID PRIMARY KEY,
    incident_id               UUID NOT NULL REFERENCES incidents(id),
    investigation_id          UUID NOT NULL REFERENCES investigations(id),
    root_cause_hypothesis_id  UUID NOT NULL REFERENCES hypotheses(id),
    summary                   TEXT NOT NULL,       -- rendered from the hypothesis + evidence graph
    generated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by               TEXT
);

CREATE TABLE remediation_proposals (
    id                 UUID PRIMARY KEY,
    investigation_id   UUID NOT NULL REFERENCES investigations(id),
    action_catalog_id  TEXT NOT NULL,
    action_catalog_version TEXT NOT NULL,
    parameters         JSONB NOT NULL,
    status             TEXT NOT NULL,
    proposed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE policy_decisions (
    id                        UUID PRIMARY KEY,
    remediation_proposal_id   UUID NOT NULL REFERENCES remediation_proposals(id),
    policy_version            TEXT NOT NULL,
    decision                  TEXT NOT NULL,       -- ALLOW|DENY|REQUIRE_APPROVAL
    required_approver_roles   TEXT[],
    reasons                   JSONB NOT NULL,
    policy_context            JSONB NOT NULL,      -- immutable snapshot of the PolicyEvaluationContext
                                                    -- evaluated against (see 09-remediation-policy-boundaries.md);
                                                    -- never updated after insert, so replay uses historical
                                                    -- context, not live ambient state
    evaluated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE approvals (
    id                        UUID PRIMARY KEY,
    remediation_proposal_id   UUID NOT NULL REFERENCES remediation_proposals(id),
    requested_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    approver_id               TEXT,
    decision                  TEXT,                -- approved|denied|timed_out
    decided_at                TIMESTAMPTZ,
    channel                   TEXT                 -- web-ui|slack
);

CREATE TABLE executions (
    id                        UUID PRIMARY KEY,
    remediation_proposal_id   UUID NOT NULL REFERENCES remediation_proposals(id),
    attempt_number            INT NOT NULL DEFAULT 1,
    idempotency_key           TEXT NOT NULL,
    status                    TEXT NOT NULL,       -- pending|running|succeeded|failed
    result                    JSONB,
    executor_version          TEXT,
    started_at                TIMESTAMPTZ,
    completed_at              TIMESTAMPTZ,
    UNIQUE (remediation_proposal_id, attempt_number),
    UNIQUE (idempotency_key)
);

CREATE TABLE verifications (
    id             UUID PRIMARY KEY,
    execution_id   UUID NOT NULL REFERENCES executions(id),
    status         TEXT NOT NULL,                  -- pending|passed|failed
    window_seconds INT NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ
);

-- Normalized join, replacing a UUID[] column, so every linked evidence
-- record is FK-enforced against evidence_refs rather than a free-floating
-- array of IDs that could reference something that was never actually stored.
CREATE TABLE verification_evidence (
    verification_id  UUID NOT NULL REFERENCES verifications(id),
    evidence_id       UUID NOT NULL REFERENCES evidence_refs(id),
    poll_sequence     INT NOT NULL,       -- ordering within the verification window's polling loop
    collected_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (verification_id, evidence_id)
);

-- Outbox / durable event log — append-only, never updated except published_at
CREATE TABLE events (
    sequence        BIGSERIAL PRIMARY KEY,
    event_id        UUID NOT NULL UNIQUE,
    event_type      TEXT NOT NULL,
    schema_version  INT NOT NULL,
    aggregate_type  TEXT NOT NULL,
    aggregate_id    UUID NOT NULL,
    correlation_id  UUID,
    causation_id    UUID,
    payload         JSONB NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ
);
CREATE INDEX ON events (aggregate_id, sequence);
CREATE INDEX ON events (published_at) WHERE published_at IS NULL;

-- Idempotency ledger for inbound commands, scoped per command type so an
-- idempotency key can never be accidentally shared across different
-- command types (e.g. an investigation_id and an execution_id colliding
-- as raw strings).
CREATE TABLE processed_commands (
    command_type     TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    result           JSONB NOT NULL,
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (command_type, idempotency_key)
);

-- Reference data, deployed via CI/CD, not runtime-writable by services
CREATE TABLE action_catalog (
    id                    TEXT NOT NULL,
    version               TEXT NOT NULL,
    description           TEXT NOT NULL,
    parameters_schema     JSONB NOT NULL,          -- JSON Schema
    blast_radius_tier     INT NOT NULL,
    allowed_environments  TEXT[] NOT NULL,
    requires_approval     BOOLEAN NOT NULL,
    success_criteria      JSONB NOT NULL,          -- for verification
    rollback_action_id    TEXT,
    active                BOOLEAN NOT NULL DEFAULT true,
    PRIMARY KEY (id, version)
);

CREATE TABLE policies (
    version        TEXT PRIMARY KEY,
    rules          JSONB NOT NULL,                 -- or rego_source TEXT, see ADR-0007
    active         BOOLEAN NOT NULL DEFAULT false,
    effective_from TIMESTAMPTZ NOT NULL
);

-- Runtime-toggleable emergency control. UNLIKE action_catalog/policies
-- above, this table IS writable at runtime (platform_admin only, via
-- incident-core's admin command path, never a direct DB edit by any
-- other service) — precisely so it can take effect immediately in an
-- incident without waiting on a deploy. See ADR-0009.
CREATE TABLE kill_switches (
    scope        TEXT PRIMARY KEY,        -- 'global' or 'service:<service_name>'
    engaged      BOOLEAN NOT NULL DEFAULT false,
    engaged_by   TEXT,
    engaged_at   TIMESTAMPTZ,
    reason       TEXT
);
```

## Phase 2 additions (as implemented)

The outbox table above is named `events` in this document's original
schema sketch; the actual implementation names it `outbox_events` (per an
explicit Phase 1 instruction) — noted here once so the name mismatch isn't
mistaken for drift. Phase 2 adds, via migration `0002_events_correlation`:

```sql
ALTER TABLE outbox_events
    ADD COLUMN producer            TEXT NOT NULL DEFAULT 'incident-core',
    ADD COLUMN publish_attempts    INT  NOT NULL DEFAULT 0,
    ADD COLUMN last_publish_error  TEXT;
```

`producer` is the canonical envelope field (`05-event-model.md`).
`publish_attempts` / `last_publish_error` are diagnostic-only bookkeeping
for the outbox relay's retry behavior (ADR-0014) — never used to decide
correctness, only to answer "why hasn't this published yet" without
grepping logs. `publish_attempts` counts relay *passes* that attempted the
event, not raw `publish()` calls within a pass (a pass's internal retries
are only visible via the `outbox.publish_retry` metric/log).

```sql
-- Consumer-side idempotency ledger (ADR-0014) -- keyed by the event's own
-- event_id, never a Redis-specific message id, so it survives both a
-- relay-side duplicate publish and a consumer restart.
CREATE TABLE consumed_events (
    consumer_name  TEXT NOT NULL,
    event_id       UUID NOT NULL,
    processed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_id)
);

-- Supports the correlation engine's candidate query
-- (repository.find_open_incident_candidates, ADR-0015).
CREATE INDEX ix_incidents_service_environment_status
    ON incidents (service, environment, status);

-- Supports finding an incident's most-recently-received alert (temporal
-- proximity scoring).
CREATE INDEX ix_alerts_incident_id_received_at
    ON alerts (incident_id, received_at DESC);
```

**`correlation_key` semantics changed in Phase 2.** In Phase 1 it was
simply the triggering alert's `fingerprint`. As of the multi-signal
correlation engine (ADR-0015), a *new* incident's `correlation_key` is
`{fingerprint}:{time_bucket}` — disambiguating it from a stale incident
that happens to share a fingerprint but fell outside the correlation
window, which the plain-fingerprint scheme could not do (a real bug caught
by `tests/integration/test_late_arriving_alert.py` during Phase 2
implementation, not a hypothetical). `correlation_key` is consequently an
internal uniqueness token, not a stable "the identity of this alert type"
value — code that wants the latter should read the initiating alert's
`fingerprint` directly.

## Phase 4 additions (as implemented)

Migration `0003_resolution_evidence` (incident_core):

```sql
ALTER TABLE alerts ADD COLUMN resolved_at TIMESTAMPTZ;   -- set once, firing -> resolved
CREATE INDEX ix_alerts_incident_id_status ON alerts (incident_id, status);

-- Deviation from the sketch above (ADR-0018): keyed to the incident, with a
-- nullable investigation_id and no FK on it until `investigations` exists.
CREATE TABLE evidence_refs (
    id                UUID PRIMARY KEY,               -- = evidence.evidence_records.id
    incident_id       UUID NOT NULL REFERENCES incidents(id),
    investigation_id  UUID,
    evidence_type     TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    source_system     TEXT NOT NULL,
    collected_at      TIMESTAMPTZ NOT NULL,
    registered_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- + trigger evidence_refs_immutable: BEFORE UPDATE OR DELETE -> raise
```

evidence-service's own migration history (`alembic -n evidence`, run as
`evidence_service_role`, version table inside `evidence`) creates
`evidence.evidence_records` -- the full record shape is in
`08-evidence-model.md`, "Phase 4 implementation" -- with the same
insert-only trigger and a `content_hash` format check. Neither role can
read the other's schema (verified by
`tests/integration/test_evidence_store.py`).

Alert identity changed with resolution support (ADR-0019): Alertmanager
alerts' `external_id` is now the firing episode (`fingerprint:startsAt`),
and a resolved notification's command idempotency key carries a
`:resolved` suffix, so it's never deduplicated against its own firing
notification.

## Alert deduplication and retries

Alert sources retry webhook deliveries on timeout or a 5xx response, and
some deliver at-least-once by design. Two independent layers make this
safe:

1. **Command-level idempotency (primary).** `alert-ingestion` derives an
   `idempotency_key` for the `AlertReceivedCommand` it sends — the
   source's `external_id` when the source provides one, otherwise a
   content hash of the normalized payload plus a debounce time bucket. A
   redelivered webhook produces the same key, so `incident-core`'s
   `processed_commands` ledger (keyed by `(command_type, idempotency_key)`
   — see `05-event-model.md`) returns the original result without
   re-processing.
2. **Database-level dedup (defense in depth).** The
   `alerts_source_external_id` partial unique index prevents a second
   `alerts` row for the same `(source, external_id)` even if a bug or a
   differently-derived idempotency key let a duplicate command through.
   The insert fails with a constraint violation; the command handler
   catches that specific violation, fetches the existing `Alert` row by
   `(source, external_id)`, and proceeds with correlation/linking against
   it — the same "validate, and also enforce it at the constraint level"
   pattern used for `remediation_proposals.parameters` and for
   `executions.idempotency_key` elsewhere in this schema.

For sources that don't supply a stable `external_id` (uncommon, but true
of some generic webhook senders), dedup relies solely on layer 1 — this is
a known, accepted limitation, not a gap to silently paper over: such
sources should be configured with a stable, source-side alert identifier
whenever the integration supports it.

## Concurrency mechanisms used, and why

| Mechanism | Where | Prevents |
|---|---|---|
| Optimistic concurrency (`version` column) | `incidents` | Two concurrent commands (e.g. a new alert correlating in, and an investigation completing) silently clobbering each other's status change |
| Partial unique index on `correlation_key` for open incidents | `incidents` | Race between two `AlertReceivedCommand`s both deciding to create a *new* incident for the same signal |
| Partial unique index on `(source, external_id)` | `alerts` | A duplicate `Alert` row being created for the same source-reported alert on a retried/duplicate webhook delivery (defense in depth behind command-level idempotency) |
| Unique `idempotency_key` | `executions` | A retried "execute" command running the remediation twice |
| Unique `(incident_id, attempt_number)` | `investigations` | Two concurrent investigation-start commands double-launching an attempt |
| `processed_commands` ledger, keyed by `(command_type, idempotency_key)` | all inbound commands to incident-core | Any at-least-once redelivery re-applying a transition — scoping by command type also prevents an accidental key collision across unrelated command types |
| FK constraints on `verification_evidence` | `verifications` ↔ `evidence_refs` | A verification citing an evidence record that was never actually stored (previously possible with a bare `UUID[]` column) |
| `consumed_events` ledger, keyed by `(consumer_name, event_id)` | any Redis Streams consumer | The same at-least-once redelivery problem as `processed_commands`, on the consumer side of the event bus instead of the command side (Phase 2, ADR-0014) |
| `pg_advisory_xact_lock(hash(service, environment))` | correlation decisions (Phase 2, ADR-0015) | Write skew: two concurrent alerts for the same service+environment, both reading zero matching candidates and independently deciding to create a new incident. This is the one mechanism in this table that is *not* a database constraint — an advisory lock depends on every code path remembering to acquire it — which is exactly why the partial unique index on `correlation_key` above remains as a backstop even though the lock makes it rarely the deciding mechanism in practice. |

All but the advisory lock are enforced at the database constraint level,
not just in application code, specifically so a bug in a single service
instance cannot violate them under concurrent load.

## Retention

- `events`: retained indefinitely (append-only audit trail); partitioned by
  month once volume warrants it.
- `alerts`, incident-related tables: retained indefinitely for eval/audit;
  no hard-delete path in v1 (a future ADR covers archival/anonymization if
  needed for compliance).
- `evidence_blobs` (in evidence-service's `evidence` schema): TTL-eligible
  for large raw payloads (e.g. full log dumps) after N days, but the
  `content_hash` and metadata row is kept forever so citations remain
  resolvable (see `08-evidence-model.md`).

## Phase 5: investigation tables (migration `0004_investigations`)

All in `incident_core`, all written only by `InvestigationCoreService`:

| Table | Key columns | Constraints |
|---|---|---|
| `investigations` | `incident_id`, `attempt_number`, `status`, `lease_owner`, `lease_expires_at`, `iteration_count`, `tool_call_count`, `evidence_count`, token counts (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`), `model_provider`, `model_name`, `model_config`, `budget`, `last_action`, `failure_reason`, `escalation_reason`, `inconclusive_reason`, `selected_hypothesis_id`, `final_result`, `started_at`, `completed_at` | unique `(incident_id, attempt_number)`; index `(status, lease_expires_at)` for the resume sweep |
| `hypotheses` | `investigation_id`, `key`, `description`, `status`, `confidence`, `missing_evidence`, `rationale` | unique `(investigation_id, key)` |
| `hypothesis_evidence_links` | `hypothesis_id`, `evidence_id`, `relation` | PK `(hypothesis_id, evidence_id)`; FK to `evidence_refs`; `relation in (supports, contradicts)`; immutable |
| `investigation_steps` | `investigation_id`, `sequence`, `iteration`, `kind`, `call_id`, `payload`, `latency_ms` | unique `(investigation_id, sequence)`; immutable |
| `rca_reports` | `investigation_id`, `selected_hypothesis_id`, `summary`, `report` | unique `investigation_id`; immutable |

`evidence_refs.investigation_id` now has an FK to `investigations`.
Immutability is a `BEFORE UPDATE OR DELETE` trigger calling
`incident_core.reject_row_mutation()`, so the audit trail can't be edited
even by incident-core's own role. Every write command locks the
investigation row and checks the lease owner (fencing, ADR-0020).

## Phase 6: hypothesis cause (migration `0005_hypothesis_cause`) and the evaluation database

- `hypotheses.cause_category` (checked against the closed taxonomy) and
  `hypotheses.component`: required by incident-core when a hypothesis is
  created, fixed afterwards; nullable only for rows written before 0005.
- `incident_intelligence_eval`: a second database on the same server with
  the same schemas, roles and grants
  (`infrastructure/postgres/init/02-eval-database.sql`, `make eval-db`),
  migrated by the same Alembic histories (`make eval-migrate`). The
  evaluation harness truncates it before every run; it holds nothing that
  matters between runs.

## Phase 7: remediation tables (migration `0006_remediation`)

| Table | Notes |
|---|---|
| `remediations` | the aggregate: incident, investigation, action, catalog version, parameters, target, environment, reason, expected effect, tier, proposal hash, source/proposer, status, policy decision, approval/execution status, attempts, executor result, failure reason, verification ref, correlation id, lease, version. Unique `(incident_id, idempotency_key)`. **Proposal columns immutable** (trigger `reject_proposal_mutation`); no deletes. |
| `remediation_policy_decisions` | decision, policy + catalog version, catalog digest, tier, required roles, every rule result, reasons, the full `policy_context`. Immutable. |
| `remediation_approvals` | one per remediation (unique): decision (approved / rejected / timed_out), approver, role, the proposal hash and decision id approved. Immutable. |
| `remediation_executions` | one per attempt: unique `(remediation_id, attempt_number)` and unique `idempotency_key`; status RUNNING / SUCCEEDED / FAILED / TIMED_OUT / UNKNOWN, owner, deadline, result, error. |
| `remediation_timeline` | append-only audit: event, from/to status, actor, correlation id, action, catalog + policy version, details, time. Immutable. |
| `kill_switches` | `global` or `service:<name>`; runtime-writable through incident-core only (ADR-0009). |

## Phase 8: verification tables (migration `0007_verification`)

| Table | Purpose | Mutability |
|---|---|---|
| `remediation_baselines` | one per remediation: the pre-action values (`values` JSONB) and when/by whom captured | insert-only |
| `verifications` | one per executed remediation: frozen spec (checks, windows, policy version, baseline), status PENDING / RUNNING / PASSED / FAILED / TIMED_OUT, lease owner / expiry, `claim_attempt` (fencing), `consecutive_successes`, `observation_count`, `known_alert_ids` (snapshot at start), grace / deadline / next poll, result, `failure_reason`, `next_action` | status columns only; spec and identity frozen by trigger |
| `verification_observations` | one row per poll: sample, per-check results, passed / conclusive, errors | insert-only |
| `verification_evidence` | PK `(verification_id, evidence_id)`, FK to `evidence_refs`; `role` baseline or observation; `poll_sequence` (0 = baseline) | insert-only |

The same migration rebuilds the partial unique index
`incidents_open_correlation_key` to exclude `RESOLVED` (now a closed
status); the repository's `ON CONFLICT … WHERE` predicate is derived from
the same constant (`CLOSED_INCIDENT_STATUSES`) so the two cannot drift.
