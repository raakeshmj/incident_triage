# incident-core

Status: not implemented — design only. See
`docs/architecture/02-component-boundaries.md`,
`docs/architecture/03-domain-model.md`,
`docs/architecture/04-incident-state-machine.md`,
`docs/architecture/05-event-model.md`,
`docs/architecture/06-database-design.md`.

## Responsibility

The sole writer of the `Alert` and Incident aggregates and everything
transactionally coupled to them: alert persistence and correlation, the
incident state machine, hypotheses, evidence references, RCA reports,
remediation proposals, policy decisions (including building the
`PolicyEvaluationContext` handed to `policy-engine`), approvals,
executions, verifications, and the outbox event log. Every other service
interacts with this data only by sending a command that this service
validates and applies — including `alert-ingestion`: on receiving an
`AlertReceivedCommand`, `incident-core` persists the `Alert`, runs
correlation, and emits the `AlertReceived` domain event, all in one
transaction.

## Internal modules (one process, cleanly separated in code)

- `alerts` — persistence and dedup of normalized `Alert` records.
- `correlation` — deterministic alert-to-incident matching (ADR-0004).
- `state_machine` — the transition table in `04-incident-state-machine.md`,
  enforced with optimistic concurrency.
- `policy_context` — builds the `PolicyEvaluationContext` (remediation
  rate, kill-switch state, incident fields) immediately before each call to
  `policy-engine` (ADR-0012).
- `approvals` — human approval requests/decisions, role-gated.
- `timeline` — read-only query API over the event log, for the UI and audit.
- `outbox_relay` — publishes committed events to Redis Streams.

## Owns

`alerts` (sole writer — persists, correlates, and links, all in one
transaction), `incidents`, `investigations`, `hypotheses`,
`evidence_refs`, `hypothesis_evidence_links`, `rca_reports`,
`remediation_proposals`, `policy_decisions` (including the immutable
`policy_context` snapshot), `approvals`, `executions`, `verifications`,
`verification_evidence`, `events` (outbox), `processed_commands` (keyed by
`(command_type, idempotency_key)`), `action_catalog`, `policies`
(reference data, deployed via CI/CD), `kill_switches` (runtime-writable,
`platform_admin` only).

## Does not own

Evidence payloads (see `evidence-service`), remediation execution (see
`remediation-executor`), the Claude API call (see `investigation-agent`).

## Talks to

- Inbound commands from: `alert-ingestion`, `investigation-agent`,
  `remediation-executor`, `web-ui` (BFF).
- Outbound: `policy-engine` (synchronous evaluation call), Postgres, Redis
  (outbox publish).
