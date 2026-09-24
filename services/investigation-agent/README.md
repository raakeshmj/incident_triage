# investigation-agent

Status: not implemented — design only. See
`docs/architecture/07-agent-tool-architecture.md`,
`docs/adr/0005-evidence-service-anti-hallucination.md`,
`docs/adr/0006-structured-agent-output-schema-validated.md`.

## Responsibility

Stateless worker invoked once per `Investigation` attempt. Runs a bounded
Claude tool-use loop, with every tool a read-only proxy to
`evidence-service`, and returns one schema-validated result
(`InvestigationResult`) to `incident-core` via a command.

## Owns

Nothing durable. No database access. No credentials to production systems.

## Explicitly cannot do

- Write to any database table directly.
- Call `remediation-executor`.
- Produce freeform, unvalidated output that any downstream component acts
  on — its only output channel is the `submit_findings` schema.

## Talks to

- Inbound: `incident-core` (investigation request, via command/queue).
- Outbound: `evidence-service` (tool calls), Anthropic API (Claude).
