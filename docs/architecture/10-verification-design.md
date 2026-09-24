# 10 — Verification Design

## Purpose

An executed remediation is not "done" because the executor returned
success — it's done when the *incident's actual signal* recovers.
Verification is the deterministic check that closes that loop; it is not
another LLM judgment call.

## Design

- Each `action_catalog` entry declares `success_criteria` (see
  `09-remediation-policy-boundaries.md`): a metric query template, a
  threshold, and a sustained duration.
- On `ExecutionCompleted`, `incident-core` creates a `Verification` row
  (`status = pending`, `window_seconds` from the catalog entry) and
  schedules a check.
- The check calls **`evidence-service`** — the same source of truth the
  investigation used — to fetch fresh evidence against the
  `success_criteria` query, on a poll cadence (e.g. every 30s) for up to
  `window_seconds`. Every poll's result is stored as `Evidence` too, linked
  to the `Verification` via the normalized `verification_evidence` join
  table (FK-enforced against `evidence_refs`, not a bare array of IDs —
  see `06-database-design.md`), so "did it work" is exactly as auditable
  and replayable as "what caused it."
- **Pass**: criteria held for the full sustained duration ⇒
  `VerificationCompleted(passed)` ⇒ `RESOLVED`.
- **Fail**: criteria not met by window end ⇒
  `VerificationCompleted(failed)` ⇒ `VERIFICATION_FAILED`.

## What happens on failure

1. If the executed action's catalog entry declares a `rollback_action_id`,
   `incident-core` creates a new `RemediationProposal` for the rollback
   action automatically, runs it through the **same policy-engine and
   approval path** (a rollback is still a remediation and still subject to
   policy — it does not get a free pass), and executes it if
   allowed/approved.
2. Whether or not a rollback ran, the incident moves to
   `VERIFICATION_FAILED` → (per the state machine) either a new
   `Investigation` attempt (if `attempt_count < max_attempts`) or
   `ESCALATED`.
3. `attempt_count` is incremented either way — this bounds the total
   number of autonomous investigate→remediate→verify loops per incident
   (default 2), guaranteeing the system cannot cycle indefinitely and
   *must* hand off to a human eventually if it can't resolve things.

## Why verification cannot be skipped or LLM-judged

- Skipping it would mean "executed" and "resolved" are conflated, which
  hides failed fixes and silently leaves incidents open in practice while
  marked closed in the system — a direct violation of the "state must
  reflect reality" principle.
- Using Claude to *judge* whether the incident recovered (rather than a
  deterministic threshold check) reintroduces exactly the hallucination
  risk the evidence model was built to eliminate, at the highest-stakes
  point in the pipeline. Verification criteria are therefore always a
  simple, human-authored threshold/duration check against real metrics —
  no model call in this path.

## Escalation on verification-path failure

If `evidence-service` itself is unreachable during the verification
window (distinct from the metric being unhealthy), that is **not** treated
as pass or fail — it's an infrastructure fault. `incident-core` marks the
verification `status = pending` beyond its window, emits an alert to
`notification-service` ("verification could not complete"), and transitions
to `ESCALATED` rather than guessing. Silence from the monitoring stack is
never interpreted as success.
