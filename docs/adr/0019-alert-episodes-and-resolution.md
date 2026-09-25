# ADR-0019: Alert firing episodes and resolution semantics

Status: Accepted

## Context

Phase 3 recorded Alertmanager's `send_resolved` notifications but they had
no effect: the idempotency key was `prometheus:external:{fingerprint}`, so a
resolved notification was swallowed as a duplicate of its firing
notification. Worse, Alertmanager's `fingerprint` hashes labels only, so a
*second* firing episode of the same alert (fires, resolves, fires again)
also deduped against the first, forever.

## Decision

1. **Identity = one firing episode.** The Alertmanager adapter's
   `external_id` is `fingerprint:startsAt` (UTC-normalized). `startsAt` is
   fixed for an episode and repeated on its resolved notification; a new
   episode gets a new one.
2. **Status-aware command idempotency.** A resolved notification's key is
   `{source}:external:{external_id}:resolved`; the firing key keeps its
   pre-Phase-4 shape so existing ledger rows still dedup redeliveries.
3. **Resolution follows the state machine, nothing else.** The alert's
   status flips `firing -> resolved` once (`resolved_at` set). The incident
   moves only along 04-incident-state-machine.md's `TRIAGING -> CANCELLED`
   edge ("all linked alerts resolved before debounce elapsed"), guarded by
   "no linked alert still firing". Past `TRIAGING`, alert resolution is
   recorded but drives no transition -- verification or a human ends those
   states. `RESOLVED` stays reserved for verified remediation.
4. **Serialized with correlation.** Resolution takes the same
   `(service, environment)` advisory lock as correlation, so a resolving
   alert and a newly-correlating alert for the same incident can't both see
   a stale "firing" count. The transition itself is an optimistic
   `version` update (the doc's concurrency rule); a version conflict is a
   409 (retryable), never a silent overwrite.
5. **Edge cases**: a duplicate resolution changes nothing; a resolution for
   an episode never seen firing is recorded unlinked and opens nothing; a
   late firing notification for an already-resolved episode reopens
   nothing.

## Consequences

- An incident that self-heals while still in `TRIAGING` ends `CANCELLED`
  ("closed without remediation"), emitting `AlertResolved` and
  `IncidentStatusChanged(TRIAGING -> CANCELLED, reason=all_linked_alerts_resolved)`.
  `CANCELLED` is a closed status, so the next episode opens a new incident.
- Nothing yet moves an incident out of `TRIAGING` (no debounce scheduler),
  so in practice every self-resolving incident today ends `CANCELLED`.
