# incident-core

Status: not implemented — design only. See
`docs/architecture/02-component-boundaries.md`,
`docs/architecture/03-domain-model.md`,
`docs/architecture/04-incident-state-machine.md`,
`docs/architecture/05-event-model.md`,
`docs/architecture/06-database-design.md`.

## Responsibility

The sole writer of the Incident aggregate and everything transactionally
coupled to it: alert correlation, the incident state machine, hypotheses,
evidence references, RCA reports, remediation proposals, policy decisions,
approvals, executions, verifications, and the outbox event log. Every other
service interacts with this data only by sending a command that this
service validates and applies.

## Internal modules (one process, cleanly separated in code)

- `correlation` — deterministic alert-to-incident matching (ADR-0004).
- `state_machine` — the transition table in `04-incident-state-machine.md`,
  enforced with optimistic concurrency.
- `approvals` — human approval requests/decisions, role-gated.
- `timeline` — read-only query API over the event log, for the UI and audit.
- `outbox_relay` — publishes committed events to Redis Streams.

## Owns

`alerts` (link only), `incidents`, `investigations`, `hypotheses`,
`evidence_refs`, `hypothesis_evidence_links`, `rca_reports`,
`remediation_proposals`, `policy_decisions`, `approvals`, `executions`,
`verifications`, `events` (outbox), `processed_commands`,
`action_catalog`, `policies` (reference data, deployed via CI/CD).

## Does not own

Evidence payloads (see `evidence-service`), remediation execution (see
`remediation-executor`), the Claude API call (see `investigation-agent`).

## Talks to

- Inbound commands from: `alert-ingestion`, `investigation-agent`,
  `remediation-executor`, `web-ui` (BFF).
- Outbound: `policy-engine` (synchronous evaluation call), Postgres, Redis
  (outbox publish).
