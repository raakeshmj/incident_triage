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
