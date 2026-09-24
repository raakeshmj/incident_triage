# alert-ingestion

Status: not implemented — design only. See
`docs/architecture/02-component-boundaries.md` and
`docs/architecture/06-database-design.md` (`alerts` table).

## Responsibility

The system's public-facing trust boundary. Receives alert webhooks
(Prometheus Alertmanager, PagerDuty, generic), authenticates them (HMAC/
shared-secret), validates and normalizes payloads into the `Alert` schema,
and hands off to `incident-core` via an idempotent command.

## Owns

Nothing durable of its own — writes go to `incident-core`'s `alerts` table
via command, keyed by an idempotency key derived from the source's alert
fingerprint.

## Does not own

Correlation, incident state, anything downstream of "a normalized alert
now exists."

## Talks to

- Inbound: public internet (webhook senders), authenticated.
- Outbound: `incident-core` only.
