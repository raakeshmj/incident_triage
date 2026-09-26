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
| `INVESTIGATING` | `RCA_READY` | `InvestigationCompleted` (command from investigation-agent, schema + evidence-citation validated) | incident-core | result contains exactly one hypothesis with status `selected_root_cause` | `investigation.id` |
| `INVESTIGATING` | `ESCALATED` | `InvestigationCompleted` with `inconclusive_reason` set (no confident root cause / insufficient evidence), **or** `InvestigationFailed` (schema invalid, evidence citation invalid, budget/time/token exceeded, agent crash/timeout per watchdog) | incident-core | no hypothesis reaches `selected_root_cause` | `investigation.id` |
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

## `RCA_READY` means a root cause was selected — nothing else

`RCA_READY` is a strict claim: it is only reachable when the investigation
selected a root-cause hypothesis. `InvestigationResult.selected_root_cause_index`
and `InvestigationResult.inconclusive_reason` are mutually exclusive (enforced
by schema validation — see `07-agent-tool-architecture.md`), so there is no
path by which an inconclusive or evidence-insufficient investigation can
land in `RCA_READY`. Both "inconclusive" and "insufficient evidence" route
directly to `ESCALATED`, identically to a hard investigation failure (bad
schema, budget exceeded, agent crash) — from the state machine's point of
view these are all "the autonomous investigation did not produce a usable
root cause," and all of them hand the incident to a human rather than
attempting an automatic retry. (Contrast this with `VERIFICATION_FAILED`,
which *does* retry automatically up to `max_attempts` — that loop exists
because a failed remediation is a different, better-understood situation
than an investigation that couldn't reach a conclusion at all.)

## Alerts arriving mid-lifecycle

A new alert that the correlation engine (ADR-0015) matches to an
already-open incident is linked via `AlertCorrelated` — it does **not**
restart the state machine. If it raises the incident's effective severity,
`incident-core` emits `IncidentSeverityChanged`, which `notification-service`
reacts to, but the state itself is untouched. This avoids an alert storm
causing the same incident to bounce back to `TRIAGING` repeatedly.

### Phase 2 addendum: correlation behavior cases

The multi-signal correlation engine (ADR-0015) is what decides "linked" vs.
"new incident" above. Its behavior across the cases that matter,
concretely:

| Case | Behavior |
|---|---|
| **A** — first alert for a signature | No open candidate incidents found (or none score above threshold) → `TRIAGING`, `IncidentCreated`. |
| **B** — a related alert arrives shortly afterward | Scores above threshold against the open incident's most recent alert (service/environment/temporal/type signals) → `AlertCorrelated`, no state change, no new incident. |
| **C** — an unrelated alert arrives | Scores below threshold against every open candidate (or there are none for that service+environment) → new incident, exactly as Case A. |
| **D** — the same external alert retries | Caught before correlation even runs: command-level idempotency (same `idempotency_key`) or the `(source, external_id)` database dedup — see `06-database-design.md`. The correlation engine is never invoked a second time for a genuine retry. |
| **E** — multiple related alerts arrive concurrently | A Postgres advisory lock serializes the "read candidates → decide → write" sequence per `(service, environment)` (ADR-0015) — the second alert to acquire the lock always sees the first's already-committed incident, so they converge on one incident rather than racing to create two. |
| **F** — a late-arriving alert | The candidate query bounds itself to incidents whose most recent alert is within the configured lookback window (default 15 minutes) of "now" — an incident with no activity in that window is never offered to the engine as a candidate at all, regardless of how well its signature would otherwise match. The alert gets a new incident. |

## Alert resolution (Phase 4)

Alert-source resolved notifications (Alertmanager `send_resolved`) are
real inputs to the lifecycle, with deterministic semantics (ADR-0019):

- An alert is one **firing episode** (Alertmanager: `fingerprint` +
  `startsAt`). Its status flips `firing -> resolved` exactly once.
- The only alert-driven transition is the table's own
  `TRIAGING -> CANCELLED`, guarded by **no linked alert still firing**. One
  resolved alert never ends an incident another linked alert is still
  firing for.
- Past `TRIAGING`, resolution is recorded (and emitted as `AlertResolved`)
  but causes no transition: verification or a human ends those states.
  `RESOLVED` remains "verification confirmed recovery, or a human resolved".
- A duplicate resolution is a no-op; a resolution for an episode never seen
  firing is recorded unlinked and opens nothing; a late firing notification
  for an ended episode reopens nothing.
- Resolution takes the same `(service, environment)` advisory lock as
  correlation, and the transition is an optimistic `version` update, per
  "Concurrency control" below.

Because nothing yet moves an incident out of `TRIAGING` (no debounce
scheduler before the investigation agent exists), every incident whose
alerts all clear today ends `CANCELLED`.

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
        │                                      │ (verification failed, attempt<max)
        ├──(inconclusive / insufficient evidence / budget exceeded / schema invalid)──▶ ESCALATED
        ▼                                      │
    RCA_READY (root cause selected) ──(deny / no action)───▶ ESCALATED
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

## Phase 5: the investigation edges are live

- `TRIAGING → INVESTIGATING`: the investigation worker's scheduler calls
  `due_for_investigation(debounce_seconds)` (default 60 s,
  `INVESTIGATION_DEBOUNCE_SECONDS`) and `request_investigation` for each;
  the guard (≥1 linked alert still firing) and the optimistic `version`
  check run under a row lock; idempotent per incident.
- `INVESTIGATING → RCA_READY`: only through `complete()`, after
  incident-core re-checks grounding and the deterministic stopping criteria
  (`15-investigation-engine.md`).
- `INVESTIGATING → ESCALATED`: inconclusive, budget exhausted, malformed
  output, evidence unavailable, model failure. The reason code is on the
  `InvestigationFailed` event and the investigation row.
- If the incident has already left `INVESTIGATING` when an investigation
  finishes, the result is recorded but the incident is not moved.
  Remediation states are untouched by Phase 5.

## Phase 7: the remediation edges are live

| From | To | Trigger |
|---|---|---|
| RCA_READY | AWAITING_APPROVAL | a proposal passes policy (always REQUIRE_APPROVAL in Phase 7) |
| RCA_READY | ESCALATED | policy denies the proposal |
| AWAITING_APPROVAL | REMEDIATION_IN_PROGRESS | a human with an eligible role approves the exact proposal |
| AWAITING_APPROVAL | ESCALATED | rejection, or the approval timeout (never auto-approves) |
| AWAITING_APPROVAL | RCA_READY | the proposal is withdrawn or superseded (new edge) |
| REMEDIATION_IN_PROGRESS | VERIFYING | the execution succeeded; `VerificationRequested` emitted |
| REMEDIATION_IN_PROGRESS | ESCALATED | execution failed, or a kill switch stopped it before it ran |

The `RCA_READY -> REMEDIATION_IN_PROGRESS` (pre-approved ALLOW) edge is not
implemented: there is no automatic execution. Verification (VERIFYING ->
RESOLVED / VERIFICATION_FAILED) is a later phase; incidents wait in
VERIFYING. Remediation state lives on the `remediations` aggregate; the
incident moves only along these edges, and only if it is still where the
remediation expects it (a human's ESCALATED is never overwritten).
