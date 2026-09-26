# 09 — Remediation / Policy Boundaries

## The shape of the boundary

```
investigation-agent            (proposes: action_catalog_id + parameters — data, not code)
        │
        ▼
incident-core                  (persists RemediationProposal, builds PolicyEvaluationContext,
                                 calls policy-engine)
        │
        ▼
policy-engine                  (pure function of proposal + context: ALLOW | DENY | REQUIRE_APPROVAL, versioned)
        │
        ├── DENY ─────────────────────────────▶ ESCALATED (nothing runs)
        ├── REQUIRE_APPROVAL ──▶ human decision ──▶ approved ──┐
        └── ALLOW (pre-approved tier only) ───────────────────┤
                                                                ▼
                                                    incident-core creates Execution
                                                    row (idempotency key) BEFORE
                                                    dispatching to remediation-executor
                                                                │
                                                                ▼
                                                    remediation-executor
                                                    (scoped credentials, one adapter
                                                     per action_catalog entry)
```

The model never sees `remediation-executor`'s address, credentials, or
API. It produces a `RemediationProposalOut` (see `07-...md`) that is just
JSON. Everything after that is deterministic code.

## Action Catalog

The action catalog is **the only vocabulary remediation can be expressed
in.** Each entry is authored and reviewed by a human, merged via normal
code review, and deployed as versioned reference data (`action_catalog`
table, populated by CI/CD — never runtime-writable).

```json
{
  "id": "restart_deployment",
  "version": "1.0",
  "description": "Rolling restart of a Kubernetes Deployment",
  "parameters_schema": {
    "type": "object",
    "required": ["namespace", "deployment_name"],
    "properties": {
      "namespace": {"type": "string", "enum": ["checkout-prod", "checkout-staging"]},
      "deployment_name": {"type": "string", "maxLength": 63}
    },
    "additionalProperties": false
  },
  "blast_radius_tier": 1,
  "allowed_environments": ["staging", "prod"],
  "requires_approval": true,
  "success_criteria": {"metric": "error_rate_by_service", "threshold": "< 1%", "sustained_seconds": 180},
  "rollback_action_id": null
}
```

Example higher-tier action:

```json
{
  "id": "rollback_deployment",
  "blast_radius_tier": 2,
  "requires_approval": true,
  "success_criteria": {"metric": "error_rate_by_service", "threshold": "< 1%", "sustained_seconds": 300},
  "rollback_action_id": null
}
```

`parameters_schema` is enforced twice: once by `incident-core` before
persisting the proposal (`07-agent-tool-architecture.md`), and again by
`remediation-executor` immediately before acting — defense in depth against
a proposal row being modified or replayed outside the normal path.

## Policy engine

- **`evaluate(proposal, action_catalog_entry, policy, policy_context) -> PolicyDecision`**
  is the engine's entire interface, and the function body performs **zero
  I/O**: no database queries, no Redis reads, no external calls, no LLM
  involvement. Every dynamic fact the rules need arrives pre-computed in
  `policy_context` (below) — this is what makes it an actual pure
  function, rather than a function that merely looks pure while quietly
  reaching into ambient state. (An earlier draft of this design had rule
  examples reference `incident.environment` and
  `remediation_rate(incident.service, …)` as if they were free lookups —
  that was an internal inconsistency, corrected here. See ADR-0012.)
- **Versioned and immutable**: `policy_decisions` records the exact
  `policy_version` used **and** the exact `policy_context` snapshot the
  decision was computed from. Changing policy tomorrow does not change the
  interpretation of yesterday's decision, and replaying a past decision
  never depends on the live state of the system at replay time — required
  for audit and for eval-harness replay to be meaningful.
- **Implementation**: a small rule DSL evaluated in Python (or OPA/Rego if
  the rule surface grows — deferred, see ADR-0007) — deliberately not a
  general-purpose scripting language, so every possible policy outcome can
  be enumerated and tested.

### PolicyEvaluationContext

All dynamic facts the rules can condition on, assembled once, immediately
before evaluation:

```python
class PolicyEvaluationContext(BaseModel):
    context_id: UUID
    incident_environment: str
    incident_severity: str
    incident_service: str
    action_blast_radius_tier: int          # denormalized from the action_catalog entry, for rule convenience
    remediation_count_last_hour: int       # executions for this service in the trailing window
    prior_attempts_this_incident: int      # incidents.attempt_count at evaluation time
    global_kill_switch_engaged: bool
    service_kill_switch_engaged: bool
    captured_at: datetime
```

**Who builds it, and when**: `incident-core`, in the same request handler
that is about to call `policy-engine`, immediately before the call.
`incident-core` is the one place in the remediation path that does the I/O
the rules need: it queries `executions` for the remediation-rate count,
reads `incidents` for environment/severity/service/attempt_count, and
reads the `kill_switches` table (see `06-database-design.md`) for both
switches. `policy-engine` never touches any of these directly — it only
ever receives the already-assembled `PolicyEvaluationContext` value as a
plain argument.

**Immutable capture for audit/replay**: the exact `PolicyEvaluationContext`
used is serialized and stored verbatim in `policy_decisions.policy_context`
(JSONB), in the same row and the same transaction as the decision, and is
never updated afterward. This is what makes a `PolicyDecision` fully
replayable on its own: re-running `evaluate(proposal, action_catalog_entry,
policy, stored_context)` for a given `policy_version` reproduces the
identical decision, independent of whatever the live remediation rate or
kill-switch state happens to be at replay time. Without capturing the
context, replay would silently substitute *current* ambient state for
*historical* ambient state — a correctness bug the immutable snapshot
exists specifically to prevent.

Example rules (illustrative, not final syntax) — every dynamic fact is
read from `context`, never from an ambient lookup:

```
DENY   if context.action_blast_radius_tier >= 3
DENY   if context.incident_environment == "prod" and action.id not in prod_allowed_actions
REQUIRE_APPROVAL(roles=["on_call_engineer", "service_owner"])
       if context.incident_environment == "prod" and context.action_blast_radius_tier >= 1
ALLOW  if context.incident_environment == "staging" and context.action_blast_radius_tier <= 1
DENY   if context.remediation_count_last_hour >= 3        # runaway-loop breaker
DENY   if context.global_kill_switch_engaged or context.service_kill_switch_engaged
```

Notably: **model-reported `confidence` never appears in a policy rule, and
never appears in `PolicyEvaluationContext` either** — it is not a
deterministic fact, and policy must never condition on it. Policy only
ever conditions on the deterministic properties of the proposed action and
the incident's environment/history, all captured explicitly in the
context.

## Approval

- Default: **required**, per action's `requires_approval` flag and the
  policy's tier-based rule. `ALLOW` (no human needed) is only reachable for
  specifically whitelisted low-tier actions in non-prod, or prod actions a
  policy version has explicitly graduated after a track record —an
  explicit, reviewed policy change, not a default.
- Approval requests carry the full RCA, the evidence graph, and the exact
  action + parameters that will run — never a summary the approver has to
  trust blindly.
- **Timeout always denies (→ `ESCALATED`), never auto-approves.** Silence
  is treated as "a human needs to look at this," not as consent.
- Role-gating: `policy_decisions.required_approver_roles` names who may
  approve a given tier; `incident-core` checks the approving identity's
  role at approval time, not just at request time (role changes between
  request and decision are respected).

## Execution

- `incident-core` creates the `Execution` row (with its unique
  `idempotency_key`) and flips the incident to `REMEDIATION_IN_PROGRESS`
  **before** dispatching — so a crash after dispatch but before the
  executor's ack still leaves a durable, resumable record, not a lost
  in-flight action.
- `remediation-executor` holds **scoped, short-lived credentials per action
  type** (e.g. a Kubernetes ServiceAccount limited to `patch`/`restart` on
  Deployments in specific namespaces, never cluster-admin). One adapter
  module per `action_catalog` entry; adding a new action means writing and
  reviewing a new adapter, not granting broader ambient permissions.
- Idempotent by construction: adapters use the target system's own
  idempotency primitives where available (e.g. Kubernetes resource
  `resourceVersion` checks, CI job dedup keys) keyed by
  `executions.idempotency_key`, so a redelivered "execute" command cannot
  double-act.
- **Kill switch**: the `kill_switches` table (global and per-service
  scopes — see `06-database-design.md`), read by `incident-core`'s context
  builder and surfaced to `policy-engine` as
  `PolicyEvaluationContext.global_kill_switch_engaged` /
  `service_kill_switch_engaged` — flipping it makes every subsequent
  proposal evaluate to `DENY` immediately, independent of any other rule.
  Unlike `policies`/`action_catalog`, this table is runtime-writable
  (`platform_admin` only, still routed through `incident-core`) precisely
  so it can act immediately in an emergency without a deploy — see
  ADR-0009.
- **Rate limiting**: `incident-core`'s context builder computes
  `remediation_count_last_hour` for the incident's service, and the
  `DENY if context.remediation_count_last_hour >= N` policy rule (above)
  enforces the runaway-loop breaker — see `review/critical-review.md`.

## Dry-run mode

Every `action_catalog` adapter supports a `dry_run` flag that validates
parameters and simulates the call (e.g. a Kubernetes dry-run apply) without
side effects. Used by: the eval harness (`11-evaluation-architecture.md`),
staging rehearsal, and an optional "preview" step surfaced to the approver
in the web UI before they approve.

## Phase 7: as built

```
InvestigationCompleted ─▶ remediation worker: RemediationPlanner (deterministic)
                                     │ RemediationProposal (data)        operator API ─┐
                                     ▼                                                  │
      incident-core RemediationCoreService.propose ◀────────────────────────────────────┘
        ├ record PROPOSED (proposal immutable, proposal_hash)
        ├ build PolicyEvaluationContext from the DB          ─┐  stored verbatim with
        ├ packages/policy evaluate(...)  (pure)               │  the decision, policy +
        └ DENY ─▶ POLICY_REJECTED (incident ESCALATED)        ─┘  catalog version, all rules
          else ─▶ AWAITING_APPROVAL (incident AWAITING_APPROVAL)
                     │  POST /api/v1/remediations/{id}/approval
                     │  (operator token; roster role; proposal_hash + policy_decision_id)
                     ▼
                 APPROVED (incident REMEDIATION_IN_PROGRESS) ── RemediationApproved
                     │  remediation worker: RemediationRunner
                     ▼
      claim_execution: re-check kill switches, approval binding, catalog, target,
                       attempts ─▶ execution row (idempotency key, deadline, lease)
                     │  SimulatorRemediationExecutor (catalog action only)
                     ▼
      complete_execution: EXECUTED (incident VERIFYING, VerificationRequested)
                        | bounded retry (retry-safe + retryable + attempts left)
                        | FAILED (incident ESCALATED)
```

### Action catalog (`packages/remediation/catalog.py`, `catalog-2026.09-1`)

| Action | Parameters | Tier (max) | Timeout | Attempts | Retry-safe | Verification |
|---|---|---|---|---|---|---|
| `restart_service` | service | 1 (1) | 120 s | 2 | yes | error_rate ≤ 5% for 300 s |
| `scale_service` | service, increase_by 1–5 | 1, 2 if > 2 (2) | 180 s | 1 | **no** (a repeat adds twice) | latency_p95 ≤ 1 s |
| `rollback_deployment` | service, from_version, to_version | 2 (2) | 300 s | 2 | yes (compare-and-set on from_version) | error_rate ≤ 5% |
| `disable_feature_flag` | service, flag | 1 (1) | 60 s | 2 | yes | error_rate ≤ 5% |
| `revert_configuration` | service, key, from_value, to_value | 2 (2) | 120 s | 2 | yes (compare-and-set on from_value) | error_rate ≤ 5% |

Every entry: allowed environments `production`, `staging`; approval
mandatory; automatic execution never allowed; requires an accepted RCA; the
target must be the RCA's root-cause component. Parameters are strict
pydantic models (`extra="forbid"`, patterns on every string), validated at
proposal, by policy, again before execution and again inside the executor.
The catalog is shipped as reviewed code with a version and a content digest
recorded on every decision (ADR-0024); nothing at runtime can add or widen
an action.

### Policy (`packages/policy/engine.py`, `policy-2026.09-1`)

`evaluate(proposal, entry, policy, context) -> PolicyDecision` imports no
I/O (boundary test). Every rule runs and reports; any deny → DENY, else
REQUIRE_APPROVAL. The current policy has no automatic-execution
environments, and every catalog entry forbids automatic execution, so ALLOW
is unreachable; incident-core would treat it as REQUIRE_APPROVAL anyway.

| Rule | Denies when |
|---|---|
| kill_switch.global / kill_switch.service | a kill switch is engaged |
| action.known | the action isn't in the catalog |
| action.parameters | parameters fail the entry's schema |
| target.known | the target isn't a catalogued service |
| target.in_incident_scope | the target isn't the incident's service or a direct neighbour |
| environment.not_prohibited | the environment is prohibited by policy |
| environment.allowed_for_action | the action isn't allowed in the environment |
| blast_radius.action_max / .environment_max | the invocation's tier exceeds the action's or the environment's maximum (production: 2) |
| incident.remediable_status | the incident isn't RCA_READY |
| rca.required | no completed investigation with an accepted RCA |
| rca.target_matches_root_cause | the target isn't the RCA's root-cause component |
| attempts.per_incident | ≥ 2 remediations already attempted for the incident |
| attempts.per_service_per_hour | ≥ 3 executions on the target in the last hour |
| approval.required | (never denies) → REQUIRE_APPROVAL; roles: tier 1 on_call_engineer or service_owner, tier 2 service_owner |

`PolicyEvaluationContext` (built by incident-core, stored with the
decision): incident id/environment/severity/service/status, investigation
status, whether an accepted RCA exists and its cause category/component,
target, target known / in scope, blast-radius tier, attempted remediations
for the incident, executions on the target in the last hour, both kill
switches, proposal source. Model confidence is not in it. A stored decision
re-evaluates to the same result (tested).

### Approval

- An approval names the `proposal_hash` (incident, investigation, action,
  catalog version, parameters, environment) and `policy_decision_id` the
  approver reviewed; anything else is refused (409). Proposal columns are
  immutable in the database, so a changed proposal is necessarily a new
  remediation (`revise` cancels the old one as superseded) with a new
  decision and a new approval.
- The approver must be on the operator roster and hold a role the decision
  requires; roles come from the roster, never from the request. An operator
  cannot approve their own proposal.
- One decision per remediation (unique): a redelivered identical decision
  is a no-op, a conflicting one is refused.
- Rejection → CANCELLED, incident ESCALATED. No decision within
  `REMEDIATION_APPROVAL_TIMEOUT_MINUTES` (30) → `timed_out`, CANCELLED,
  incident ESCALATED. Silence never approves.
- Approval only makes a remediation eligible; nothing can override a
  policy rejection.

### Executor boundary

`RemediationExecutor` (`execute(request) -> ExecutorResult`,
`inspect(idempotency_key)`) is called only by `RemediationRunner`, only for
an execution attempt incident-core has just authorized.
`SimulatorRemediationExecutor` performs one catalog action on the simulated
environment through its control surfaces: the service's fault state
(`chaos:{service}`, polled by the running services) and the simulated
deployment / config registries (records say `deployed_by:
remediation-executor`). Rollback and config revert are compare-and-set
(the running version / current value must match, or it fails "target
changed" without acting); a repeat after success is a no-op. Restart clears
process-level faults (memory leak, CPU burn); scale records desired
replicas (≤ 10); flags are set off. Every action is logged to
`sim:ops:{service}` and its result stored under the idempotency key. No
shell, Docker, Kubernetes or database access (boundary test).

### Failure handling and recovery

| Situation | Behaviour |
|---|---|
| policy rejection | POLICY_REJECTED (terminal), incident ESCALATED |
| approval rejected / timed out | CANCELLED, incident ESCALATED |
| operator withdraws | CANCELLED, incident back to RCA_READY |
| kill switch engaged before execution | claim refuses: CANCELLED, incident ESCALATED; nothing runs |
| catalog changed / parameters invalid / target unknown / attempts exhausted at claim | FAILED, incident ESCALATED |
| target changed (someone deployed) | executor fails without acting (compare-and-set), FAILED |
| executor failure, retryable, retry-safe action, attempts left | new attempt (new idempotency key) |
| otherwise | FAILED, incident ESCALATED |
| timeout | TIMED_OUT; retried only for retry-safe actions, within attempts; `scale_service` never |
| duplicate RemediationApproved / execution request | claim finds it settled; the executor returns its stored result |
| worker dies mid-execution | lease expires; the next claim marks the attempt UNKNOWN and returns a reconcile ticket; the runner asks the executor about that idempotency key: applied → EXECUTED (reconciled), not applied → retry only if retry-safe |
| lost event | the worker's sweep picks up APPROVED and lease-expired EXECUTING remediations |

A kill switch cannot stop an attempt already running (documented limit);
it stops every later claim.

### Audit trail

`remediation_timeline` (immutable) records every step -- proposed,
policy_evaluated, approval_requested, approved / rejected /
approval_timed_out, execution_started, execution_attempt_failed,
execution_interrupted, executed / failed / cancelled,
verification_requested -- each with actor, timestamp, correlation id,
action id, catalog version and policy version. Policy decisions and
approvals are immutable rows; proposal columns are immutable; every
transition also emits an outbox event.
