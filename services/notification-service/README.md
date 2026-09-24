# notification-service

Status: not implemented — design only. See
`docs/architecture/02-component-boundaries.md`,
`docs/architecture/05-event-model.md`.

## Responsibility

Stateless consumer of domain events (via Redis Streams, fanned out from
`incident-core`'s outbox). Delivers human-facing notifications (Slack,
email, webhook) on incident lifecycle transitions. Fire-and-forget — must
never block or influence the state machine.

## Owns

A small consumer-side dedup table (`consumer_name`, `event_id`) to make
at-least-once delivery safe against duplicate notifications.

## Talks to

- Inbound: Redis Streams (event fan-out).
- Outbound: Slack/email/webhook endpoints.
