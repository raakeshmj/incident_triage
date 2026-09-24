# evidence-service

Status: not implemented — design only. See
`docs/architecture/08-evidence-model.md`,
`docs/adr/0005-evidence-service-anti-hallucination.md`,
`docs/adr/0013-v1-single-postgres-logical-schemas.md`.

## Responsibility

The only component permitted to query external observability, deployment,
Git, config, and historical-incident systems for facts used in
investigation or verification. Every query and its verbatim raw response
are persisted as an immutable, content-hashed evidence record **before**
any summarized view is returned to a caller. This is the system's
anti-hallucination boundary.

## Modes

`live` (real backing systems), `record` (real systems + save fixture),
`replay` (serve saved fixtures — used by local dev and `eval-harness`).

## Owns

Its own logical Postgres schema (`evidence`) — metadata + raw payload
references — behind its own database role scoped to that schema only.
For v1 this is physically the same Postgres instance as `incident-core`'s
`incident_core` schema, with no cross-schema grants either direction; full
physical separation (its own instance, or object storage for large
payloads) is deferred until scale requires it (ADR-0013).

## Does not own

Whether a citation is accepted into a hypothesis (that's `incident-core`'s
validation) — this service only guarantees that anything it returns is
backed by a real, stored record.

## Talks to

- Inbound: `investigation-agent` (tool calls), `incident-core`
  (verification checks, and read access for the `search_historical_incidents`
  tool — see `docs/review/critical-review.md` §9 for why this dependency is
  called out explicitly).
- Outbound (read-only credentials only): Prometheus, Loki, Tempo, GitHub
  API, config store, Kubernetes API (read-only).
