# 10 — Verification Design

## Purpose

An executed remediation is not "done" because the executor returned
success — it is done when the *incident's actual signal* recovers.
Verification is the deterministic check that closes that loop. It is not an
LLM judgment call, and it trusts none of: executor `SUCCESS`, a model's
assertion, or the remediation's own status.

Implemented in Phase 8. Decisions that differ from the original design are
recorded in ADR-0025.

## Components

| Piece | Where | Role |
|---|---|---|
| Rules (pure) | `packages/domain/verification.py` | `VerificationPolicy`, `build_spec`, `evaluate_sample`, `decide` — no I/O, no clock |
| Per-action policy | `packages/remediation/catalog.py` (`verification=`) | windows + check templates per catalog action |
| Commands (single writer) | `packages/incident/verifications.py` (`VerificationCoreService`) | start, claim, record observation, finalize, incident transitions, outbox events |
| Baseline | `RemediationCoreService.record_baseline` + `packages/verification/engine.py` (`BaselineCollector`) | captured through the evidence service *before* the action runs |
| Observer | `packages/verification/observer.py` (`EvidenceObserver`) | every observation goes through `EvidenceService`; returns a `Sample` + evidence ids |
| Engine | `packages/verification/engine.py` (`VerificationEngine`) | tick-based: claim a due verification, observe, record |
| Worker | `apps/worker/verification_main.py` | consumes `VerificationRequested`, ticks due verifications, heartbeats |

## Lifecycle

```
remediation runner
  ├─ baseline: EvidenceObserver(needs of the action) -> record_baseline (fenced, once)
  │     baseline unavailable -> action NOT executed (retryable failure)
  ├─ executor (idempotent by key)
  └─ complete_execution  ── one transaction ──────────────────────────────
         remediation EXECUTED · incident VERIFYING · verification PENDING
         (spec + baseline copied, baseline evidence linked, poll_sequence 0)
         outbox: RemediationExecuted, VerificationRequested
verification worker
  ├─ VerificationRequested -> start: PENDING -> RUNNING, grace + deadline set,
  │                           known_alert_ids snapshot; VerificationStarted
  └─ tick: claim_due (lease + claim_attempt++) -> observe -> record_observation
           -> evaluate_sample -> streak -> decide
           -> PASSED | FAILED | TIMED_OUT -> finalize (VerificationCompleted + incident)
```

## Windows

`VerificationPolicy(grace_seconds, window_seconds, poll_interval_seconds,
required_consecutive, baseline_required, checks)`; `timeout = grace +
window + 2 × interval`. No observation is taken during the grace period.

| Action | grace | window | interval | consecutive | Checks |
|---|---|---|---|---|---|
| `restart_service` | 45 s | 240 s | 15 s | 3 | health healthy · error_rate ≤ 0.05 · no new firing alerts |
| `scale_service` | 30 s | 240 s | 15 s | 3 | replicas = baseline + increase_by · health · latency_p95 ≤ 1.0 s · no new alerts |
| `rollback_deployment` | 60 s | 300 s | 15 s | 3 | running version = to_version · health · error_rate · latency · no new alerts |
| `disable_feature_flag` | 30 s | 180 s | 15 s | 2 | flag = false · health · no new alerts (no baseline required) |
| `revert_configuration` | 45 s | 240 s | 15 s | 3 | config value = to_value · health · error_rate · no new alerts |

`VERIFICATION_TIME_SCALE` multiplies all windows (tests, evaluation and the
local demo only; production is 1.0). The policy version
(`verification-2026.09-1`) is stored on every verification.

## Deciding

- **A sample** is one observation of every source the spec needs. If any
  source errored or is missing, the sample is **inconclusive** — it neither
  passes nor fails, and it resets the success streak.
- **State checks** (version, config, flag, replicas) are definitive: if the
  executed change is not actually in place, verification FAILS immediately.
- **PASSED** only after `required_consecutive` consecutive passing
  conclusive samples. One good sample between bad ones (a transient
  recovery) resets and does not pass.
- **At the deadline:** FAILED if any conclusive observation was made
  (the signal was seen and never recovered for long enough); TIMED_OUT if
  none was (the evidence never became conclusive — silence is never success).
- **New alerts:** alerts firing for the incident that were not in the
  `known_alert_ids` snapshot at start. It is an id comparison, independent of
  clock skew between database and workers.

## Closing the loop (incident-core)

| Verdict | Incident | Events |
|---|---|---|
| PASSED | VERIFYING → RESOLVED | VerificationCompleted, IncidentStatusChanged, IncidentResolved |
| FAILED, attempts left, an alert still firing | VERIFYING → VERIFICATION_FAILED; the investigation scheduler then starts a new attempt (→ INVESTIGATING) | VerificationCompleted(next_action=reinvestigate), IncidentStatusChanged, then InvestigationStarted |
| FAILED, attempts exhausted or nothing firing | VERIFYING → VERIFICATION_FAILED → ESCALATED | VerificationCompleted(next_action=escalate), IncidentEscalated |
| TIMED_OUT | VERIFYING → ESCALATED | VerificationCompleted, IncidentEscalated |

`MAX_INVESTIGATION_ATTEMPTS` (default 2) bounds the
investigate → remediate → verify loop. **A failed verification never
executes another remediation automatically**: a re-investigation may lead to
a new proposal, which goes through policy and human approval like any other.
No automatic rollback proposal is created (ADR-0025).

## Safety

- **Fencing.** Every write carries the lease owner and `claim_attempt`; a
  worker that lost its lease (crash, pause) cannot record an observation or
  a verdict.
- **Staleness.** A verdict moves the incident only if the incident is still
  VERIFYING, the remediation is EXECUTED, the remediation's
  `verification_ref` is this verification and no newer remediation exists.
  Otherwise the verdict is recorded with `next_action = none` and an
  annotated reason ("[not applied: …]") — a human's ESCALATED is never
  overwritten.
- **Crash recovery.** State lives in Postgres: a restarted worker resumes by
  claiming due verifications; a verification past its deadline is finalized
  from its recorded observations.
- **Idempotency.** Duplicate `VerificationRequested` deliveries are no-ops;
  `start` is idempotent; the consumer dedup ledger still applies.
- **Immutability.** Baselines, observations and evidence links are
  append-only (triggers); the spec columns of a verification are frozen.

## Evidence

Every baseline and observation is collected through `EvidenceService` and
stored as ordinary evidence (`requested_by = verification:<id>`), linked via
`verification_evidence (verification_id, evidence_id, role, poll_sequence)`
with a foreign key to `evidence_refs`. `role = baseline` (sequence 0) or
`observation` (1..n). "Did it work" is as auditable and replayable as "what
caused it."
