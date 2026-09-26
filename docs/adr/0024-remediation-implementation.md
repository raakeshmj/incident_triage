# ADR-0024: Remediation as built -- catalog and policy as versioned code, approval binding, no automatic path

Status: Accepted

## Context

09-remediation-policy-boundaries.md and ADRs 0007-0009/0012 designed the
remediation path. Implementing it (Phase 7) required several concrete
choices the design left open or sketched differently.

## Decision

1. **Catalog and policy ship as reviewed, versioned code**
   (`packages/remediation/catalog.py`, `packages/policy/engine.py`), not
   as `action_catalog` / `policies` tables populated by CI/CD. They are
   still immutable at runtime and still recorded on every decision (catalog
   version + content digest, policy version). A table-backed catalog is a
   later change if catalogs need to vary per deployment.
2. **One `remediations` aggregate** with its own lifecycle (PROPOSED,
   POLICY_REJECTED, AWAITING_APPROVAL, APPROVED, EXECUTING, EXECUTED,
   FAILED, CANCELLED) plus `remediation_policy_decisions`,
   `remediation_approvals`, `remediation_executions` and an immutable
   `remediation_timeline` -- the design's `remediation_proposals` /
   `policy_decisions` / `approvals` / `executions`, renamed under one
   prefix. Proposal columns are immutable by trigger.
3. **Approval binds to `proposal_hash` + `policy_decision_id`.** A changed
   proposal is a new remediation.
4. **No automatic execution in Phase 7.** The policy has no
   auto-execution environments, every catalog entry forbids it, and
   incident-core treats ALLOW like REQUIRE_APPROVAL.
5. **DENY escalates the incident** (the state machine's RCA_READY →
   ESCALATED edge); a withdrawn proposal returns it to RCA_READY (new
   edge AWAITING_APPROVAL → RCA_READY).
6. **Execution is claimed, leased and reconciled**, not fire-and-forget:
   attempts are rows with idempotency keys; a lost worker's attempt becomes
   UNKNOWN and is resolved by asking the executor, never assumed; retries
   only for retry-safe actions.
7. **Operator authentication is a stand-in**: a shared operator token and a
   static approver roster (roles), pending a real identity provider.

## Consequences

- Every remediation is reconstructible from its rows: proposal, the exact
  context and rules that decided it, who approved which hash, each attempt
  and its result.
- Adding an action is a code change with review (catalog entry, executor
  handler, tests), as ADR-0008 intended.
- Verification after execution is requested (event + reference) but not
  performed yet; incidents wait in VERIFYING.
