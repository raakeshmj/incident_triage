"""Phase 7 on real Postgres + isolated Redis (DB 15): planning, policy,
approval, bounded execution against the simulator control plane, failure
handling and the audit trail. No model API (the investigation that reaches
RCA_READY uses the scripted model)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.agents.fake import FakeInvestigationModel
from packages.domain.errors import (
    ApprovalMismatchError,
    ApproverNotAuthorizedError,
    RemediationStateError,
)
from packages.domain.remediation import (
    PolicyEvaluationContext,
    RemediationProposal,
    RemediationStatus,
)
from packages.evaluation.recording import EvidenceStoreReader
from packages.incident.db.models import IncidentRow, OutboxEventRow
from packages.incident.investigations import InvestigationCoreService
from packages.incident.remediations import RemediationCoreService
from packages.policy.engine import DEFAULT_POLICY, evaluate
from packages.remediation.catalog import get_entry
from packages.remediation.executor import (
    OPS_LOG_KEY,
    ExecutionRequest,
    ExecutorResult,
    SimulatorRemediationExecutor,
)
from packages.remediation.planner import RemediationPlanner
from packages.remediation.runner import RemediationRunner
from simulator.changes.registry import ChangeRegistry
from tests import investigation_support as support

SERVICE = "checkout-service"
BAD_FAULT = {
    "scenario": "bad-deployment",
    "params": {"error_rate": 0.3},
    "started_at": 0,
    "expires_at": 9e9,
}


class Env:
    def __init__(
        self, session_factory, core, evidence_session_factory, redis, policy=DEFAULT_POLICY, **kw
    ):
        self.session_factory = session_factory
        self.core = core
        self.redis = redis
        self.investigations = InvestigationCoreService(session_factory)
        self.evidence = support.build_evidence_service(evidence_session_factory, core, redis)
        self.reader = EvidenceStoreReader(evidence_session_factory)
        self.remediations = RemediationCoreService(
            session_factory, topology=support.CATALOG, policy=policy, **kw
        )
        self.executor = SimulatorRemediationExecutor(redis)
        self.planner = RemediationPlanner(self.reader)

    def rca_ready_incident(self) -> tuple[uuid.UUID, uuid.UUID]:
        incident_id = support.open_incident(self.core)
        investigation_id = support.start(self.investigations, incident_id)
        engine = support.make_engine(
            self.investigations,
            self.core,
            self.evidence,
            FakeInvestigationModel(support.happy_script()),
        )
        engine.run(investigation_id)
        assert self.incident_status(incident_id) == "RCA_READY"
        return incident_id, investigation_id

    def planned(self) -> tuple[uuid.UUID, object]:
        incident_id, investigation_id = self.rca_ready_incident()
        report = self.investigations.get_trace(investigation_id)["rca_report"]["report"]
        proposal, _ = self.planner.plan(incident_id, investigation_id, report)
        assert proposal is not None
        view = self.remediations.propose(proposal, idempotency_key=f"planner:{investigation_id}")
        return incident_id, view

    def approve(self, view, approver="alice", roles=("service_owner",)):
        return self.remediations.decide_approval(
            view.id,
            approver=approver,
            approver_roles=list(roles),
            approve=True,
            proposal_hash=view.proposal_hash,
            policy_decision_id=view.policy_decision_id,
        )

    def runner(self, executor=None, owner="runner-a", **kw) -> RemediationRunner:
        return RemediationRunner(self.remediations, executor or self.executor, owner=owner, **kw)

    def incident_status(self, incident_id) -> str:
        with self.session_factory() as session:
            return session.get(IncidentRow, incident_id).status

    def events(self, event_type) -> list[dict]:
        with self.session_factory() as session:
            rows = (
                session.query(OutboxEventRow).filter(OutboxEventRow.event_type == event_type).all()
            )
            return [r.payload for r in rows]


@pytest.fixture
def env(session_factory, core, evidence_session_factory, test_redis):
    registry = ChangeRegistry(test_redis)
    registry.seed()
    registry.record_deployment(service=SERVICE, version="1.1.0-bad")
    test_redis.set(f"chaos:{SERVICE}", json.dumps(BAD_FAULT))
    return Env(session_factory, core, evidence_session_factory, test_redis)


def _proposal(incident_id, action="rollback_deployment", **params):
    base = {"service": SERVICE, "from_version": "1.1.0-bad", "to_version": "1.0.0"}
    return RemediationProposal(
        incident_id=incident_id,
        action_id=action,
        parameters={**base, **params} if action == "rollback_deployment" else params,
        reason="r",
        expected_effect="e",
        source="operator",
        proposed_by="ops-bob",
    )


# --- the happy path, end to end -------------------------------------------------------


def test_planned_rollback_is_approved_executed_and_audited(env):
    incident_id, view = env.planned()
    assert view.status == RemediationStatus.AWAITING_APPROVAL
    assert view.action_id == "rollback_deployment" and view.blast_radius_tier == 2
    assert view.parameters == {
        "service": SERVICE,
        "from_version": "1.1.0-bad",
        "to_version": "1.0.0",
    }
    assert env.incident_status(incident_id) == "AWAITING_APPROVAL"

    decision = env.remediations.policy_decisions(view.id)[0]
    assert decision["decision"] == "REQUIRE_APPROVAL"
    assert decision["policy_context"]["rca_component"] == SERVICE
    assert decision["policy_version"] == DEFAULT_POLICY.version

    approved = env.approve(view)
    assert approved.status == RemediationStatus.APPROVED
    assert env.incident_status(incident_id) == "REMEDIATION_IN_PROGRESS"

    done = env.runner().run(view.id)
    assert done.status == RemediationStatus.EXECUTED and done.execution_attempts == 1
    assert done.verification_ref is not None
    assert env.incident_status(incident_id) == "VERIFYING"
    # the simulated world actually changed: the bad build is gone, a rollback is on record
    assert env.redis.get(f"chaos:{SERVICE}") is None
    current = ChangeRegistry(env.redis).current_deployment(SERVICE)
    assert current["version"] == "1.0.0" and current["change_type"] == "rollback"
    assert current["deployed_by"] == "remediation-executor"

    timeline = env.remediations.timeline(view.id)
    assert [t["event"] for t in timeline] == [
        "proposed",
        "policy_evaluated",
        "approval_requested",
        "approved",
        "execution_started",
        "executed",
        "verification_requested",
    ]
    assert all(
        t["actor"] and t["correlation_id"] and t["action_id"] == "rollback_deployment"
        for t in timeline
    )
    assert {t["actor"] for t in timeline} >= {
        "planner:remediation-planner",
        "policy-engine",
        "human:alice",
    }
    assert all(t["policy_version"] == DEFAULT_POLICY.version for t in timeline[1:])
    verification = env.events("VerificationRequested")
    assert len(verification) == 1 and verification[0]["requirements"][0]["metric"] == "error_rate"
    for event in (
        "RemediationProposed",
        "RemediationApprovalRequested",
        "RemediationApproved",
        "RemediationStarted",
        "RemediationExecuted",
    ):
        assert len(env.events(event)) == 1, event


def test_a_stored_policy_decision_replays_identically(env):
    incident_id, view = env.planned()
    stored = env.remediations.policy_decisions(view.id)[0]
    proposal = RemediationProposal(
        incident_id=incident_id,
        investigation_id=view.investigation_id,
        action_id=view.action_id,
        parameters=view.parameters,
        reason=view.reason,
        expected_effect=view.expected_effect,
        source=view.source,  # type: ignore[arg-type]
        proposed_by=view.proposed_by,
    )
    replayed = evaluate(
        proposal,
        get_entry(view.action_id),
        DEFAULT_POLICY,
        PolicyEvaluationContext.model_validate(stored["policy_context"]),
    )
    assert replayed.decision == stored["decision"]
    assert [r.model_dump() for r in replayed.rules] == stored["rules"]


def test_audit_rows_and_proposals_are_immutable(env):
    _, view = env.planned()
    env.approve(view)
    for statement in (
        "UPDATE incident_core.remediation_timeline SET actor = 'x'",
        "UPDATE incident_core.remediation_approvals SET approver = 'mallory'",
        "UPDATE incident_core.remediation_policy_decisions SET decision = 'ALLOW'",
        "UPDATE incident_core.remediations SET parameters = '{}'::jsonb",
        "DELETE FROM incident_core.remediations",
    ):
        with env.session_factory() as session, pytest.raises(DBAPIError):
            session.execute(text(statement))
            session.commit()


# --- policy rejections ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "params", "rule"),
    [
        ("delete_database", {"service": SERVICE}, "action.known"),
        ("rollback_deployment", {"service": "billing-service"}, "target.known"),
        ("rollback_deployment", {"service": "inventory-service"}, "target.in_incident_scope"),
        ("rollback_deployment", {"service": "payment-service"}, "rca.target_matches_root_cause"),
        ("scale_service", {"service": SERVICE, "increase_by": 99}, "action.parameters"),
    ],
)
def test_policy_rejections_are_final_and_escalate(env, action, params, rule):
    incident_id, _ = env.rca_ready_incident()
    view = env.remediations.propose(_proposal(incident_id, action, **params), idempotency_key="k")
    assert view.status == RemediationStatus.POLICY_REJECTED
    assert rule in env.remediations.policy_decisions(view.id)[0]["policy_context"] or rule in [
        r["rule_id"]
        for r in env.remediations.policy_decisions(view.id)[0]["rules"]
        if r["outcome"] == "deny"
    ]
    assert env.incident_status(incident_id) == "ESCALATED"
    with pytest.raises(RemediationStateError):  # no approval can override policy
        env.remediations.decide_approval(
            view.id,
            approver="alice",
            approver_roles=["service_owner"],
            approve=True,
            proposal_hash=view.proposal_hash,
            policy_decision_id=view.policy_decision_id,
        )
    assert env.remediations.claim_execution(view.id, owner="w") is None


def test_the_kill_switch_denies_new_proposals(env):
    incident_id, _ = env.rca_ready_incident()
    env.remediations.set_kill_switch("global", engaged=True, actor="human:carol", reason="drill")
    view = env.remediations.propose(_proposal(incident_id), idempotency_key="k")
    assert view.status == RemediationStatus.POLICY_REJECTED
    assert "kill switch" in (view.failure_reason or "")
    assert len(env.events("KillSwitchChanged")) == 1


def test_a_kill_switch_engaged_after_approval_stops_execution(env):
    incident_id, view = env.planned()
    env.approve(view)
    env.remediations.set_kill_switch(f"service:{SERVICE}", engaged=True, actor="human:carol")
    result = env.runner().run(view.id)
    assert result.status == RemediationStatus.CANCELLED and "kill switch" in (
        result.failure_reason or ""
    )
    assert env.redis.get(f"chaos:{SERVICE}") is not None  # nothing was executed
    assert env.remediations.executions(view.id) == []
    assert env.incident_status(incident_id) == "ESCALATED"


def test_remediation_attempts_are_bounded(
    session_factory, core, evidence_session_factory, test_redis
):
    ChangeRegistry(test_redis).seed()
    ChangeRegistry(test_redis).record_deployment(service=SERVICE, version="1.1.0-bad")
    env = Env(
        session_factory,
        core,
        evidence_session_factory,
        test_redis,
        policy=replace(
            DEFAULT_POLICY,
            max_attempted_remediations_per_incident=1,
            max_executions_per_service_per_hour=1,
        ),
    )
    incident_id, view = env.planned()
    env.approve(view)
    assert env.runner().run(view.id).status == RemediationStatus.EXECUTED
    with session_factory() as session:  # (a later phase's verification failure would reopen it)
        session.execute(text("UPDATE incident_core.incidents SET status = 'RCA_READY'"))
        session.commit()
    again = env.remediations.propose(
        _proposal(incident_id, to_version="1.0.0", from_version="1.0.0"), idempotency_key="k2"
    )
    denied = [
        r["rule_id"]
        for r in env.remediations.policy_decisions(again.id)[0]["rules"]
        if r["outcome"] == "deny"
    ]
    assert again.status == RemediationStatus.POLICY_REJECTED
    assert {"attempts.per_incident", "attempts.per_service_per_hour"} <= set(denied)


# --- approval -----------------------------------------------------------------------


def test_approval_needs_an_eligible_role_and_a_matching_binding(env):
    _, view = env.planned()
    with pytest.raises(ApproverNotAuthorizedError):
        env.approve(view, approver="dave", roles=["on_call_engineer"])  # tier 2 needs service_owner
    with pytest.raises(ApprovalMismatchError):
        env.remediations.decide_approval(
            view.id,
            approver="alice",
            approver_roles=["service_owner"],
            approve=True,
            proposal_hash="sha256:" + "0" * 64,
            policy_decision_id=view.policy_decision_id,
        )
    with pytest.raises(ApprovalMismatchError):
        env.remediations.decide_approval(
            view.id,
            approver="alice",
            approver_roles=["service_owner"],
            approve=True,
            proposal_hash=view.proposal_hash,
            policy_decision_id=uuid.uuid4(),
        )
    assert env.remediations.get(view.id).status == RemediationStatus.AWAITING_APPROVAL


def test_duplicate_approval_is_idempotent_and_a_conflicting_one_is_refused(env):
    _, view = env.planned()
    first = env.approve(view)
    assert env.approve(view) == first  # redelivered identical decision
    with pytest.raises(RemediationStateError):
        env.remediations.decide_approval(
            view.id,
            approver="alice",
            approver_roles=["service_owner"],
            approve=False,
            proposal_hash=view.proposal_hash,
            policy_decision_id=view.policy_decision_id,
        )
    assert len(env.events("RemediationApproved")) == 1


def test_rejection_cancels_and_escalates(env):
    incident_id, view = env.planned()
    rejected = env.remediations.decide_approval(
        view.id,
        approver="alice",
        approver_roles=["service_owner"],
        approve=False,
        proposal_hash=view.proposal_hash,
        policy_decision_id=view.policy_decision_id,
        comment="not now",
    )
    assert rejected.status == RemediationStatus.CANCELLED and rejected.approval_status == "rejected"
    assert env.incident_status(incident_id) == "ESCALATED"
    assert env.runner().run(view.id).status == RemediationStatus.CANCELLED


def test_a_changed_proposal_needs_a_new_approval(env):
    incident_id, view = env.planned()
    revised = env.remediations.revise(
        view.id,
        parameters={**view.parameters, "to_version": "0.9.0"},
        proposed_by="ops-bob",
        idempotency_key="rev-1",
    )
    assert env.remediations.get(view.id).status == RemediationStatus.CANCELLED
    assert revised.status == RemediationStatus.AWAITING_APPROVAL
    assert revised.proposal_hash != view.proposal_hash
    assert revised.policy_decision_id != view.policy_decision_id
    with pytest.raises(ApprovalMismatchError):  # the old approval binding doesn't carry over
        env.remediations.decide_approval(
            revised.id,
            approver="alice",
            approver_roles=["service_owner"],
            approve=True,
            proposal_hash=view.proposal_hash,
            policy_decision_id=view.policy_decision_id,
        )
    with pytest.raises(RemediationStateError):  # the superseded one can't be approved
        env.approve(view)
    with pytest.raises(ApproverNotAuthorizedError):  # and a proposer can't approve their own
        env.approve(revised, approver="ops-bob")
    assert env.approve(revised).status == RemediationStatus.APPROVED


def test_unanswered_approvals_time_out_to_escalation(
    session_factory, core, evidence_session_factory, test_redis
):
    ChangeRegistry(test_redis).seed()
    ChangeRegistry(test_redis).record_deployment(service=SERVICE, version="1.1.0-bad")
    env = Env(
        session_factory,
        core,
        evidence_session_factory,
        test_redis,
        approval_timeout=timedelta(seconds=0),
    )
    incident_id, view = env.planned()
    time.sleep(0.01)
    assert env.remediations.expire_approvals() == [view.id]
    expired = env.remediations.get(view.id)
    assert expired.status == RemediationStatus.CANCELLED and expired.approval_status == "timed_out"
    assert env.incident_status(incident_id) == "ESCALATED"


# --- execution failures, retries, recovery -------------------------------------------


class ScriptedExecutor:
    name = "scripted"

    def __init__(self, *results, delay=0.0):
        self.results = list(results)
        self.calls: list[ExecutionRequest] = []
        self.delay = delay
        self.known: dict[str, ExecutorResult] = {}

    def execute(self, request):
        self.calls.append(request)
        time.sleep(self.delay)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        self.known[request.idempotency_key] = result
        return result

    def inspect(self, key):
        return self.known.get(key)


def test_duplicate_execution_requests_do_not_act_twice(env):
    _, view = env.planned()
    env.approve(view)
    runner = env.runner()
    assert runner.run(view.id).status == RemediationStatus.EXECUTED
    assert (
        runner.run(view.id).status == RemediationStatus.EXECUTED
    )  # redelivered RemediationApproved
    assert len(env.remediations.executions(view.id)) == 1
    key = env.remediations.executions(view.id)[0]["idempotency_key"]
    request = ExecutionRequest(
        execution_id="x",
        idempotency_key=key,
        action_id="rollback_deployment",
        catalog_version=view.catalog_version,
        parameters=view.parameters,
        target_service=SERVICE,
        environment="production",
        deadline=env.remediations.get(view.id).updated_at + timedelta(hours=1),
    )
    assert env.executor.execute(request).succeeded  # the executor's own idempotency record
    assert env.redis.llen(OPS_LOG_KEY.format(service=SERVICE)) == 1


def test_a_retryable_failure_of_a_retry_safe_action_is_retried_within_bounds(env):
    _, view = env.planned()
    env.approve(view)
    flaky = ScriptedExecutor(
        ExecutorResult(False, error="registry busy", retryable=True),
        ExecutorResult(True, {"ok": 1}),
    )
    done = env.runner(flaky).run(view.id)
    assert done.status == RemediationStatus.EXECUTED and done.execution_attempts == 2
    assert [e["status"] for e in env.remediations.executions(view.id)] == ["FAILED", "SUCCEEDED"]
    assert len({c.idempotency_key for c in flaky.calls}) == 2


def test_retries_stop_at_the_catalog_limit(env):
    incident_id, view = env.planned()
    env.approve(view)
    always = ScriptedExecutor(*[ExecutorResult(False, error="busy", retryable=True)] * 5)
    done = env.runner(always).run(view.id)
    assert done.status == RemediationStatus.FAILED
    assert len(always.calls) == get_entry("rollback_deployment").max_attempts  # type: ignore[union-attr]
    assert env.incident_status(incident_id) == "ESCALATED"


def test_a_non_retryable_executor_failure_fails_immediately(env):
    incident_id, view = env.planned()
    env.approve(view)
    broken = ScriptedExecutor(ExecutorResult(False, error="target changed", retryable=False))
    done = env.runner(broken).run(view.id)
    assert done.status == RemediationStatus.FAILED and len(broken.calls) == 1
    assert "target changed" in (done.failure_reason or "")
    assert env.incident_status(incident_id) == "ESCALATED"


def test_target_no_longer_valid_is_detected_by_the_real_executor(env):
    _, view = env.planned()
    env.approve(view)
    ChangeRegistry(env.redis).record_deployment(
        service=SERVICE, version="1.2.0"
    )  # someone else deployed
    done = env.runner().run(view.id)
    assert done.status == RemediationStatus.FAILED
    assert "target changed" in (done.failure_reason or "")
    assert ChangeRegistry(env.redis).current_deployment(SERVICE)["version"] == "1.2.0"  # untouched


def test_an_execution_timeout_is_bounded_and_retried_only_when_safe(env):
    _, view = env.planned()
    env.approve(view)
    slow = ScriptedExecutor(ExecutorResult(True), ExecutorResult(True, {"second": 1}), delay=0.5)
    deadline_soon = env.remediations._clock  # noqa: SLF001

    def clock():  # every deadline looks 0.1s away
        return deadline_soon() + timedelta(
            seconds=get_entry("rollback_deployment").timeout_seconds - 0.1
        )  # type: ignore[union-attr]

    done = env.runner(slow, clock=clock).run(view.id)
    statuses = [e["status"] for e in env.remediations.executions(view.id)]
    assert statuses == ["TIMED_OUT", "TIMED_OUT"]  # retry-safe: retried, then bounded
    assert done.status == RemediationStatus.FAILED


def test_a_timeout_of_a_non_retry_safe_action_is_not_retried(env):
    def traffic_hypotheses(request):  # the scripted investigation, blaming capacity instead
        turn = support.hypotheses_turn(request)
        turn.actions[0].arguments["updates"][0]["cause_category"] = "traffic"
        return turn

    incident_id = support.open_incident(env.core)
    investigation_id = support.start(env.investigations, incident_id)
    script = [support.GATHER, traffic_hypotheses, support.conclude_turn]
    support.make_engine(
        env.investigations, env.core, env.evidence, FakeInvestigationModel(script)
    ).run(investigation_id)
    view = env.remediations.propose(
        RemediationProposal(
            incident_id=incident_id,
            investigation_id=investigation_id,
            action_id="scale_service",
            parameters={"service": SERVICE, "increase_by": 2},
            reason="r",
            expected_effect="e",
            source="planner",
            proposed_by="remediation-planner",
        ),
        idempotency_key="scale",
    )
    assert view.status == RemediationStatus.AWAITING_APPROVAL, view.failure_reason
    env.approve(view, approver="dave", roles=["on_call_engineer"])  # tier 1
    slow = ScriptedExecutor(ExecutorResult(True), ExecutorResult(True), delay=0.5)
    now = env.remediations._clock  # noqa: SLF001
    timeout = get_entry("scale_service").timeout_seconds  # type: ignore[union-attr]
    done = env.runner(slow, clock=lambda: now() + timedelta(seconds=timeout - 0.1)).run(view.id)
    assert done.status == RemediationStatus.FAILED
    assert "not retry-safe" in (done.failure_reason or "")
    assert len(slow.calls) == 1  # "add 2 instances" is never blindly repeated
    assert env.incident_status(incident_id) == "ESCALATED"


def test_a_worker_crash_after_acting_is_reconciled_without_acting_again(env):
    _, view = env.planned()
    env.approve(view)
    ticket = env.remediations.claim_execution(view.id, owner="runner-a")
    assert ticket is not None
    # runner-a acted, then died before recording the result
    env.executor.execute(
        ExecutionRequest(
            execution_id=str(ticket.execution_id),
            idempotency_key=ticket.idempotency_key,
            action_id=ticket.action_id,
            catalog_version=ticket.catalog_version,
            parameters=ticket.parameters,
            target_service=ticket.target_service,
            environment=ticket.environment,
            deadline=ticket.deadline,
        )
    )
    with env.session_factory() as session:
        session.execute(
            text(
                "UPDATE incident_core.remediations "
                "SET lease_expires_at = now() - interval '1 minute'"
            )
        )
        session.commit()
    assert env.remediations.pending_executions() == [view.id]
    done = env.runner(owner="runner-b").run(view.id)
    assert done.status == RemediationStatus.EXECUTED
    assert done.executor_result["reconciled"] is True  # type: ignore[index]
    assert [e["status"] for e in env.remediations.executions(view.id)] == ["SUCCEEDED"]
    assert env.redis.llen(OPS_LOG_KEY.format(service=SERVICE)) == 1  # acted exactly once
    assert "execution_interrupted" in [t["event"] for t in env.remediations.timeline(view.id)]


def test_a_worker_crash_before_acting_retries_a_retry_safe_action(env):
    _, view = env.planned()
    env.approve(view)
    assert env.remediations.claim_execution(view.id, owner="runner-a") is not None  # died at once
    with env.session_factory() as session:
        session.execute(
            text(
                "UPDATE incident_core.remediations "
                "SET lease_expires_at = now() - interval '1 minute'"
            )
        )
        session.commit()
    done = env.runner(owner="runner-b").run(view.id)
    assert done.status == RemediationStatus.EXECUTED
    assert [e["status"] for e in env.remediations.executions(view.id)] == ["FAILED", "SUCCEEDED"]


def test_a_live_lease_blocks_a_second_worker(env):
    _, view = env.planned()
    env.approve(view)
    assert env.remediations.claim_execution(view.id, owner="runner-a") is not None
    assert env.remediations.claim_execution(view.id, owner="runner-b") is None
