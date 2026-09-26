# ADR-0025: Verification and the closed loop

Status: Accepted

## Context

Phase 8 implements `architecture/10-verification-design.md`, which predates
the Phase 7 remediation implementation. Some of its details conflict with
decisions already made (human approval for every action, no automatic
execution), and a few questions it left open needed answers: what counts
as a transient recovery, how to treat missing evidence, what RESOLVED means
for correlation, and how a stale verdict is prevented from closing an
incident that has moved on.

## Decision

1. **Deterministic rules only.** Verdicts come from pure functions
   (`packages/domain/verification.py`) over evidence-service observations.
   No model, no executor result, no remediation status.
2. **Per-action policies in the catalog** (grace, window, poll interval,
   required consecutive successes, checks). PASSED needs N consecutive
   conclusive passing observations, so a transient recovery resets.
3. **State checks first.** If the executed change is not in place (version,
   config, flag, replicas), verification fails immediately.
4. **Missing evidence is inconclusive, never success.** At the deadline:
   FAILED if anything conclusive was observed, TIMED_OUT (→ ESCALATED)
   otherwise.
5. **Baselines are mandatory where the checks need them**: captured through
   the evidence service before the action; if they cannot be captured, the
   action does not run.
6. **No automatic rollback proposal on failure** — a deliberate departure
   from §10's `rollback_action_id`. A failed verification leads only to a
   bounded re-investigation (`MAX_INVESTIGATION_ATTEMPTS`) or escalation.
   Any further action is a new proposal through policy and human approval.
7. **RESOLVED is closed for correlation**: it releases the correlation key
   (partial unique index rebuilt in migration 0007), so a recurrence opens
   a new incident instead of attaching to a verified-resolved one.
8. **Fencing and staleness.** Lease + `claim_attempt` fence every write; a
   verdict moves the incident only if it is still VERIFYING for this
   remediation's current verification and nothing newer exists. Otherwise
   the verdict is recorded and annotated, and the incident is left alone.
9. **New alerts** are detected by id against a snapshot taken at start
   (`known_alert_ids`), not by timestamps, to avoid DB/worker clock skew.

## Alternatives considered

- *Auto-proposing the catalog rollback on failure*: rejected. It is a second
  remediation chosen without an investigation, and Phase 7 already rules out
  automatic execution; proposing it automatically adds little, since a human
  approves regardless.
- *Single passing observation*: rejected — flapping services would resolve.
- *Treat evidence-service outage as failure*: rejected — it would trigger
  re-investigation of a problem that may not exist; escalation is honest.
- *Keep RESOLVED holding the correlation key until CLOSED*: rejected;
  `CLOSED` is not implemented and recurrences would be hidden.

## Consequences

Verification is slower (minutes per action) but cannot be fooled by a
single good sample or by executor success. Humans see more escalations
(timeouts, exhausted attempts) instead of automatic retries. `CLOSED` and
`SUPPRESSED` remain unimplemented.
