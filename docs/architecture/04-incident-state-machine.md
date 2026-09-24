# 04 — Incident State Machine

The state machine is owned exclusively by `incident-core`. Every transition
is a single database transaction: validate current `version` (optimistic
concurrency) → check guard → apply new `status` → increment `version` →
insert outbox event, all in one commit. No other service ever writes
`incidents.status`.

## States

| State | Meaning | Terminal? |
|---|---|---|
| `TRIAGING` | Incident created from correlated alert(s); debounce window open to absorb related alerts before committing resources to investigation | No |
| `INVESTIGATING` | An `Investigation` is running (or queued) | No |
| `RCA_READY` | Latest investigation completed; a root-cause hypothesis is selected; remediation proposal may or may not exist | No |
| `AWAITING_APPROVAL` | A `RemediationProposal` exists and policy returned `REQUIRE_APPROVAL` | No |
| `REMEDIATION_IN_PROGRESS` | An approved `Execution` is running | No |
| `VERIFYING` | Execution completed; automated verification window is running | No |
| `VERIFICATION_FAILED` | Verification did not confirm recovery | No |
| `RESOLVED` | Verification confirmed recovery, or a human manually resolved | No |
| `ESCALATED` | Human has taken over; system stops proposing/acting autonomously | No |
| `SUPPRESSED` | Correlated as noise/duplicate/maintenance-window; never investigated | Yes |
| `CANCELLED` | Closed without remediation (e.g. alert self-resolved before investigation concluded, or human dismissed) | Yes |
| `CLOSED` | Terminal archival state after `RESOLVED`, `CANCELLED`, or human-resolved `ESCALATED` | Yes |

## Transition table

| From | To | Trigger | Actor | Guard | Idempotency key |
|---|---|---|---|---|---|
| *(none)* | `TRIAGING` | `AlertCorrelated` (new incident) | incident-core (correlation module) | no open incident matches `correlation_key` | `alert.fingerprint` + debounce bucket |
| `TRIAGING` | `SUPPRESSED` | correlation classifies as noise/maintenance window | incident-core | dedup/suppression rule matched (deterministic) | `incident.id` |
| `TRIAGING` | `INVESTIGATING` | debounce window elapsed | incident-core scheduler | ≥1 linked alert still firing | `incident.id` + `attempt_count` |
| `TRIAGING` | `CANCELLED` | all linked alerts resolved before debounce elapsed | incident-core | no alert firing | `incident.id` |
| `INVESTIGATING` | `RCA_READY` | `InvestigationCompleted` (command from investigation-agent) | incident-core validates schema + evidence refs | ≥1 hypothesis with `selected_root_cause`, OR explicit "no confident hypothesis" result | `investigation.id` |
| `INVESTIGATING` | `ESCALATED` | `InvestigationFailed` (budget exceeded / schema invalid / agent error) | incident-core | attempt exhausted retry budget (see below) | `investigation.id` |
| `RCA_READY` | `AWAITING_APPROVAL` | policy-engine returns `REQUIRE_APPROVAL` for the proposal | incident-core | proposal exists | `remediation_proposal.id` |
| `RCA_READY` | `REMEDIATION_IN_PROGRESS` | policy-engine returns `ALLOW` (pre-approved tier) | incident-core | proposal exists, action catalog entry active | `remediation_proposal.id` |
| `RCA_READY` | `ESCALATED` | policy-engine returns `DENY`, or no proposal and human requests takeover | incident-core / human | — | `remediation_proposal.id` or `incident.id` |
| `RCA_READY` | `CLOSED` | human accepts RCA with no remediation needed | human via web-ui | — | `incident.id` |
| `AWAITING_APPROVAL` | `REMEDIATION_IN_PROGRESS` | human approves | human via web-ui/Slack | approver has required role for blast-radius tier | `approval.id` |
| `AWAITING_APPROVAL` | `ESCALATED` | human denies, or approval timeout elapses | incident-core scheduler / human | **timeout always denies, never approves** | `approval.id` |
| `REMEDIATION_IN_PROGRESS` | `VERIFYING` | `ExecutionCompleted` (command from remediation-executor) | incident-core | execution status = `succeeded` | `execution.id` |
| `REMEDIATION_IN_PROGRESS` | `ESCALATED` | `ExecutionFailed` (executor error, not a verification failure) | incident-core | — | `execution.id` |
| `VERIFYING` | `RESOLVED` | verification window completes, success criteria met | incident-core | — | `verification.id` |
| `VERIFYING` | `VERIFICATION_FAILED` | verification window completes, criteria not met | incident-core | — | `verification.id` |
| `VERIFICATION_FAILED` | `INVESTIGATING` | auto-retry: rollback action (if defined) executed, new investigation starts | incident-core | `attempt_count < max_attempts` (config, default 2) | `incident.id` + new `attempt_count` |
| `VERIFICATION_FAILED` | `ESCALATED` | `attempt_count >= max_attempts` | incident-core | — | `incident.id` |
| `RESOLVED` | `CLOSED` | post-mortem data captured / timeout | incident-core scheduler | — | `incident.id` |
| `ESCALATED` | `CLOSED` | human resolves manually | human via web-ui | — | `incident.id` |
| *(any non-terminal)* | `ESCALATED` | human explicitly takes over | human via web-ui | always allowed — human override is a safety valve | `incident.id` |

## Alerts arriving mid-lifecycle

A new alert that correlates to an already-open incident (`correlation_key`
match) is linked via `AlertLinked` — it does **not** restart the state
machine. If it raises the incident's effective severity, `incident-core`
emits `IncidentSeverityChanged`, which `notification-service` reacts to,
but the state itself is untouched. This avoids an alert storm causing the
same incident to bounce back to `TRIAGING` repeatedly.

## Concurrency control

- Every transition reads `incidents.version`, and the `UPDATE` includes
  `WHERE id = :id AND version = :expected_version`. Zero rows affected ⇒
  the caller reloads and retries (or, for command handlers, returns a
  conflict to the caller, which retries the command — commands are
  idempotent, see `05-event-model.md`).
- This makes "two alerts correlate to the same new incident at once" and
  "verification completes while a human is approving" safe by construction
  rather than by locking discipline.

## Why `ESCALATED` is reachable from everywhere

Autonomy has to be interruptible unconditionally. A human can always force
`ESCALATED` regardless of guards; every other transition is guarded. This
is the one exception, and it's deliberate — see
`review/critical-review.md` for the reasoning.

## Diagram

```
                 ┌────────────┐
        ┌───────▶│ SUPPRESSED │ (terminal)
        │        └────────────┘
        │
[new] ─▶ TRIAGING ──(all resolved)──▶ CANCELLED (terminal)
        │
        ▼ (debounce elapsed, still firing)
   INVESTIGATING ◀────────────────────────────┐
        │                                      │ (retry, attempt<max)
        ├──(budget exceeded)──▶ ESCALATED      │
        ▼                                      │
    RCA_READY ──(deny / no action)───▶ ESCALATED
        │        └─(human accepts, no fix)──▶ CLOSED
        ├──(REQUIRE_APPROVAL)──▶ AWAITING_APPROVAL
        │                              ├─(approve)─┐
        │                              └─(deny/timeout)─▶ ESCALATED
        └──(pre-approved ALLOW)────────┐            │
                                        ▼            ▼
                              REMEDIATION_IN_PROGRESS
                                        │
                              ┌─────────┴─────────┐
                        (exec fails)          (exec succeeds)
                              ▼                     ▼
                          ESCALATED              VERIFYING
                                                    │
                                      ┌─────────────┴─────────────┐
                                (criteria met)             (criteria not met)
                                      ▼                             ▼
                                  RESOLVED               VERIFICATION_FAILED
                                      │                     │           │
                                      ▼               (attempt<max) (attempt>=max)
                                   CLOSED                   │           │
                                                             ▼           ▼
                                                      INVESTIGATING  ESCALATED
                                                                         │
                                                                         ▼
                                                                      CLOSED (human)
```
