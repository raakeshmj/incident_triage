# 06 — Database Design

Single Postgres database for `incident-core` (owns the schema below).
`evidence-service` owns a separate schema/database for evidence blobs
(kept physically separate so its different access pattern — large,
write-once, TTL-eligible payloads — doesn't bloat `incident-core`'s
transactional tables or its backup/restore time). `eval-harness` uses its
own store (files or a separate database) — see `11-evaluation-architecture.md`.

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
    evidence_ids   UUID[] NOT NULL DEFAULT '{}',
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ
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

-- Idempotency ledger for inbound commands
CREATE TABLE processed_commands (
    idempotency_key  TEXT PRIMARY KEY,
    command_type     TEXT NOT NULL,
    result           JSONB NOT NULL,
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
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
```

## Concurrency mechanisms used, and why

| Mechanism | Where | Prevents |
|---|---|---|
| Optimistic concurrency (`version` column) | `incidents` | Two concurrent commands (e.g. a new alert correlating in, and an investigation completing) silently clobbering each other's status change |
| Partial unique index on `correlation_key` for open incidents | `incidents` | Race between two `AlertReceived` commands both deciding to create a *new* incident for the same signal |
| Unique `idempotency_key` | `executions` | A retried "execute" command running the remediation twice |
| Unique `(incident_id, attempt_number)` | `investigations` | Two concurrent investigation-start commands double-launching an attempt |
| `processed_commands` ledger | all inbound commands to incident-core | Any at-least-once redelivery re-applying a transition |

All of the above are enforced at the database constraint level, not just
in application code, specifically so a bug in a single service instance
cannot violate them under concurrent load.

## Retention

- `events`: retained indefinitely (append-only audit trail); partitioned by
  month once volume warrants it.
- `alerts`, incident-related tables: retained indefinitely for eval/audit;
  no hard-delete path in v1 (a future ADR covers archival/anonymization if
  needed for compliance).
- `evidence_blobs` (in evidence-service): TTL-eligible for large raw
  payloads (e.g. full log dumps) after N days, but the `content_hash` and
  metadata row is kept forever so citations remain resolvable (see
  `08-evidence-model.md`).
