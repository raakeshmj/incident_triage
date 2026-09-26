"""Phase 8 verification on real Postgres + isolated Redis: the full closed
loop (RCA -> rollback -> verification -> RESOLVED), failure paths
(re-investigation, escalation, timeout), transient recovery, crash
recovery, duplicates, stale results and new alerts during verification.

Telemetry comes from a controllable canned Prometheus behind the real
evidence service; the rollback runs against the simulator control plane
in Redis DB 15. No model API, no live telemetry."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.agents.fake import FakeInvestigationModel
from packages.domain.errors import LeaseLostError
from packages.domain.remediation import RemediationStatus
from packages.domain.verification import Sample, VerificationStatus
from packages.evaluation.recording import EvidenceStoreReader
from packages.incident.db.models import IncidentRow, OutboxEventRow
from packages.incident.investigations import InvestigationCoreService
from packages.incident.remediations import RemediationCoreService
from packages.incident.verifications import VerificationCoreService
from packages.remediation.executor import OPS_LOG_KEY, SimulatorRemediationExecutor
from packages.remediation.planner import RemediationPlanner
from packages.remediation.runner import RemediationRunner
from packages.verification.engine import BaselineCollector, VerificationEngine
from packages.verification.observer import EvidenceObserver
from simulator.changes.registry import ChangeRegistry
from tests import investigation_support as support
from tests.factories import make_alert_command

SERVICE = "checkout-service"
SCALE = 0.002  # rollback: grace 0.12s, poll 0.03s, timeout ~0.78s


class Loop:
    def __init__(self, session_factory, core, evidence_session_factory, redis, max_attempts=2):
        self.session_factory, self.core, self.redis = session_factory, core, redis
        self.telemetry = support.Telemetry()
        self.evidence = support.controllable_evidence_service(
            evidence_session_factory, core, redis, self.telemetry
        )
        self.investigations = InvestigationCoreService(session_factory)
        self.remediations = RemediationCoreService(
            session_factory, topology=support.CATALOG, verification_time_scale=SCALE
        )
        self.verifications = VerificationCoreService(
            session_factory, max_investigation_attempts=max_attempts
        )
        observer = EvidenceObserver(self.evidence)
        self.runner = RemediationRunner(
            self.remediations,
            SimulatorRemediationExecutor(redis),
            owner="runner",
            baseline=BaselineCollector(self.remediations, observer),
        )
        self.engine = VerificationEngine(
            self.verifications, observer, owner="verifier-a", lease_seconds=5
        )

    def executed(self):
        """RCA_READY via the scripted investigation, then plan, approve, execute."""
        incident_id = support.open_incident(self.core)
        investigation_id = support.start(self.investigations, incident_id)
        support.make_engine(
            self.investigations,
            self.core,
            self.evidence,
            FakeInvestigationModel(support.happy_script()),
        ).run(investigation_id)
        report = self.investigations.get_trace(investigation_id)["rca_report"]["report"]
        proposal, _ = RemediationPlanner(EvidenceStoreReader(self.evidence._session_factory)).plan(  # noqa: SLF001
            incident_id, investigation_id, report
        )
        view = self.remediations.propose(proposal, idempotency_key=f"planner:{investigation_id}")  # type: ignore[arg-type]
        self.remediations.decide_approval(
            view.id,
            approver="alice",
            approver_roles=["service_owner"],
            approve=True,
            proposal_hash=view.proposal_hash,
            policy_decision_id=view.policy_decision_id,  # type: ignore[arg-type]
        )
        done = self.runner.run(view.id)
        assert done.status == RemediationStatus.EXECUTED, done.failure_reason
        return incident_id, done

    def status(self, incident_id) -> str:
        with self.session_factory() as session:
            return session.get(IncidentRow, incident_id).status

    def events(self, event_type) -> list[dict]:
        with self.session_factory() as session:
            return [
                r.payload
                for r in session.query(OutboxEventRow).filter(
                    OutboxEventRow.event_type == event_type
                )
            ]


@pytest.fixture
def loop(session_factory, core, evidence_session_factory, test_redis):
    registry = ChangeRegistry(test_redis)
    registry.seed()
    registry.record_deployment(service=SERVICE, version="1.1.0-bad")
    test_redis.set(
        f"chaos:{SERVICE}",
        json.dumps(
            {"scenario": "bad-deployment", "params": {}, "started_at": 0, "expires_at": 9e9}
        ),
    )
    return Loop(session_factory, core, evidence_session_factory, test_redis)


def _recover(loop):
    loop.telemetry.error_rate = 0.002


# --- the closed loop ------------------------------------------------------------------


def test_rollback_is_verified_from_evidence_and_the_incident_resolves(loop):
    incident_id, remediation = loop.executed()
    assert loop.status(incident_id) == "VERIFYING"
    verification_id = remediation.verification_ref
    view = loop.verifications.get(verification_id)
    assert view.status == VerificationStatus.PENDING
    # the baseline was observed before the rollback: sick, on the bad build
    assert view.baseline["deployment"]["version"] == "1.1.0-bad"  # type: ignore[index]
    assert view.baseline["health"]["signals"]["error_rate"] == pytest.approx(0.31)  # type: ignore[index]

    _recover(loop)
    done = loop.engine.run_until_done(verification_id)
    assert done.status == VerificationStatus.PASSED and done.next_action == "resolve"
    assert done.consecutive_successes == done.spec["required_consecutive"] == 3
    assert loop.status(incident_id) == "RESOLVED"
    assert len(loop.events("IncidentResolved")) == 1
    assert [e["status"] for e in loop.events("VerificationCompleted")] == ["PASSED"]
    assert len(loop.events("VerificationStarted")) == 1

    observations = loop.verifications.observations(verification_id)
    assert len(observations) == 3 and all(o["passed"] for o in observations)
    assert all(o["evidence_ids"] for o in observations)
    links = loop.verifications.evidence_links(verification_id)
    assert {link["role"] for link in links} == {"baseline", "observation"}
    assert {link["poll_sequence"] for link in links} == {0, 1, 2, 3}


def test_resolution_needs_evidence_not_the_executor_report(loop):
    incident_id, remediation = loop.executed()  # the executor said "succeeded"
    done = loop.engine.run_until_done(remediation.verification_ref)  # ...but errors never drop
    assert done.status == VerificationStatus.FAILED
    assert "error_rate" in (done.failure_reason or "")
    assert loop.status(incident_id) != "RESOLVED"


def test_failed_verification_goes_back_to_investigation_then_escalates(loop, session_factory):
    incident_id, remediation = loop.executed()
    first = loop.engine.run_until_done(remediation.verification_ref)
    assert first.status == VerificationStatus.FAILED and first.next_action == "reinvestigate"
    assert loop.status(incident_id) == "VERIFICATION_FAILED"
    assert incident_id in loop.investigations.due_for_investigation(debounce_seconds=3600)
    started = support.start(loop.investigations, incident_id)
    assert loop.status(incident_id) == "INVESTIGATING"
    with session_factory() as session:
        assert session.get(IncidentRow, incident_id).attempt_count == 2
    assert started is not None

    # second loop, attempts now exhausted: a second failure escalates
    support.make_engine(
        loop.investigations,
        loop.core,
        loop.evidence,
        FakeInvestigationModel(support.happy_script()),
    ).run(started)
    assert loop.status(incident_id) == "RCA_READY"
    with session_factory() as session:  # the rollback already happened; stage the next one
        session.execute(text("UPDATE incident_core.remediations SET status = status"))
    ChangeRegistry(loop.redis).record_deployment(service=SERVICE, version="1.1.0-bad")
    report = loop.investigations.get_trace(started)["rca_report"]["report"]
    proposal, _ = RemediationPlanner(EvidenceStoreReader(loop.evidence._session_factory)).plan(
        incident_id, started, report
    )  # noqa: SLF001
    second = loop.remediations.propose(proposal, idempotency_key=f"planner:{started}")  # type: ignore[arg-type]
    assert second.status == RemediationStatus.AWAITING_APPROVAL, second.failure_reason
    loop.remediations.decide_approval(
        second.id,
        approver="alice",
        approver_roles=["service_owner"],
        approve=True,
        proposal_hash=second.proposal_hash,
        policy_decision_id=second.policy_decision_id,  # type: ignore[arg-type]
    )
    executed = loop.runner.run(second.id)
    final = loop.engine.run_until_done(executed.verification_ref)  # type: ignore[arg-type]
    assert final.status == VerificationStatus.FAILED and final.next_action == "escalate"
    assert loop.status(incident_id) == "ESCALATED"
    escalated = loop.events("IncidentEscalated")
    assert len(escalated) == 1 and "attempts exhausted" in escalated[0]["reason"]


def test_a_failed_verification_never_triggers_another_execution(loop):
    incident_id, remediation = loop.executed()
    loop.engine.run_until_done(remediation.verification_ref)
    assert loop.redis.llen(OPS_LOG_KEY.format(service=SERVICE)) == 1
    assert loop.remediations.pending_executions() == []


def test_evidence_outage_times_out_and_escalates(loop):
    incident_id, remediation = loop.executed()
    loop.telemetry.down = True
    done = loop.engine.run_until_done(remediation.verification_ref)
    assert done.status == VerificationStatus.TIMED_OUT and done.next_action == "escalate"
    assert loop.status(incident_id) == "ESCALATED"
    observations = loop.verifications.observations(remediation.verification_ref)
    assert observations and not any(o["conclusive"] for o in observations)


def test_a_transient_recovery_does_not_pass(loop):
    incident_id, remediation = loop.executed()
    # healthy, sick, then healthy for good: the streak restarts after the relapse
    loop.telemetry.script = [0.002, 0.3]
    _recover(loop)
    done = loop.engine.run_until_done(remediation.verification_ref)
    passed = [o["passed"] for o in loop.verifications.observations(remediation.verification_ref)]
    assert passed[:2] == [True, False]
    assert done.status == VerificationStatus.PASSED and len(passed) == 5


def test_the_wrong_version_fails_immediately(loop):
    incident_id, remediation = loop.executed()
    ChangeRegistry(loop.redis).record_deployment(
        service=SERVICE, version="1.2.0"
    )  # someone deployed
    _recover(loop)
    done = loop.engine.run_until_done(remediation.verification_ref)
    assert done.status == VerificationStatus.FAILED and "1.2.0" in (done.failure_reason or "")
    assert len(loop.verifications.observations(remediation.verification_ref)) == 1


# --- duplicates, crashes, stale results, new alerts ------------------------------------


def test_duplicate_starts_and_ticks_are_harmless(loop):
    _, remediation = loop.executed()
    vid = remediation.verification_ref
    first, second = loop.engine.start(vid), loop.engine.start(vid)
    assert first.started_at == second.started_at
    assert len(loop.events("VerificationStarted")) == 1
    other = VerificationEngine(
        loop.verifications, EvidenceObserver(loop.evidence), owner="verifier-b"
    )
    ticket = loop.verifications.claim_due(vid, owner="verifier-a", lease_seconds=30) or None
    if ticket is None:  # grace not over yet: wait until the first poll is due
        import time

        time.sleep(0.2)
        ticket = loop.verifications.claim_due(vid, owner="verifier-a", lease_seconds=30)
    assert ticket is not None
    assert other.tick(vid) is None  # a live lease: the second worker does nothing


def test_a_crashed_verifier_is_superseded_and_its_late_write_rejected(loop, session_factory):
    incident_id, remediation = loop.executed()
    vid = remediation.verification_ref
    loop.engine.start(vid)
    import time

    time.sleep(0.2)
    stale = loop.verifications.claim_due(vid, owner="verifier-a", lease_seconds=30)
    assert stale is not None  # ...and verifier-a dies holding it
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE incident_core.verifications "
                "SET lease_expires_at = now() - interval '1 minute'"
            )
        )
        session.commit()
    assert vid in loop.verifications.due()
    _recover(loop)
    survivor = VerificationEngine(
        loop.verifications, EvidenceObserver(loop.evidence), owner="verifier-b"
    )
    done = survivor.run_until_done(vid)
    assert done.status == VerificationStatus.PASSED and loop.status(incident_id) == "RESOLVED"
    with pytest.raises(LeaseLostError):  # verifier-a wakes up: its observation is refused
        loop.verifications.record_observation(
            stale, sample=Sample(), evidence_ids=[], observed_at=done.completed_at
        )
    assert loop.verifications.get(vid).status == VerificationStatus.PASSED


def test_a_stale_verdict_cannot_close_an_incident_a_human_took_over(loop, session_factory):
    incident_id, remediation = loop.executed()
    with session_factory() as session:
        session.execute(
            text("UPDATE incident_core.incidents SET status = 'ESCALATED', version = version + 1")
        )
        session.commit()
    _recover(loop)
    done = loop.engine.run_until_done(remediation.verification_ref)
    assert done.status == VerificationStatus.PASSED and done.next_action == "none"
    assert "not applied" in (done.failure_reason or "")
    assert loop.status(incident_id) == "ESCALATED"
    assert loop.events("IncidentResolved") == []


def test_a_new_alert_during_verification_blocks_resolution(loop):
    incident_id, remediation = loop.executed()
    vid = remediation.verification_ref
    loop.engine.start(vid)
    result = loop.core.handle_alert_received(
        make_alert_command(
            service=SERVICE,
            external_id=str(uuid.uuid4()),
            extra_labels={
                "region": "us-east-1",
                "alertname": "HighP95Latency",
                "alert_type": "latency",
            },
        )
    )
    assert result.incident_id == incident_id  # correlated into the incident being verified
    assert loop.status(incident_id) == "VERIFYING"
    _recover(loop)
    done = loop.engine.run_until_done(vid)
    assert done.status == VerificationStatus.FAILED
    assert "alert(s) fired since verification started" in (done.failure_reason or "")
    assert loop.status(incident_id) != "RESOLVED"


def test_resolved_incidents_do_not_absorb_new_alerts(loop):
    incident_id, remediation = loop.executed()
    _recover(loop)
    loop.engine.run_until_done(remediation.verification_ref)
    assert loop.status(incident_id) == "RESOLVED"
    later = support.open_incident(loop.core)
    assert later != incident_id  # a new episode is a new incident


def test_baselines_are_required_before_acting(loop):
    incident_id = support.open_incident(loop.core)
    investigation_id = support.start(loop.investigations, incident_id)
    support.make_engine(
        loop.investigations,
        loop.core,
        loop.evidence,
        FakeInvestigationModel(support.happy_script()),
    ).run(investigation_id)
    report = loop.investigations.get_trace(investigation_id)["rca_report"]["report"]
    proposal, _ = RemediationPlanner(EvidenceStoreReader(loop.evidence._session_factory)).plan(
        incident_id, investigation_id, report
    )  # noqa: SLF001
    view = loop.remediations.propose(proposal, idempotency_key="k")  # type: ignore[arg-type]
    loop.remediations.decide_approval(
        view.id,
        approver="alice",
        approver_roles=["service_owner"],
        approve=True,
        proposal_hash=view.proposal_hash,
        policy_decision_id=view.policy_decision_id,  # type: ignore[arg-type]
    )
    loop.telemetry.down = True  # no "before" can be observed
    done = loop.runner.run(view.id)
    assert done.status == RemediationStatus.FAILED
    assert "baseline unavailable" in (done.failure_reason or "")
    assert loop.redis.llen(OPS_LOG_KEY.format(service=SERVICE)) == 0  # never acted blind
    assert loop.redis.get(f"chaos:{SERVICE}") is not None


def test_verification_specs_and_evidence_links_are_immutable(loop, session_factory):
    _, remediation = loop.executed()
    _recover(loop)
    loop.engine.run_until_done(remediation.verification_ref)
    for statement in (
        "UPDATE incident_core.verifications SET spec = '{}'::jsonb",
        "UPDATE incident_core.verification_observations SET passed = NOT passed",
        "UPDATE incident_core.verification_evidence SET role = 'observation'",
        "UPDATE incident_core.remediation_baselines SET values = '{}'::jsonb",
        "DELETE FROM incident_core.verifications",
    ):
        with session_factory() as session, pytest.raises(DBAPIError):
            session.execute(text(statement))
            session.commit()


def test_verification_windows_are_persisted_and_action_specific(loop):
    _, remediation = loop.executed()
    spec = loop.verifications.get(remediation.verification_ref).spec
    assert spec["grace_seconds"] == pytest.approx(60 * SCALE)
    assert spec["poll_interval_seconds"] == pytest.approx(15 * SCALE)
    assert spec["policy_version"] and spec["action_id"] == "rollback_deployment"
    assert timedelta(seconds=spec["timeout_seconds"]) > timedelta(seconds=spec["grace_seconds"])
