"""Policy engine: evaluate(proposal, action_catalog_entry, policy, context).

Pure. No database, network, clock, randomness or model: every dynamic fact
arrives in the `PolicyEvaluationContext` incident-core built and will store
verbatim with the decision, so re-running `evaluate` on a stored decision's
inputs reproduces it exactly.

Every rule runs and reports (pass / deny / require_approval); the decision
is DENY if any rule denies, otherwise REQUIRE_APPROVAL. Policy
`policy-2026.09-1` never yields ALLOW: there is no automatic-execution path
in Phase 7, whatever an action's catalog entry says (docs/architecture/
09-remediation-policy-boundaries.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.domain.remediation import (
    PolicyDecision,
    PolicyEvaluationContext,
    RemediationProposal,
    RuleResult,
)
from packages.remediation.catalog import CATALOG_VERSION, ActionCatalogEntry


@dataclass(frozen=True)
class Policy:
    version: str
    prohibited_environments: frozenset[str] = frozenset()
    max_blast_radius_by_environment: dict[str, int] = field(default_factory=dict)
    max_attempted_remediations_per_incident: int = 2
    max_executions_per_service_per_hour: int = 3
    approver_roles_by_tier: dict[int, tuple[str, ...]] = field(default_factory=dict)
    # Environments where an ALLOW decision is possible at all. Empty: every
    # permitted remediation needs a human.
    auto_execution_environments: frozenset[str] = frozenset()
    remediable_incident_statuses: frozenset[str] = frozenset({"RCA_READY"})


DEFAULT_POLICY = Policy(
    version="policy-2026.09-1",
    prohibited_environments=frozenset(),
    max_blast_radius_by_environment={"production": 2, "staging": 2},
    max_attempted_remediations_per_incident=2,
    max_executions_per_service_per_hour=3,
    approver_roles_by_tier={
        1: ("on_call_engineer", "service_owner"),
        2: ("service_owner",),
    },
    auto_execution_environments=frozenset(),
)


def evaluate(
    proposal: RemediationProposal,
    entry: ActionCatalogEntry | None,
    policy: Policy,
    context: PolicyEvaluationContext,
) -> PolicyDecision:
    rules: list[RuleResult] = []

    def rule(rule_id: str, ok: bool, detail: str, *, on_fail: str = "deny") -> None:
        rules.append(
            RuleResult(rule_id=rule_id, outcome="pass" if ok else on_fail, detail=detail)  # type: ignore[arg-type]
        )

    rule(
        "kill_switch.global",
        not context.global_kill_switch_engaged,
        "global remediation kill switch is engaged"
        if context.global_kill_switch_engaged
        else "global kill switch released",
    )
    rule(
        "kill_switch.service",
        not context.service_kill_switch_engaged,
        f"kill switch engaged for {context.target_service}"
        if context.service_kill_switch_engaged
        else "service kill switch released",
    )
    rule(
        "action.known",
        entry is not None,
        f"action {proposal.action_id!r} is in catalog {CATALOG_VERSION}"
        if entry
        else f"unknown action {proposal.action_id!r}: not in catalog {CATALOG_VERSION}",
    )
    tier: int | None = None
    if entry is not None:
        params, problems = entry.validate(proposal.parameters)
        rule(
            "action.parameters",
            params is not None,
            "parameters valid" if params else "invalid parameters: " + "; ".join(problems),
        )
        if params is not None:
            tier = entry.blast_radius(params)
        rule(
            "action.auto_execution_not_requested",
            True,
            "catalog entry permits automatic execution: "
            f"{entry.auto_execution_allowed} (not used by this policy)",
        )

    rule(
        "target.known",
        context.target_known,
        f"target {context.target_service!r} is "
        + ("a catalogued service" if context.target_known else "not a known service"),
    )
    rule(
        "target.in_incident_scope",
        context.target_in_incident_scope,
        f"target {context.target_service!r} is "
        + (
            "within the incident's service scope"
            if context.target_in_incident_scope
            else f"outside the scope of the incident on {context.incident_service}"
        ),
    )

    env = context.incident_environment
    rule(
        "environment.not_prohibited",
        env not in policy.prohibited_environments,
        f"environment {env!r} "
        + ("is prohibited" if env in policy.prohibited_environments else "permitted by policy"),
    )
    if entry is not None:
        rule(
            "environment.allowed_for_action",
            env in entry.allowed_environments,
            f"{entry.action_id} "
            + ("allowed" if env in entry.allowed_environments else "not allowed")
            + f" in {env!r}",
        )
    if tier is not None and entry is not None:
        env_max = policy.max_blast_radius_by_environment.get(env, 0)
        rule(
            "blast_radius.action_max",
            tier <= entry.max_blast_radius,
            f"blast radius {tier} vs {entry.action_id} maximum {entry.max_blast_radius}",
        )
        rule(
            "blast_radius.environment_max",
            tier <= env_max,
            f"blast radius {tier} vs {env!r} maximum {env_max}",
        )

    rule(
        "incident.remediable_status",
        context.incident_status in policy.remediable_incident_statuses,
        f"incident is {context.incident_status}",
    )
    if entry is not None and entry.requires_rca:
        rule(
            "rca.required",
            context.rca_available,
            "a completed investigation with an accepted RCA exists"
            if context.rca_available
            else f"{entry.action_id} requires an accepted RCA; none exists",
        )
        if entry.target_must_match_root_cause and context.rca_available:
            rule(
                "rca.target_matches_root_cause",
                context.target_service == context.rca_component,
                f"target {context.target_service!r} vs root-cause component "
                f"{context.rca_component!r}",
            )

    rule(
        "attempts.per_incident",
        context.attempted_remediations_this_incident
        < policy.max_attempted_remediations_per_incident,
        f"{context.attempted_remediations_this_incident} remediation(s) already attempted "
        f"for this incident (limit {policy.max_attempted_remediations_per_incident})",
    )
    rule(
        "attempts.per_service_per_hour",
        context.remediation_executions_last_hour_for_target
        < policy.max_executions_per_service_per_hour,
        f"{context.remediation_executions_last_hour_for_target} execution(s) on "
        f"{context.target_service} in the last hour "
        f"(limit {policy.max_executions_per_service_per_hour})",
    )

    roles = list(policy.approver_roles_by_tier.get(tier or 0, ()))
    automatic = (
        env in policy.auto_execution_environments
        and entry is not None
        and entry.auto_execution_allowed
        and not entry.approval_mandatory
    )
    rule(
        "approval.required",
        automatic,
        "automatic execution permitted"
        if automatic
        else f"human approval required ({' or '.join(roles) or 'no eligible role'})",
        on_fail="require_approval",
    )
    if not roles:
        rule("approval.eligible_role_exists", False, f"no approver role is defined for tier {tier}")

    if any(r.outcome == "deny" for r in rules):
        decision = "DENY"
    elif any(r.outcome == "require_approval" for r in rules):
        decision = "REQUIRE_APPROVAL"
    else:
        decision = "ALLOW"
    return PolicyDecision(
        decision=decision,  # type: ignore[arg-type]
        policy_version=policy.version,
        catalog_version=CATALOG_VERSION if entry is not None else None,
        blast_radius_tier=tier,
        required_approver_roles=roles if decision == "REQUIRE_APPROVAL" else [],
        rules=rules,
    )
