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

Nothing. This service has **no database access and no credentials to
`incident-core`'s database** — it cannot write the `alerts` table even in
principle. It sends `incident-core` an `AlertReceivedCommand`, idempotency-
keyed by the source's `external_id` when present, else a content hash of
the normalized payload plus a debounce time bucket. `incident-core` is the
one that persists the `Alert` row, in the same transaction that correlates
it — see `docs/architecture/06-database-design.md`.

## Does not own

Correlation, incident state, anything downstream of "a normalized alert
now exists."

## Talks to

- Inbound: public internet (webhook senders), authenticated.
- Outbound: `incident-core` only.
