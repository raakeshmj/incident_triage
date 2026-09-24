# 09 — Remediation / Policy Boundaries

## The shape of the boundary

```
investigation-agent            (proposes: action_catalog_id + parameters — data, not code)
        │
        ▼
incident-core                  (persists RemediationProposal, calls policy-engine)
        │
        ▼
policy-engine                  (deterministic: ALLOW | DENY | REQUIRE_APPROVAL, versioned)
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

- **Pure function**: `(RemediationProposal, ActionCatalogEntry, active Policy version) → PolicyDecision`. No side effects, no external calls, no LLM involvement anywhere in this path.
- **Versioned and immutable**: `policy_decisions` records the exact
  `policy_version` used. Changing policy tomorrow does not change the
  interpretation of yesterday's decision — required for audit and for
  eval-harness replay to be meaningful.
- **Implementation**: a small rule DSL evaluated in Python (or OPA/Rego if
  the rule surface grows — deferred, see ADR-0007) — deliberately not a
  general-purpose scripting language, so every possible policy outcome can
  be enumerated and tested.

Example rules (illustrative, not final syntax):

```
DENY   if action.blast_radius_tier >= 3
DENY   if incident.environment == "prod" and action.id not in prod_allowed_actions
REQUIRE_APPROVAL(roles=["on_call_engineer", "service_owner"])
       if incident.environment == "prod" and action.blast_radius_tier >= 1
ALLOW  if incident.environment == "staging" and action.blast_radius_tier <= 1
DENY   if remediation_rate(incident.service, window="1h") >= 3   # runaway-loop breaker
DENY   if global_kill_switch.enabled
```

Notably: **model-reported `confidence` never appears in a policy rule.**
Policy only ever conditions on the deterministic properties of the
proposed action and the incident's environment/history.

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
- **Kill switch**: a single global (and per-service) flag checked by
  `policy-engine` before every decision — flipping it to "frozen" makes
  every proposal evaluate to `DENY` immediately, independent of any other
  rule. This is the manual "stop the robot" control.
- **Rate limiting**: policy denies further remediation for a
  service/incident once a short-window remediation count is exceeded
  (runaway-loop breaker — see `review/critical-review.md`).

## Dry-run mode

Every `action_catalog` adapter supports a `dry_run` flag that validates
parameters and simulates the call (e.g. a Kubernetes dry-run apply) without
side effects. Used by: the eval harness (`11-evaluation-architecture.md`),
staging rehearsal, and an optional "preview" step surfaced to the approver
in the web UI before they approve.
