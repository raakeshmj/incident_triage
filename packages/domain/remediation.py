"""Remediation domain types (Phase 7): pure, no I/O.

A remediation is one proposed catalog action against one target, from
proposal through policy, approval and execution. Its proposal is immutable
once recorded and fingerprinted (`proposal_hash`); an approval binds to that
hash and to the policy decision it approved. Changing anything means a new
proposal, a new decision and a new approval.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RemediationStatus(str, Enum):
    PROPOSED = "PROPOSED"
    POLICY_REJECTED = "POLICY_REJECTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_REMEDIATION_STATUSES = frozenset(
    {
        RemediationStatus.POLICY_REJECTED,
        RemediationStatus.EXECUTED,
        RemediationStatus.FAILED,
        RemediationStatus.CANCELLED,
    }
)
# Statuses that count as "a remediation was actually attempted".
ATTEMPTED_REMEDIATION_STATUSES = frozenset(
    {RemediationStatus.EXECUTING, RemediationStatus.EXECUTED, RemediationStatus.FAILED}
)

ALLOWED_TRANSITIONS: dict[RemediationStatus, frozenset[RemediationStatus]] = {
    RemediationStatus.PROPOSED: frozenset(
        {RemediationStatus.POLICY_REJECTED, RemediationStatus.AWAITING_APPROVAL}
    ),
    RemediationStatus.AWAITING_APPROVAL: frozenset(
        {RemediationStatus.APPROVED, RemediationStatus.CANCELLED}
    ),
    # APPROVED -> FAILED: a pre-execution re-check failed (catalog/target/attempts)
    RemediationStatus.APPROVED: frozenset(
        {RemediationStatus.EXECUTING, RemediationStatus.CANCELLED, RemediationStatus.FAILED}
    ),
    # EXECUTING -> EXECUTING: a further (bounded) attempt; -> CANCELLED: a kill
    # switch engaged between attempts (never while one is running)
    RemediationStatus.EXECUTING: frozenset(
        {
            RemediationStatus.EXECUTED,
            RemediationStatus.FAILED,
            RemediationStatus.EXECUTING,
            RemediationStatus.CANCELLED,
        }
    ),
}


def can_transition(from_status: RemediationStatus, to_status: RemediationStatus) -> bool:
    return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())


class ExecutionStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"  # the worker died mid-call; reconciled, never assumed


class RemediationProposal(BaseModel):
    """What any proposer submits: the deterministic planner, an operator, or
    (later) a model's structured recommendation. Data only -- validated,
    resolved against the catalog and evaluated by application code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: uuid.UUID
    investigation_id: uuid.UUID | None = None
    action_id: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any]
    reason: str = Field(min_length=1, max_length=1000)
    expected_effect: str = Field(min_length=1, max_length=1000)
    source: Literal["planner", "operator", "model"]
    proposed_by: str = Field(min_length=1, max_length=128)

    @property
    def target_service(self) -> str | None:
        value = self.parameters.get("service")
        return value if isinstance(value, str) else None


def proposal_hash(
    *,
    incident_id: uuid.UUID,
    investigation_id: uuid.UUID | None,
    action_id: str,
    catalog_version: str,
    parameters: dict[str, Any],
    environment: str,
) -> str:
    """The identity an approval binds to: exactly what would run, where."""
    body = json.dumps(
        {
            "incident_id": str(incident_id),
            "investigation_id": str(investigation_id) if investigation_id else None,
            "action_id": action_id,
            "catalog_version": catalog_version,
            "parameters": parameters,
            "environment": environment,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


class PolicyEvaluationContext(BaseModel):
    """Every dynamic fact a policy rule may condition on (ADR-0012), built
    by incident-core immediately before evaluation and stored verbatim with
    the decision. Model confidence is deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_id: uuid.UUID
    captured_at: datetime
    incident_id: uuid.UUID
    incident_environment: str
    incident_severity: str
    incident_service: str
    incident_status: str
    investigation_status: str | None
    rca_available: bool
    rca_cause_category: str | None
    rca_component: str | None
    target_service: str | None
    target_known: bool
    target_in_incident_scope: bool
    action_blast_radius_tier: int | None
    attempted_remediations_this_incident: int
    remediation_executions_last_hour_for_target: int
    global_kill_switch_engaged: bool
    service_kill_switch_engaged: bool
    proposal_source: str


class RuleResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    outcome: Literal["pass", "deny", "require_approval"]
    detail: str


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    policy_version: str
    catalog_version: str | None
    blast_radius_tier: int | None
    required_approver_roles: list[str]
    rules: list[RuleResult]

    @property
    def reasons(self) -> list[str]:
        return [r.detail for r in self.rules if r.outcome != "pass"]

    @property
    def denying_rules(self) -> list[str]:
        return [r.rule_id for r in self.rules if r.outcome == "deny"]


class RemediationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    incident_id: uuid.UUID
    investigation_id: uuid.UUID | None
    action_id: str
    catalog_version: str
    parameters: dict[str, Any]
    target_service: str
    environment: str
    reason: str
    expected_effect: str
    blast_radius_tier: int | None
    proposal_hash: str
    source: str
    proposed_by: str
    status: RemediationStatus
    policy_decision_id: uuid.UUID | None
    policy_decision: str | None
    approval_status: str | None
    execution_status: str | None
    execution_attempts: int
    executor_result: dict[str, Any] | None
    failure_reason: str | None
    verification_ref: uuid.UUID | None
    correlation_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
