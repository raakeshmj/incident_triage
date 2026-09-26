"""The policy engine: pure, deterministic, replayable; every safety rule
exercised with plain values. No database, network or clock."""

from __future__ import annotations

import ast
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.domain.remediation import PolicyEvaluationContext, RemediationProposal
from packages.policy.engine import DEFAULT_POLICY, evaluate
from packages.remediation.catalog import CATALOG, get_entry

INCIDENT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _proposal(action="rollback_deployment", **params) -> RemediationProposal:
    defaults = {
        "rollback_deployment": {
            "service": "checkout-service",
            "from_version": "1.1.0-bad",
            "to_version": "1.0.0",
        },
        "scale_service": {"service": "checkout-service", "increase_by": 2},
        "restart_service": {"service": "checkout-service"},
    }.get(action, {"service": "checkout-service"})
    return RemediationProposal(
        incident_id=INCIDENT,
        action_id=action,
        parameters={**defaults, **params},
        reason="r",
        expected_effect="e",
        source="planner",
        proposed_by="remediation-planner",
    )


def _context(proposal: RemediationProposal, **overrides) -> PolicyEvaluationContext:
    entry = get_entry(proposal.action_id)
    params, _ = entry.validate(proposal.parameters) if entry else (None, [])
    base = {
        "context_id": uuid.UUID(int=7),
        "captured_at": datetime(2026, 9, 1, tzinfo=UTC),
        "incident_id": INCIDENT,
        "incident_environment": "production",
        "incident_severity": "critical",
        "incident_service": "checkout-service",
        "incident_status": "RCA_READY",
        "investigation_status": "COMPLETED",
        "rca_available": True,
        "rca_cause_category": "deployment",
        "rca_component": "checkout-service",
        "target_service": proposal.target_service,
        "target_known": True,
        "target_in_incident_scope": True,
        "action_blast_radius_tier": entry.blast_radius(params) if entry and params else None,
        "attempted_remediations_this_incident": 0,
        "remediation_executions_last_hour_for_target": 0,
        "global_kill_switch_engaged": False,
        "service_kill_switch_engaged": False,
        "proposal_source": proposal.source,
    }
    return PolicyEvaluationContext(**{**base, **overrides})


def _decide(proposal=None, policy=DEFAULT_POLICY, **context):
    proposal = proposal or _proposal()
    return evaluate(proposal, get_entry(proposal.action_id), policy, _context(proposal, **context))


def test_a_valid_proposal_requires_approval_by_the_right_role():
    decision = _decide()
    assert decision.decision == "REQUIRE_APPROVAL"
    assert decision.blast_radius_tier == 2
    assert decision.required_approver_roles == ["service_owner"]
    assert decision.denying_rules == []
    assert _decide(
        _proposal("restart_service"), rca_cause_category="resource_memory"
    ).required_approver_roles == [
        "on_call_engineer",
        "service_owner",
    ]


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"global_kill_switch_engaged": True}, "kill_switch.global"),
        ({"service_kill_switch_engaged": True}, "kill_switch.service"),
        ({"target_known": False}, "target.known"),
        ({"target_in_incident_scope": False}, "target.in_incident_scope"),
        ({"incident_status": "INVESTIGATING"}, "incident.remediable_status"),
        ({"rca_available": False}, "rca.required"),
        ({"rca_component": "payment-service"}, "rca.target_matches_root_cause"),
        ({"attempted_remediations_this_incident": 2}, "attempts.per_incident"),
        ({"remediation_executions_last_hour_for_target": 3}, "attempts.per_service_per_hour"),
        ({"incident_environment": "development"}, "environment.allowed_for_action"),
    ],
)
def test_each_safety_rule_denies_on_its_own(overrides, rule):
    decision = _decide(**overrides)
    assert decision.decision == "DENY"
    assert rule in decision.denying_rules
    assert decision.required_approver_roles == []


def test_unknown_actions_are_denied():
    proposal = _proposal("delete_database", service="checkout-service")
    decision = evaluate(proposal, None, DEFAULT_POLICY, _context(proposal))
    assert decision.decision == "DENY" and "action.known" in decision.denying_rules
    assert decision.catalog_version is None


def test_invalid_or_extra_parameters_are_denied():
    assert "action.parameters" in _decide(_proposal(to_version="$(rm -rf /)")).denying_rules
    assert "action.parameters" in _decide(_proposal(command="kubectl delete ns")).denying_rules
    assert "action.parameters" in _decide(_proposal("scale_service", increase_by=50)).denying_rules


def test_prohibited_environments_and_excessive_blast_radius_are_denied():
    prod_locked = replace(DEFAULT_POLICY, prohibited_environments=frozenset({"production"}))
    assert "environment.not_prohibited" in _decide(policy=prod_locked).denying_rules
    tight = replace(DEFAULT_POLICY, max_blast_radius_by_environment={"production": 1})
    assert "blast_radius.environment_max" in _decide(policy=tight).denying_rules
    big_scale = _proposal("scale_service", increase_by=4)  # tier 2
    assert _decide(big_scale, rca_cause_category="traffic").blast_radius_tier == 2
    assert "blast_radius.environment_max" in _decide(big_scale, policy=tight).denying_rules


def test_the_current_policy_never_allows_automatic_execution():
    everywhere = replace(DEFAULT_POLICY, auto_execution_environments=frozenset({"production"}))
    for action in CATALOG:
        proposal = (
            _proposal(action)
            if action in ("rollback_deployment", "scale_service", "restart_service")
            else None
        )
        if proposal is None:
            continue
        assert _decide(proposal).decision == "REQUIRE_APPROVAL"
        # even a policy that permits auto-execution can't: every catalog entry
        # makes approval mandatory and forbids automatic execution
        assert _decide(proposal, policy=everywhere).decision == "REQUIRE_APPROVAL"
    assert all(e.approval_mandatory and not e.auto_execution_allowed for e in CATALOG.values())


def test_evaluation_is_deterministic_and_replayable_from_the_stored_context():
    proposal = _proposal()
    context = _context(proposal)
    first = evaluate(proposal, get_entry(proposal.action_id), DEFAULT_POLICY, context)
    stored = context.model_dump(mode="json")  # what incident-core persists
    replayed = evaluate(
        proposal,
        get_entry(proposal.action_id),
        DEFAULT_POLICY,
        PolicyEvaluationContext.model_validate(stored),
    )
    assert replayed == first


def test_model_confidence_is_not_a_policy_input():
    fields = set(PolicyEvaluationContext.model_fields)
    assert not any("confidence" in f for f in fields)


def test_the_policy_engine_is_pure_by_import():
    source = Path("packages/policy/engine.py").read_text()
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    for forbidden in (
        "sqlalchemy",
        "redis",
        "httpx",
        "requests",
        "socket",
        "anthropic",
        "subprocess",
        "os",
        "time",
    ):
        assert forbidden not in imported
    assert "packages.incident" not in source and "datetime.now" not in source
