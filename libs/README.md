# libs

Status: not implemented — design only.

Shared, versioned contracts used by multiple services — kept here
specifically so the event/command/evidence schemas have exactly one
definition, not one per service that can drift.

- `schemas/` — Pydantic models: domain entities, event envelope
  (`docs/architecture/05-event-model.md`), command payloads, the
  `InvestigationResult` / `RemediationProposalOut` agent-output contract
  (`docs/architecture/07-agent-tool-architecture.md`), the `action_catalog`
  parameter-schema contract.
- `otel/` — shared OpenTelemetry setup (tracing/metrics conventions,
  `correlation_id`/`causation_id` propagation) so every service instruments
  consistently.
