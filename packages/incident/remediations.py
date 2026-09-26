"""incident-core's remediation commands (Phase 7): the only writer of
remediations, policy decisions, approvals, executions, the remediation
timeline and kill switches.

    propose ──▶ PROPOSED ──(policy, pure)──▶ POLICY_REJECTED        (incident -> ESCALATED)
                                  └───────▶ AWAITING_APPROVAL      (incident -> AWAITING_APPROVAL)
    decide_approval ──▶ APPROVED (incident -> REMEDIATION_IN_PROGRESS) | CANCELLED (-> ESCALATED)
    claim_execution ──▶ EXECUTING (execution attempt row, idempotency key, lease)
    complete_execution ──▶ EXECUTED (incident -> VERIFYING, VerificationRequested)
                         | retry (bounded, retry-safe actions only) | FAILED (-> ESCALATED)

Every command is one transaction: row lock, guard, state change, timeline
entry, outbox event. The policy engine is called with a context this
module builds from the database immediately before the call and stores
verbatim with the decision (ADR-0012). No model, and no model output, can
reach past `propose()`: everything after it is these commands, driven by
deterministic workers and humans.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.domain.enums import IncidentStatus
from packages.domain.errors import (
    ApprovalMismatchError,
    ApproverNotAuthorizedError,
    ConcurrentModificationError,
    IncidentNotFoundError,
    LeaseLostError,
    RemediationNotFoundError,
    RemediationStateError,
)
from packages.domain.events import (
    AGGREGATE_TYPE_INCIDENT,
    AGGREGATE_TYPE_KILL_SWITCH,
    AGGREGATE_TYPE_REMEDIATION,
    EVENT_TYPE_INCIDENT_STATUS_CHANGED,
    EVENT_TYPE_KILL_SWITCH_CHANGED,
    EVENT_TYPE_REMEDIATION_APPROVAL_REQUESTED,
    EVENT_TYPE_REMEDIATION_APPROVED,
    EVENT_TYPE_REMEDIATION_CANCELLED,
    EVENT_TYPE_REMEDIATION_EXECUTED,
    EVENT_TYPE_REMEDIATION_FAILED,
    EVENT_TYPE_REMEDIATION_POLICY_EVALUATED,
    EVENT_TYPE_REMEDIATION_PROPOSED,
    EVENT_TYPE_REMEDIATION_REJECTED,
    EVENT_TYPE_REMEDIATION_STARTED,
    EVENT_TYPE_VERIFICATION_REQUESTED,
    PRODUCER_INCIDENT_CORE,
    IncidentStatusChangedPayload,
    KillSwitchChangedPayload,
    RemediationEventPayload,
    VerificationRequestedPayload,
)
from packages.domain.investigation import InvestigationStatus
from packages.domain.remediation import (
    ATTEMPTED_REMEDIATION_STATUSES,
    ExecutionStatus,
    PolicyDecision,
    PolicyEvaluationContext,
    RemediationProposal,
    RemediationStatus,
    RemediationView,
    can_transition,
    proposal_hash,
)
from packages.incident import repository
from packages.incident.db.models import (
    IncidentRow,
    InvestigationRow,
    KillSwitchRow,
    RcaReportRow,
    RemediationApprovalRow,
    RemediationExecutionRow,
    RemediationPolicyDecisionRow,
    RemediationRow,
    RemediationTimelineRow,
)
from packages.policy.engine import DEFAULT_POLICY, Policy, evaluate
from packages.remediation.catalog import (
    CATALOG_VERSION,
    ActionCatalogEntry,
    catalog_digest,
    get_entry,
)
from packages.telemetry.logging import get_logger
from packages.telemetry.metrics import get_metrics

log = get_logger(__name__)
metrics = get_metrics()

GLOBAL_SCOPE = "global"


def service_scope(service: str) -> str:
    return f"service:{service}"


class ServiceTopology(Protocol):
    """What the context builder needs from the service catalog."""

    @property
    def services(self) -> Any: ...

    def neighborhood(self, service: str) -> frozenset[str]: ...


@dataclass(frozen=True)
class ExecutionTicket:
    """What a runner needs to act on one claimed execution attempt.
    `mode="reconcile"`: a previous worker died mid-call; ask the executor
    what happened to `idempotency_key` before doing anything else."""

    mode: str  # "execute" | "reconcile"
    remediation: RemediationView
    execution_id: uuid.UUID
    attempt: int
    idempotency_key: str
    deadline: datetime
    action_id: str
    catalog_version: str
    parameters: dict[str, Any]
    target_service: str
    environment: str


class RemediationCoreService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        topology: ServiceTopology,
        policy: Policy = DEFAULT_POLICY,
        approval_timeout: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._topology = topology
        self._policy = policy
        self._approval_timeout = approval_timeout
        self._clock = clock

    @property
    def policy(self) -> Policy:
        return self._policy

    # --- proposal + policy ------------------------------------------------------------

    def propose(
        self,
        proposal: RemediationProposal,
        *,
        idempotency_key: str,
        correlation_id: uuid.UUID | None = None,
    ) -> RemediationView:
        """Record a proposal, evaluate policy on it, and route it: rejected,
        or awaiting a human. Idempotent per (incident, idempotency_key)."""
        with self._session_factory() as session:
            incident = self._lock_incident(session, proposal.incident_id)
            existing = session.execute(
                select(RemediationRow).where(
                    RemediationRow.incident_id == proposal.incident_id,
                    RemediationRow.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return self._view(session, existing)

            now = self._clock()
            entry = get_entry(proposal.action_id)
            params, _ = entry.validate(proposal.parameters) if entry else (None, [])
            tier = entry.blast_radius(params) if entry and params else None
            row = RemediationRow(
                id=uuid.uuid4(),
                incident_id=incident.id,
                investigation_id=proposal.investigation_id,
                idempotency_key=idempotency_key,
                action_id=proposal.action_id,
                catalog_version=CATALOG_VERSION,
                parameters=proposal.parameters,
                target_service=proposal.target_service or "",
                environment=incident.environment,
                reason=proposal.reason,
                expected_effect=proposal.expected_effect,
                blast_radius_tier=tier,
                proposal_hash=proposal_hash(
                    incident_id=incident.id,
                    investigation_id=proposal.investigation_id,
                    action_id=proposal.action_id,
                    catalog_version=CATALOG_VERSION,
                    parameters=proposal.parameters,
                    environment=incident.environment,
                ),
                source=proposal.source,
                proposed_by=proposal.proposed_by,
                status=RemediationStatus.PROPOSED.value,
                execution_attempts=0,
                correlation_id=correlation_id or incident.id,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            actor = f"{proposal.source}:{proposal.proposed_by}"
            self._timeline(
                session,
                row,
                "proposed",
                None,
                RemediationStatus.PROPOSED,
                actor,
                details={
                    "parameters": proposal.parameters,
                    "reason": proposal.reason,
                    "expected_effect": proposal.expected_effect,
                    "proposal_hash": row.proposal_hash,
                    "idempotency_key": idempotency_key,
                },
            )
            self._event(session, row, EVENT_TYPE_REMEDIATION_PROPOSED, actor)

            context = self._context(session, incident, proposal, entry, tier, now)
            decision = evaluate(proposal, entry, self._policy, context)
            decision_row = self._record_decision(session, row, decision, context, now)
            metrics.increment(
                "remediation.policy_decision", decision=decision.decision, action=row.action_id
            )

            if decision.decision == "DENY":
                self._transition(
                    session,
                    row,
                    RemediationStatus.POLICY_REJECTED,
                    "policy_rejected",
                    "policy-engine",
                    details={"denying_rules": decision.denying_rules, "reasons": decision.reasons},
                    policy_version=decision.policy_version,
                )
                row.failure_reason = "; ".join(decision.reasons)[:2000]
                row.completed_at = now
                self._event(
                    session, row, EVENT_TYPE_REMEDIATION_FAILED, "policy-engine", decision_row
                )
                self._move_incident(
                    session,
                    incident,
                    {IncidentStatus.RCA_READY},
                    IncidentStatus.ESCALATED,
                    "remediation_policy_rejected",
                )
            else:
                # ALLOW is treated exactly like REQUIRE_APPROVAL: Phase 7 has no
                # automatic-execution path (the current policy never allows one).
                self._transition(
                    session,
                    row,
                    RemediationStatus.AWAITING_APPROVAL,
                    "approval_requested",
                    "policy-engine",
                    details={"required_approver_roles": decision.required_approver_roles},
                    policy_version=decision.policy_version,
                )
                row.approval_status = "pending"
                self._event(
                    session,
                    row,
                    EVENT_TYPE_REMEDIATION_APPROVAL_REQUESTED,
                    "policy-engine",
                    decision_row,
                )
                self._move_incident(
                    session,
                    incident,
                    {IncidentStatus.RCA_READY},
                    IncidentStatus.AWAITING_APPROVAL,
                    "remediation_awaiting_approval",
                )
            session.commit()
            log.info(
                "remediation.proposed",
                remediation_id=str(row.id),
                incident_id=str(row.incident_id),
                action=row.action_id,
                decision=decision.decision,
                status=row.status,
            )
            return self._view(session, row)

    def _context(
        self,
        session: Session,
        incident: IncidentRow,
        proposal: RemediationProposal,
        entry: ActionCatalogEntry | None,
        tier: int | None,
        now: datetime,
    ) -> PolicyEvaluationContext:
        """Every fact policy may use, read here and nowhere else (ADR-0012)."""
        investigation = self._rca_investigation(session, incident.id, proposal.investigation_id)
        rca = (
            session.execute(
                select(RcaReportRow).where(RcaReportRow.investigation_id == investigation.id)
            ).scalar_one_or_none()
            if investigation is not None
            else None
        )
        root = (rca.report.get("root_cause_hypothesis") or {}) if rca else {}
        target = proposal.target_service
        attempted = session.execute(
            select(func.count())
            .select_from(RemediationRow)
            .where(
                RemediationRow.incident_id == incident.id,
                RemediationRow.status.in_([s.value for s in ATTEMPTED_REMEDIATION_STATUSES]),
            )
        ).scalar_one()
        recent = session.execute(
            select(func.count())
            .select_from(RemediationExecutionRow)
            .join(RemediationRow, RemediationRow.id == RemediationExecutionRow.remediation_id)
            .where(
                RemediationRow.target_service == (target or ""),
                RemediationExecutionRow.started_at >= now - timedelta(hours=1),
            )
        ).scalar_one()
        switches = self._engaged_scopes(session)
        return PolicyEvaluationContext(
            context_id=uuid.uuid4(),
            captured_at=now,
            incident_id=incident.id,
            incident_environment=incident.environment,
            incident_severity=incident.severity,
            incident_service=incident.service,
            incident_status=incident.status,
            investigation_status=investigation.status if investigation else None,
            rca_available=rca is not None
            and investigation is not None
            and investigation.status == InvestigationStatus.COMPLETED.value,
            rca_cause_category=root.get("cause_category"),
            rca_component=root.get("component"),
            target_service=target,
            target_known=bool(target) and target in self._topology.services,
            target_in_incident_scope=bool(target)
            and target in self._topology.neighborhood(incident.service),
            action_blast_radius_tier=tier,
            attempted_remediations_this_incident=int(attempted),
            remediation_executions_last_hour_for_target=int(recent),
            global_kill_switch_engaged=GLOBAL_SCOPE in switches,
            service_kill_switch_engaged=bool(target) and service_scope(target or "") in switches,
            proposal_source=proposal.source,
        )

    @staticmethod
    def _rca_investigation(
        session: Session, incident_id: uuid.UUID, investigation_id: uuid.UUID | None
    ) -> InvestigationRow | None:
        stmt = select(InvestigationRow).where(InvestigationRow.incident_id == incident_id)
        if investigation_id is not None:
            stmt = stmt.where(InvestigationRow.id == investigation_id)
        return session.execute(
            stmt.order_by(InvestigationRow.attempt_number.desc()).limit(1)
        ).scalar_one_or_none()

    def _record_decision(
        self,
        session: Session,
        row: RemediationRow,
        decision: PolicyDecision,
        context: PolicyEvaluationContext,
        now: datetime,
    ) -> RemediationPolicyDecisionRow:
        decision_row = RemediationPolicyDecisionRow(
            id=uuid.uuid4(),
            remediation_id=row.id,
            proposal_hash=row.proposal_hash,
            policy_version=decision.policy_version,
            catalog_version=decision.catalog_version,
            catalog_digest=catalog_digest(),
            decision=decision.decision,
            blast_radius_tier=decision.blast_radius_tier,
            required_approver_roles=decision.required_approver_roles,
            rules=[r.model_dump() for r in decision.rules],
            reasons=decision.reasons,
            policy_context=context.model_dump(mode="json"),
            evaluated_at=now,
        )
        session.add(decision_row)
        session.flush()
        row.policy_decision_id = decision_row.id
        self._timeline(
            session,
            row,
            "policy_evaluated",
            RemediationStatus.PROPOSED,
            RemediationStatus.PROPOSED,
            "policy-engine",
            details={
                "decision": decision.decision,
                "policy_decision_id": str(decision_row.id),
                "denying_rules": decision.denying_rules,
                "reasons": decision.reasons,
                "blast_radius_tier": decision.blast_radius_tier,
                "catalog_digest": decision_row.catalog_digest,
            },
            policy_version=decision.policy_version,
        )
        self._event(
            session,
            row,
            EVENT_TYPE_REMEDIATION_POLICY_EVALUATED,
            "policy-engine",
            decision_row,
        )
        return decision_row

    # --- approval ---------------------------------------------------------------------

    def decide_approval(
        self,
        remediation_id: uuid.UUID,
        *,
        approver: str,
        approver_roles: list[str],
        approve: bool,
        proposal_hash: str,
        policy_decision_id: uuid.UUID,
        comment: str | None = None,
    ) -> RemediationView:
        """A human's decision, bound to the exact proposal and policy decision
        the human saw. Duplicate identical decisions are idempotent; a
        different second decision is refused."""
        with self._session_factory() as session:
            row = self._lock(session, remediation_id)
            existing = session.execute(
                select(RemediationApprovalRow).where(
                    RemediationApprovalRow.remediation_id == remediation_id
                )
            ).scalar_one_or_none()
            wanted = "approved" if approve else "rejected"
            if existing is not None:
                if (
                    existing.approver == approver
                    and existing.decision == wanted
                    and existing.proposal_hash == proposal_hash
                ):
                    return self._view(session, row)  # duplicate delivery of the same decision
                raise RemediationStateError(
                    f"remediation {remediation_id} already has a decision ({existing.decision})"
                )
            if row.status != RemediationStatus.AWAITING_APPROVAL.value:
                raise RemediationStateError(
                    f"remediation {remediation_id} is {row.status}, not awaiting approval"
                )
            if proposal_hash != row.proposal_hash or policy_decision_id != row.policy_decision_id:
                raise ApprovalMismatchError(
                    "the approval does not match the current proposal and policy decision; "
                    "review the current proposal and approve that"
                )
            decision_row = session.get(RemediationPolicyDecisionRow, row.policy_decision_id)
            assert decision_row is not None
            eligible = [r for r in approver_roles if r in decision_row.required_approver_roles]
            if approve and not eligible:
                raise ApproverNotAuthorizedError(
                    f"{approver} holds {sorted(approver_roles)}; this remediation needs one of "
                    f"{decision_row.required_approver_roles}"
                )
            if approve and row.source == "operator" and row.proposed_by == approver:
                raise ApproverNotAuthorizedError("a proposer cannot approve their own proposal")

            now = self._clock()
            session.add(
                RemediationApprovalRow(
                    id=uuid.uuid4(),
                    remediation_id=row.id,
                    policy_decision_id=decision_row.id,
                    proposal_hash=row.proposal_hash,
                    decision=wanted,
                    approver=approver,
                    approver_role=eligible[0] if eligible else None,
                    comment=comment,
                    decided_at=now,
                )
            )
            session.flush()
            incident = self._lock_incident(session, row.incident_id)
            actor = f"human:{approver}"
            if approve:
                row.approval_status = "approved"
                self._transition(
                    session,
                    row,
                    RemediationStatus.APPROVED,
                    "approved",
                    actor,
                    details={"role": eligible[0], "comment": comment},
                    policy_version=decision_row.policy_version,
                )
                self._event(session, row, EVENT_TYPE_REMEDIATION_APPROVED, actor, decision_row)
                self._move_incident(
                    session,
                    incident,
                    {IncidentStatus.AWAITING_APPROVAL},
                    IncidentStatus.REMEDIATION_IN_PROGRESS,
                    "remediation_approved",
                )
            else:
                row.approval_status = "rejected"
                self._finish_cancelled(
                    session,
                    row,
                    incident,
                    event="rejected",
                    actor=actor,
                    reason=f"approval rejected by {approver}" + (f": {comment}" if comment else ""),
                    incident_to=IncidentStatus.ESCALATED,
                    event_type=EVENT_TYPE_REMEDIATION_REJECTED,
                    decision_row=decision_row,
                )
            session.commit()
            metrics.increment("remediation.approval", decision=wanted, action=row.action_id)
            return self._view(session, row)

    def expire_approvals(self) -> list[uuid.UUID]:
        """AWAITING_APPROVAL past the timeout -> CANCELLED, incident ESCALATED.
        Silence is never consent (ADR-0009)."""
        cutoff = self._clock() - self._approval_timeout
        with self._session_factory() as session:
            ids = list(
                session.execute(
                    select(RemediationRow.id).where(
                        RemediationRow.status == RemediationStatus.AWAITING_APPROVAL.value,
                        RemediationRow.updated_at < cutoff,
                    )
                ).scalars()
            )
        expired = []
        for remediation_id in ids:
            with self._session_factory() as session:
                row = self._lock(session, remediation_id)
                if row.status != RemediationStatus.AWAITING_APPROVAL.value:
                    continue
                session.add(
                    RemediationApprovalRow(
                        id=uuid.uuid4(),
                        remediation_id=row.id,
                        policy_decision_id=row.policy_decision_id,
                        proposal_hash=row.proposal_hash,
                        decision="timed_out",
                        approver="system:approval-timeout",
                        decided_at=self._clock(),
                    )
                )
                row.approval_status = "timed_out"
                incident = self._lock_incident(session, row.incident_id)
                self._finish_cancelled(
                    session,
                    row,
                    incident,
                    event="approval_timed_out",
                    actor="system:approval-timeout",
                    reason=f"no approval within {self._approval_timeout}",
                    incident_to=IncidentStatus.ESCALATED,
                )
                session.commit()
                expired.append(remediation_id)
        return expired

    def cancel(self, remediation_id: uuid.UUID, *, actor: str, reason: str) -> RemediationView:
        """Withdraw a remediation that hasn't started executing; the incident
        goes back to RCA_READY (the RCA still stands)."""
        with self._session_factory() as session:
            row = self._lock(session, remediation_id)
            if row.status not in (
                RemediationStatus.AWAITING_APPROVAL.value,
                RemediationStatus.APPROVED.value,
            ):
                raise RemediationStateError(f"cannot cancel a remediation that is {row.status}")
            incident = self._lock_incident(session, row.incident_id)
            self._finish_cancelled(
                session,
                row,
                incident,
                event="cancelled",
                actor=actor,
                reason=reason,
                incident_to=IncidentStatus.RCA_READY,
            )
            session.commit()
            return self._view(session, row)

    def revise(
        self,
        remediation_id: uuid.UUID,
        *,
        parameters: dict[str, Any],
        proposed_by: str,
        reason: str | None = None,
        idempotency_key: str,
    ) -> RemediationView:
        """A changed proposal is a new proposal: the old one is cancelled
        (superseded), and the new one gets its own policy decision and needs
        its own approval."""
        old = self.get(remediation_id)
        self.cancel(
            remediation_id, actor=f"operator:{proposed_by}", reason="superseded by revision"
        )
        return self.propose(
            RemediationProposal(
                incident_id=old.incident_id,
                investigation_id=old.investigation_id,
                action_id=old.action_id,
                parameters=parameters,
                reason=reason or old.reason,
                expected_effect=old.expected_effect,
                source="operator",
                proposed_by=proposed_by,
            ),
            idempotency_key=idempotency_key,
            correlation_id=old.correlation_id,
        )

    # --- execution ------------------------------------------------------------------

    def claim_execution(
        self, remediation_id: uuid.UUID, *, owner: str, lease_seconds: int = 120
    ) -> ExecutionTicket | None:
        """Start (or recover) one execution attempt. Re-checks, at execution
        time, everything that could have changed since approval: kill
        switches, the approval's binding, the catalog entry and parameters,
        and the attempt limit. Returns None if there is nothing to do or
        another worker holds it."""
        with self._session_factory() as session:
            row = self._lock(session, remediation_id)
            now = self._clock()
            status = RemediationStatus(row.status)
            if status not in (RemediationStatus.APPROVED, RemediationStatus.EXECUTING):
                return None
            latest = self._latest_execution(session, row.id)
            lease_live = row.lease_expires_at is not None and row.lease_expires_at > now
            if latest is not None and latest.status == ExecutionStatus.RUNNING.value:
                if lease_live:
                    return None
                # the worker holding it died mid-call: what happened is unknown
                latest.status = ExecutionStatus.UNKNOWN.value
                latest.error = f"worker {latest.owner} lost its lease mid-execution"
                latest.completed_at = now
                row.execution_status = ExecutionStatus.UNKNOWN.value
                row.lease_owner, row.lease_expires_at = (
                    owner,
                    now + timedelta(seconds=lease_seconds),
                )
                self._timeline(
                    session,
                    row,
                    "execution_interrupted",
                    status,
                    status,
                    f"worker:{owner}",
                    details={"execution_id": str(latest.id), "previous_owner": latest.owner},
                )
                session.commit()
                return self._ticket(session, row, latest, "reconcile")

            incident = self._lock_incident(session, row.incident_id)
            entry = get_entry(row.action_id)
            blocker = self._execution_blocker(session, row, entry)
            if blocker is not None:
                code, text = blocker
                if code == "kill_switch":
                    self._finish_cancelled(
                        session,
                        row,
                        incident,
                        event="cancelled",
                        actor="system:kill-switch",
                        reason=text,
                        incident_to=IncidentStatus.ESCALATED,
                    )
                else:
                    self._finish_failed(session, row, incident, code, text, "system:pre-execution")
                session.commit()
                return None
            assert entry is not None
            attempt = row.execution_attempts + 1
            execution = RemediationExecutionRow(
                id=uuid.uuid4(),
                remediation_id=row.id,
                attempt_number=attempt,
                idempotency_key=f"{row.id}:{attempt}",
                status=ExecutionStatus.RUNNING.value,
                executor="pending",
                owner=owner,
                started_at=now,
                deadline_at=now + timedelta(seconds=entry.timeout_seconds),
            )
            session.add(execution)
            row.execution_attempts = attempt
            row.execution_status = ExecutionStatus.RUNNING.value
            row.lease_owner, row.lease_expires_at = (
                owner,
                now + timedelta(seconds=max(lease_seconds, entry.timeout_seconds + 30)),
            )
            self._transition(
                session,
                row,
                RemediationStatus.EXECUTING,
                "execution_started",
                f"worker:{owner}",
                details={
                    "execution_id": str(execution.id),
                    "attempt": attempt,
                    "idempotency_key": execution.idempotency_key,
                    "deadline": execution.deadline_at.isoformat(),
                },
            )
            self._event(session, row, EVENT_TYPE_REMEDIATION_STARTED, f"worker:{owner}")
            session.commit()
            metrics.increment(
                "remediation.execution_started", action=row.action_id, attempt=attempt
            )
            return self._ticket(session, row, execution, "execute")

    def _execution_blocker(
        self, session: Session, row: RemediationRow, entry: ActionCatalogEntry | None
    ) -> tuple[str, str] | None:
        switches = self._engaged_scopes(session)
        if GLOBAL_SCOPE in switches or service_scope(row.target_service) in switches:
            return ("kill_switch", "remediation kill switch engaged before execution")
        approval = session.execute(
            select(RemediationApprovalRow).where(RemediationApprovalRow.remediation_id == row.id)
        ).scalar_one_or_none()
        if (
            approval is None
            or approval.decision != "approved"
            or approval.proposal_hash != row.proposal_hash
            or approval.policy_decision_id != row.policy_decision_id
        ):
            return ("approval_invalid", "no approval bound to this exact proposal")
        if entry is None or row.catalog_version != CATALOG_VERSION:
            return (
                "catalog_changed",
                f"catalog entry {row.action_id} ({row.catalog_version}) is no longer current",
            )
        _, problems = entry.validate(row.parameters)
        if problems:
            return ("invalid_parameters", "; ".join(problems))
        if row.target_service not in self._topology.services:
            return ("target_invalid", f"target {row.target_service} is no longer a known service")
        if row.execution_attempts >= entry.max_attempts:
            return ("attempts_exhausted", f"{row.execution_attempts} attempt(s) already made")
        return None

    def complete_execution(
        self,
        execution_id: uuid.UUID,
        *,
        owner: str,
        outcome: str,  # succeeded | failed | timed_out
        executor: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        retryable: bool = False,
    ) -> RemediationView:
        """Record an attempt's outcome and move the remediation on: EXECUTED,
        another attempt (only if the action is retry-safe, the failure is
        retryable and attempts remain), or FAILED."""
        with self._session_factory() as session:
            execution = session.get(RemediationExecutionRow, execution_id, with_for_update=True)
            if execution is None:
                raise RemediationNotFoundError(f"execution {execution_id}")
            row = self._lock(session, execution.remediation_id)
            if row.status != RemediationStatus.EXECUTING.value:
                return self._view(session, row)  # already settled (duplicate completion)
            if row.lease_owner != owner:
                raise LeaseLostError(f"{owner} does not hold remediation {row.id}")
            if execution.status not in (
                ExecutionStatus.RUNNING.value,
                ExecutionStatus.UNKNOWN.value,
            ):
                return self._view(session, row)
            now = self._clock()
            execution.executor = executor
            execution.completed_at = now
            execution.result = result
            execution.error = (error or "")[:2000] or None
            entry = get_entry(row.action_id)
            incident = self._lock_incident(session, row.incident_id)
            actor = f"executor:{executor}"
            row.executor_result = {"attempt": execution.attempt_number, **(result or {})}
            if outcome == "succeeded":
                execution.status = ExecutionStatus.SUCCEEDED.value
                row.execution_status = ExecutionStatus.SUCCEEDED.value
                row.lease_owner = row.lease_expires_at = None
                row.completed_at = now
                self._transition(
                    session,
                    row,
                    RemediationStatus.EXECUTED,
                    "executed",
                    actor,
                    details={"execution_id": str(execution.id), "result": result or {}},
                )
                self._event(session, row, EVENT_TYPE_REMEDIATION_EXECUTED, actor)
                self._request_verification(session, row, entry)
                self._move_incident(
                    session,
                    incident,
                    {IncidentStatus.REMEDIATION_IN_PROGRESS},
                    IncidentStatus.VERIFYING,
                    "remediation_executed",
                )
            else:
                execution.status = (
                    ExecutionStatus.TIMED_OUT.value
                    if outcome == "timed_out"
                    else ExecutionStatus.FAILED.value
                )
                row.execution_status = execution.status
                can_retry = (
                    entry is not None
                    and entry.retry_safe
                    and retryable
                    and row.execution_attempts < entry.max_attempts
                )
                if can_retry:
                    row.lease_owner = row.lease_expires_at = None
                    self._timeline(
                        session,
                        row,
                        "execution_attempt_failed",
                        RemediationStatus.EXECUTING,
                        RemediationStatus.EXECUTING,
                        actor,
                        details={
                            "execution_id": str(execution.id),
                            "outcome": outcome,
                            "error": execution.error,
                            "will_retry": True,
                        },
                    )
                else:
                    why = (
                        "timed out; outcome unknown and the action is not retry-safe"
                        if outcome == "timed_out" and not (entry and entry.retry_safe)
                        else f"{outcome}: {execution.error or 'no detail'}"
                    )
                    self._finish_failed(session, row, incident, outcome, why, actor)
            session.commit()
            metrics.increment(
                "remediation.execution_outcome", outcome=outcome, action=row.action_id
            )
            return self._view(session, row)

    def pending_executions(self) -> list[uuid.UUID]:
        """APPROVED, or EXECUTING with an expired lease: what a runner should
        pick up (at-least-once delivery backstop + crash recovery)."""
        now = self._clock()
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(RemediationRow.id)
                    .where(
                        (RemediationRow.status == RemediationStatus.APPROVED.value)
                        | (
                            (RemediationRow.status == RemediationStatus.EXECUTING.value)
                            & (
                                RemediationRow.lease_expires_at.is_(None)
                                | (RemediationRow.lease_expires_at < now)
                            )
                        )
                    )
                    .order_by(RemediationRow.updated_at)
                ).scalars()
            )

    def _request_verification(
        self, session: Session, row: RemediationRow, entry: ActionCatalogEntry | None
    ) -> None:
        verification_id = uuid.uuid4()
        row.verification_ref = verification_id
        requirements = [v.__dict__ for v in (entry.verification if entry else ())]
        self._timeline(
            session,
            row,
            "verification_requested",
            RemediationStatus.EXECUTED,
            RemediationStatus.EXECUTED,
            "incident-core",
            details={"verification_id": str(verification_id), "requirements": requirements},
        )
        repository.insert_outbox_event(
            session,
            event_type=EVENT_TYPE_VERIFICATION_REQUESTED,
            aggregate_type=AGGREGATE_TYPE_REMEDIATION,
            aggregate_id=row.id,
            correlation_id=row.correlation_id,
            producer=PRODUCER_INCIDENT_CORE,
            payload=VerificationRequestedPayload(
                verification_id=verification_id,
                remediation_id=row.id,
                incident_id=row.incident_id,
                action_id=row.action_id,
                target_service=row.target_service,
                requirements=requirements,
            ).model_dump(mode="json"),
        )

    # --- kill switches ----------------------------------------------------------------

    def set_kill_switch(
        self, scope: str, *, engaged: bool, actor: str, reason: str | None = None
    ) -> None:
        if scope != GLOBAL_SCOPE and not scope.startswith("service:"):
            raise ValueError("scope is 'global' or 'service:<name>'")
        with self._session_factory() as session:
            row = session.get(KillSwitchRow, scope, with_for_update=True)
            if row is None:
                row = KillSwitchRow(scope=scope)
                session.add(row)
            row.engaged = engaged
            row.changed_by = actor
            row.reason = reason
            row.changed_at = self._clock()
            repository.insert_outbox_event(
                session,
                event_type=EVENT_TYPE_KILL_SWITCH_CHANGED,
                aggregate_type=AGGREGATE_TYPE_KILL_SWITCH,
                aggregate_id=uuid.uuid5(uuid.NAMESPACE_URL, f"kill-switch:{scope}"),
                producer=PRODUCER_INCIDENT_CORE,
                payload=KillSwitchChangedPayload(
                    scope=scope, engaged=engaged, actor=actor, reason=reason
                ).model_dump(mode="json"),
            )
            session.commit()
        log.warning("remediation.kill_switch", scope=scope, engaged=engaged, actor=actor)

    def kill_switches(self) -> dict[str, bool]:
        with self._session_factory() as session:
            return {r.scope: r.engaged for r in session.execute(select(KillSwitchRow)).scalars()}

    @staticmethod
    def _engaged_scopes(session: Session) -> set[str]:
        return set(
            session.execute(select(KillSwitchRow.scope).where(KillSwitchRow.engaged)).scalars()
        )

    # --- reads -------------------------------------------------------------------

    def get(self, remediation_id: uuid.UUID) -> RemediationView:
        with self._session_factory() as session:
            row = session.get(RemediationRow, remediation_id)
            if row is None:
                raise RemediationNotFoundError(str(remediation_id))
            return self._view(session, row)

    def list_for_incident(self, incident_id: uuid.UUID) -> list[RemediationView]:
        with self._session_factory() as session:
            rows = session.execute(
                select(RemediationRow)
                .where(RemediationRow.incident_id == incident_id)
                .order_by(RemediationRow.created_at)
            ).scalars()
            return [self._view(session, r) for r in rows]

    def timeline(self, remediation_id: uuid.UUID) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.execute(
                select(RemediationTimelineRow)
                .where(RemediationTimelineRow.remediation_id == remediation_id)
                .order_by(RemediationTimelineRow.sequence)
            ).scalars()
            return [
                {
                    "sequence": r.sequence,
                    "event": r.event,
                    "from_status": r.from_status,
                    "to_status": r.to_status,
                    "actor": r.actor,
                    "correlation_id": str(r.correlation_id),
                    "action_id": r.action_id,
                    "catalog_version": r.catalog_version,
                    "policy_version": r.policy_version,
                    "details": r.details,
                    "occurred_at": r.occurred_at.isoformat(),
                }
                for r in rows
            ]

    def policy_decisions(self, remediation_id: uuid.UUID) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.execute(
                select(RemediationPolicyDecisionRow)
                .where(RemediationPolicyDecisionRow.remediation_id == remediation_id)
                .order_by(RemediationPolicyDecisionRow.evaluated_at)
            ).scalars()
            return [
                {
                    "id": str(r.id),
                    "decision": r.decision,
                    "policy_version": r.policy_version,
                    "catalog_version": r.catalog_version,
                    "catalog_digest": r.catalog_digest,
                    "proposal_hash": r.proposal_hash,
                    "blast_radius_tier": r.blast_radius_tier,
                    "required_approver_roles": r.required_approver_roles,
                    "rules": r.rules,
                    "reasons": r.reasons,
                    "policy_context": r.policy_context,
                    "evaluated_at": r.evaluated_at.isoformat(),
                }
                for r in rows
            ]

    def executions(self, remediation_id: uuid.UUID) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.execute(
                select(RemediationExecutionRow)
                .where(RemediationExecutionRow.remediation_id == remediation_id)
                .order_by(RemediationExecutionRow.attempt_number)
            ).scalars()
            return [
                {
                    "id": str(r.id),
                    "attempt": r.attempt_number,
                    "idempotency_key": r.idempotency_key,
                    "status": r.status,
                    "executor": r.executor,
                    "owner": r.owner,
                    "started_at": r.started_at.isoformat(),
                    "deadline_at": r.deadline_at.isoformat(),
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "result": r.result,
                    "error": r.error,
                }
                for r in rows
            ]

    # --- internals ----------------------------------------------------------------------

    def _ticket(
        self, session: Session, row: RemediationRow, execution: RemediationExecutionRow, mode: str
    ) -> ExecutionTicket:
        return ExecutionTicket(
            mode=mode,
            remediation=self._view(session, row),
            execution_id=execution.id,
            attempt=execution.attempt_number,
            idempotency_key=execution.idempotency_key,
            deadline=execution.deadline_at,
            action_id=row.action_id,
            catalog_version=row.catalog_version,
            parameters=dict(row.parameters),
            target_service=row.target_service,
            environment=row.environment,
        )

    @staticmethod
    def _latest_execution(
        session: Session, remediation_id: uuid.UUID
    ) -> RemediationExecutionRow | None:
        return session.execute(
            select(RemediationExecutionRow)
            .where(RemediationExecutionRow.remediation_id == remediation_id)
            .order_by(RemediationExecutionRow.attempt_number.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()

    def _finish_failed(
        self,
        session: Session,
        row: RemediationRow,
        incident: IncidentRow,
        code: str,
        text: str,
        actor: str,
    ) -> None:
        row.failure_reason = f"{code}: {text}"[:2000]
        row.lease_owner = row.lease_expires_at = None
        row.completed_at = self._clock()
        self._transition(
            session,
            row,
            RemediationStatus.FAILED,
            "failed",
            actor,
            details={"code": code, "detail": text},
        )
        self._event(session, row, EVENT_TYPE_REMEDIATION_FAILED, actor)
        self._move_incident(
            session,
            incident,
            {IncidentStatus.REMEDIATION_IN_PROGRESS, IncidentStatus.AWAITING_APPROVAL},
            IncidentStatus.ESCALATED,
            f"remediation_failed:{code}",
        )

    def _finish_cancelled(
        self,
        session: Session,
        row: RemediationRow,
        incident: IncidentRow,
        *,
        event: str,
        actor: str,
        reason: str,
        incident_to: IncidentStatus,
        event_type: str = EVENT_TYPE_REMEDIATION_CANCELLED,
        decision_row: RemediationPolicyDecisionRow | None = None,
    ) -> None:
        row.failure_reason = reason[:2000]
        row.lease_owner = row.lease_expires_at = None
        row.completed_at = self._clock()
        self._transition(
            session, row, RemediationStatus.CANCELLED, event, actor, details={"reason": reason}
        )
        self._event(session, row, event_type, actor, decision_row)
        self._move_incident(
            session,
            incident,
            {IncidentStatus.AWAITING_APPROVAL, IncidentStatus.REMEDIATION_IN_PROGRESS},
            incident_to,
            f"remediation_{event}",
        )

    def _transition(
        self,
        session: Session,
        row: RemediationRow,
        to_status: RemediationStatus,
        event: str,
        actor: str,
        *,
        details: dict[str, Any] | None = None,
        policy_version: str | None = None,
    ) -> None:
        from_status = RemediationStatus(row.status)
        if not can_transition(from_status, to_status):
            raise RemediationStateError(f"{from_status.value} -> {to_status.value} is not allowed")
        row.status = to_status.value
        row.version += 1
        row.updated_at = self._clock()
        self._timeline(session, row, event, from_status, to_status, actor, details, policy_version)

    def _timeline(
        self,
        session: Session,
        row: RemediationRow,
        event: str,
        from_status: RemediationStatus | None,
        to_status: RemediationStatus | None,
        actor: str,
        details: dict[str, Any] | None = None,
        policy_version: str | None = None,
    ) -> None:
        sequence = (
            session.execute(
                select(func.coalesce(func.max(RemediationTimelineRow.sequence), 0)).where(
                    RemediationTimelineRow.remediation_id == row.id
                )
            ).scalar_one()
            + 1
        )
        if policy_version is None and row.policy_decision_id is not None:
            decision = session.get(RemediationPolicyDecisionRow, row.policy_decision_id)
            policy_version = decision.policy_version if decision else None
        session.add(
            RemediationTimelineRow(
                remediation_id=row.id,
                sequence=sequence,
                event=event,
                from_status=from_status.value if from_status else None,
                to_status=to_status.value if to_status else None,
                actor=actor,
                correlation_id=row.correlation_id,
                action_id=row.action_id,
                catalog_version=row.catalog_version,
                policy_version=policy_version,
                details=details or {},
                occurred_at=self._clock(),
            )
        )
        session.flush()

    def _event(
        self,
        session: Session,
        row: RemediationRow,
        event_type: str,
        actor: str,
        decision_row: RemediationPolicyDecisionRow | None = None,
    ) -> None:
        if decision_row is None and row.policy_decision_id is not None:
            decision_row = session.get(RemediationPolicyDecisionRow, row.policy_decision_id)
        repository.insert_outbox_event(
            session,
            event_type=event_type,
            aggregate_type=AGGREGATE_TYPE_REMEDIATION,
            aggregate_id=row.id,
            correlation_id=row.correlation_id,
            producer=PRODUCER_INCIDENT_CORE,
            payload=RemediationEventPayload(
                remediation_id=row.id,
                incident_id=row.incident_id,
                action_id=row.action_id,
                catalog_version=row.catalog_version,
                status=row.status,
                proposal_hash=row.proposal_hash,
                actor=actor,
                policy_version=decision_row.policy_version if decision_row else None,
                policy_decision=decision_row.decision if decision_row else None,
                detail={"failure_reason": row.failure_reason} if row.failure_reason else {},
            ).model_dump(mode="json"),
        )

    def _move_incident(
        self,
        session: Session,
        incident: IncidentRow,
        from_statuses: set[IncidentStatus],
        to_status: IncidentStatus,
        reason: str,
    ) -> None:
        """Move the incident only along the edges this phase owns; if a human
        already moved it elsewhere, record the remediation and leave it."""
        if IncidentStatus(incident.status) not in from_statuses:
            log.info(
                "remediation.incident_not_moved",
                incident_id=str(incident.id),
                status=incident.status,
                wanted=to_status.value,
            )
            return
        from_status = incident.status
        if not repository.transition_incident_status(
            session, incident=incident, to_status=to_status, now=self._clock(), closes=False
        ):
            raise ConcurrentModificationError(f"incident {incident.id} changed concurrently")
        repository.insert_outbox_event(
            session,
            event_type=EVENT_TYPE_INCIDENT_STATUS_CHANGED,
            aggregate_type=AGGREGATE_TYPE_INCIDENT,
            aggregate_id=incident.id,
            correlation_id=incident.id,
            producer=PRODUCER_INCIDENT_CORE,
            payload=IncidentStatusChangedPayload(
                incident_id=incident.id,
                from_status=from_status,
                to_status=incident.status,
                reason=reason,
                version=incident.version,
            ).model_dump(mode="json"),
        )

    @staticmethod
    def _lock(session: Session, remediation_id: uuid.UUID) -> RemediationRow:
        row = session.get(RemediationRow, remediation_id, with_for_update=True)
        if row is None:
            raise RemediationNotFoundError(str(remediation_id))
        return row

    @staticmethod
    def _lock_incident(session: Session, incident_id: uuid.UUID) -> IncidentRow:
        incident = session.get(IncidentRow, incident_id, with_for_update=True)
        if incident is None:
            raise IncidentNotFoundError(str(incident_id))
        return incident

    @staticmethod
    def _view(session: Session, row: RemediationRow) -> RemediationView:
        decision = (
            session.get(RemediationPolicyDecisionRow, row.policy_decision_id)
            if row.policy_decision_id
            else None
        )
        return RemediationView(
            id=row.id,
            incident_id=row.incident_id,
            investigation_id=row.investigation_id,
            action_id=row.action_id,
            catalog_version=row.catalog_version,
            parameters=dict(row.parameters),
            target_service=row.target_service,
            environment=row.environment,
            reason=row.reason,
            expected_effect=row.expected_effect,
            blast_radius_tier=row.blast_radius_tier,
            proposal_hash=row.proposal_hash,
            source=row.source,
            proposed_by=row.proposed_by,
            status=RemediationStatus(row.status),
            policy_decision_id=row.policy_decision_id,
            policy_decision=decision.decision if decision else None,
            approval_status=row.approval_status,
            execution_status=row.execution_status,
            execution_attempts=row.execution_attempts,
            executor_result=row.executor_result,
            failure_reason=row.failure_reason,
            verification_ref=row.verification_ref,
            correlation_id=row.correlation_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )
