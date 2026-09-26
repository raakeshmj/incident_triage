"""Phase 8 closed loop through the event architecture (real Postgres, real
Redis DB 15, the real API): workers are driven exactly as in production --
outbox relay -> Redis Streams -> consumer groups -- with canned, test-
controlled telemetry behind the real evidence service.

    RCA_READY -> InvestigationCompleted -> remediation worker (plan, policy)
      -> approval via the API -> RemediationApproved -> runner (baseline,
      rollback) -> VerificationRequested -> verification worker (start,
      ticks) -> VerificationCompleted -> incident RESOLVED / VERIFICATION_FAILED

Scenario 4 (duplicate events), 5 (crash during verification) and 6 (a new
alert during verification) run here; the read API the dashboard uses is
checked against the same data. No model API is called.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest
from sqlalchemy import text

from apps.api.dependencies import get_query_service, get_remediation_service
from apps.worker.config import WorkerSettings
from apps.worker.investigation_main import build_runtime as build_investigation_runtime
from apps.worker.main import relay_once
from apps.worker.remediation_main import RemediationSettings
from apps.worker.remediation_main import build_consumers as remediation_consumers
from apps.worker.remediation_main import build_runtime as build_remediation_runtime
from apps.worker.verification_main import VerificationSettings, tick_due
from apps.worker.verification_main import build_consumers as verification_consumers
from apps.worker.verification_main import build_engine as build_verification_engine
from packages.agents.config import InvestigationSettings
from packages.agents.fake import FakeInvestigationModel
from packages.domain.verification import Sample, VerificationStatus
from packages.evaluation.recording import EvidenceStoreReader
from packages.events.envelope import OutboxEventEnvelope
from packages.events.publisher import RedisStreamEventPublisher
from packages.incident.db.models import IncidentRow, OutboxEventRow
from packages.incident.investigations import InvestigationCoreService
from packages.incident.service import IncidentCoreService
from packages.remediation.executor import OPS_LOG_KEY, SimulatorRemediationExecutor
from simulator.changes.registry import ChangeRegistry
from tests import investigation_support as support

SERVICE = "checkout-service"
TOKEN = "e2e-lifecycle-token"
DB = os.environ["INCIDENT_CORE_DATABASE_URL"]


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_TOKEN", TOKEN)
    monkeypatch.setenv("REMEDIATION_APPROVERS", "alice=service_owner")
    get_remediation_service.cache_clear()
    get_query_service.cache_clear()
    yield {"Authorization": f"Bearer {TOKEN}"}
    get_remediation_service.cache_clear()
    get_query_service.cache_clear()


class Stack:
    def __init__(self, session_factory, evidence_session_factory, redis):
        self.session_factory, self.redis = session_factory, redis
        registry = ChangeRegistry(redis)
        registry.seed()
        registry.record_deployment(service=SERVICE, version="1.1.0-bad")
        redis.set(
            f"chaos:{SERVICE}",
            json.dumps(
                {"scenario": "bad-deployment", "params": {}, "started_at": 0, "expires_at": 9e9}
            ),
        )
        self.core = IncidentCoreService(session_factory)
        self.telemetry = support.Telemetry()
        self.evidence = support.controllable_evidence_service(
            evidence_session_factory, self.core, redis, self.telemetry
        )
        prefix = f"stream:test-lifecycle:{uuid.uuid4().hex[:8]}"
        self.worker = WorkerSettings(  # type: ignore[call-arg]
            _env_file=None,
            incident_core_database_url=DB,
            outbox_stream_prefix=prefix,
            outbox_shard_count=2,
        )
        self.publisher = RedisStreamEventPublisher(redis, stream_prefix=prefix, shard_count=2)
        self.remediation = build_remediation_runtime(
            database_url=DB,
            executor=SimulatorRemediationExecutor(redis),
            evidence=EvidenceStoreReader(evidence_session_factory),
            catalog=support.CATALOG,
            owner="remediation-worker",
            evidence_service=self.evidence,
            settings=RemediationSettings(_env_file=None, verification_time_scale=0.02),  # type: ignore[call-arg]
        )
        self.verifications, self.engine = build_verification_engine(
            database_url=DB,
            evidence_service=self.evidence,
            settings=VerificationSettings(_env_file=None),  # type: ignore[call-arg]
            owner="verification-worker",
        )
        self.consumers = remediation_consumers(
            self.remediation, self.worker, redis
        ) + verification_consumers(self.engine, self.worker, redis)
        self.investigation = build_investigation_runtime(
            database_url=DB,
            settings=InvestigationSettings(_env_file=None, investigation_debounce_seconds=0),  # type: ignore[call-arg]
            model_factory=lambda spec: FakeInvestigationModel(support.happy_script()),
            evidence_service=self.evidence,
            owner="investigation-worker",
        )

    def rca_ready(self) -> uuid.UUID:
        incident_id = support.open_incident(self.core)
        [investigation_id] = self.investigation.schedule_due()
        self.investigation.engine.run(investigation_id)
        assert self.status(incident_id) == "RCA_READY"
        return incident_id

    def pump(self) -> None:
        relay_once(self.session_factory, self.publisher)
        for consumer in self.consumers:
            consumer.run_once()

    def verify(self, deadline_seconds: float = 30) -> None:
        """Deliver pending events once, then run the verification worker's
        tick loop (consumer polls block, so they don't run every tick)."""
        self.pump()
        end = time.monotonic() + deadline_seconds
        while time.monotonic() < end:
            tick_due(self.verifications, self.engine)
            if not self.verifications.due() and self.status_of_latest() in {
                "PASSED",
                "FAILED",
                "TIMED_OUT",
            }:
                return
            time.sleep(0.02)

    def status_of_latest(self) -> str | None:
        with self.session_factory() as session:
            return session.execute(
                text(
                    "SELECT status FROM incident_core.verifications "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            ).scalar_one_or_none()

    def status(self, incident_id) -> str:
        with self.session_factory() as session:
            return session.get(IncidentRow, incident_id).status

    def events(self, event_type) -> list[OutboxEventRow]:
        with self.session_factory() as session:
            return list(
                session.query(OutboxEventRow).filter(OutboxEventRow.event_type == event_type)
            )

    def redeliver(self, event_type) -> None:
        for row in self.events(event_type):
            self.publisher.publish(
                OutboxEventEnvelope.model_validate(
                    {
                        "event_id": row.event_id,
                        "event_type": row.event_type,
                        "schema_version": 1,
                        "aggregate_type": row.aggregate_type,
                        "aggregate_id": row.aggregate_id,
                        "correlation_id": row.correlation_id,
                        "causation_id": None,
                        "producer": "incident-core",
                        "occurred_at": row.occurred_at,
                        "payload": row.payload,
                    }
                )
            )
        for consumer in self.consumers:
            consumer.run_once()


@pytest.fixture
def stack(session_factory, evidence_session_factory, test_redis):
    return Stack(session_factory, evidence_session_factory, test_redis)


def _approve(client, operator, incident_id):
    remediation = client.get(f"/api/v1/incidents/{incident_id}/remediations").json()[0]
    response = client.post(
        f"/api/v1/remediations/{remediation['id']}/approval",
        json={
            "approver": "alice",
            "decision": "approve",
            "proposal_hash": remediation["proposal_hash"],
            "policy_decision_id": remediation["policy_decision_id"],
        },
        headers=operator,
    )
    assert response.status_code == 200, response.text
    return remediation


def test_closed_loop_resolves_and_the_read_api_shows_it(stack, client, operator):
    incident_id = stack.rca_ready()
    stack.pump()  # InvestigationCompleted -> proposal -> policy
    remediation = _approve(client, operator, incident_id)
    stack.pump()  # RemediationApproved -> baseline + rollback -> VerificationRequested
    assert stack.status(incident_id) == "VERIFYING"
    stack.telemetry.error_rate = 0.002  # the rollback took
    stack.verify()
    assert stack.status(incident_id) == "RESOLVED"
    stack.pump()
    assert len(stack.events("IncidentResolved")) == 1

    listed = client.get("/api/v1/incidents", params={"status": "RESOLVED"}).json()
    item = next(i for i in listed["items"] if i["id"] == str(incident_id))
    assert item["remediation_status"] == "EXECUTED" and item["verification_status"] == "PASSED"
    assert item["investigation_status"] == "COMPLETED" and item["ended_at"]
    assert client.get("/api/v1/incidents", params={"status": "TRIAGING"}).json()["items"] == []

    detail = client.get(f"/api/v1/incidents/{incident_id}/detail").json()
    kinds = {e["kind"] for e in detail["timeline"]}
    assert {
        "alert",
        "state",
        "investigation",
        "evidence",
        "hypothesis",
        "rca",
        "remediation",
        "verification",
    } <= kinds
    verification = detail["verifications"][0]
    assert verification["verification"]["status"] == "PASSED"
    assert verification["verification"]["baseline"]["deployment"]["version"] == "1.1.0-bad"
    evidence_id = verification["observations"][0]["evidence_ids"][0]
    evidence = client.get(f"/api/v1/evidence/{evidence_id}").json()
    assert evidence["incident_id"] == str(incident_id) and evidence["requested_by"].startswith(
        "verification:"
    )
    assert detail["remediations"][0]["remediation"]["id"] == remediation["id"]

    overview = client.get("/api/v1/overview").json()
    assert overview["incidents_by_status"]["RESOLVED"] >= 1
    metrics = client.get("/api/v1/metrics").json()
    assert metrics["verification_latency"]["count"] >= 1
    assert metrics["successful_remediation_rate"]["rate"] == 1.0


def test_scenario_4_duplicate_events_have_no_duplicate_side_effects(stack, client, operator):
    incident_id = stack.rca_ready()
    stack.pump()
    stack.redeliver("InvestigationCompleted")
    assert len(client.get(f"/api/v1/incidents/{incident_id}/remediations").json()) == 1
    _approve(client, operator, incident_id)
    stack.pump()
    stack.redeliver("RemediationApproved")
    stack.redeliver("VerificationRequested")
    stack.telemetry.error_rate = 0.002
    stack.verify()
    stack.redeliver("VerificationRequested")
    assert stack.redis.llen(OPS_LOG_KEY.format(service=SERVICE)) == 1  # rolled back exactly once
    assert len(stack.events("RemediationStarted")) == 1
    assert len(stack.events("VerificationStarted")) == 1
    assert len(stack.events("VerificationCompleted")) == 1
    assert stack.status(incident_id) == "RESOLVED"


def test_scenario_5_a_verifier_crash_is_recovered_without_duplicate_effects(
    stack, client, operator, session_factory
):
    incident_id = stack.rca_ready()
    stack.pump()
    _approve(client, operator, incident_id)
    stack.pump()
    stack.pump()  # VerificationRequested -> started
    verification_id = stack.verifications.for_incident(incident_id)[0].id
    time.sleep(1.5)  # past the grace period
    doomed = stack.verifications.claim_due(verification_id, owner="doomed-worker", lease_seconds=60)
    assert doomed is not None  # ...and that worker process dies here
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE incident_core.verifications "
                "SET lease_expires_at = now() - interval '1 second'"
            )
        )
        session.commit()
    stack.telemetry.error_rate = 0.002
    stack.verify()
    assert stack.status(incident_id) == "RESOLVED"
    from packages.domain.errors import LeaseLostError

    with pytest.raises(LeaseLostError):
        stack.verifications.record_observation(
            doomed,
            sample=Sample(),
            evidence_ids=[],
            observed_at=stack.verifications.get(verification_id).completed_at,
        )
    assert stack.verifications.get(verification_id).status == VerificationStatus.PASSED
    assert stack.redis.llen(OPS_LOG_KEY.format(service=SERVICE)) == 1


def test_scenario_6_a_new_alert_during_verification_blocks_resolution(stack, client, operator):
    incident_id = stack.rca_ready()
    stack.pump()
    _approve(client, operator, incident_id)
    stack.pump()
    stack.pump()  # started
    response = client.post(
        "/api/v1/alerts",
        json={
            "source": "prometheus",
            "external_id": f"latency-{uuid.uuid4()}",
            "labels": {
                "service": SERVICE,
                "environment": "production",
                "region": "us-east-1",
                "alertname": "HighP95Latency",
                "alert_type": "latency",
            },
            "severity": "critical",
        },
    )
    assert response.status_code == 202 and response.json()["incident_id"] == str(incident_id)
    stack.telemetry.error_rate = 0.002  # the old symptom recovered...
    stack.verify()
    assert stack.status(incident_id) == "VERIFICATION_FAILED"  # ...but a new alert is firing
    verification = stack.verifications.for_incident(incident_id)[0]
    assert (
        verification.status == VerificationStatus.FAILED
        and verification.next_action == "reinvestigate"
    )
    assert stack.events("IncidentResolved") == []
    # deterministic next step: the investigation scheduler re-investigates
    [again] = stack.investigation.schedule_due()
    assert stack.status(incident_id) == "INVESTIGATING"
    assert (
        InvestigationCoreService(stack.session_factory)
        .load_state(again)
        .investigation.attempt_number
        == 2
    )


def test_the_read_api_filters_and_handles_missing_records(stack, client):
    incident_id = support.open_incident(stack.core)
    all_items = client.get("/api/v1/incidents").json()
    assert all_items["total"] == 1 and all_items["items"][0]["status"] == "TRIAGING"
    assert all_items["services"] == [SERVICE]
    assert client.get("/api/v1/incidents", params={"severity": "info"}).json()["total"] == 0
    assert client.get("/api/v1/incidents", params={"service": "nope"}).json()["total"] == 0
    assert (
        client.get("/api/v1/incidents", params={"since": "2999-01-01T00:00:00Z"}).json()["total"]
        == 0
    )
    assert client.get(f"/api/v1/incidents/{uuid.uuid4()}/detail").status_code == 404
    assert client.get(f"/api/v1/evidence/{uuid.uuid4()}").status_code == 404
    detail = client.get(f"/api/v1/incidents/{incident_id}/detail").json()
    assert detail["remediations"] == [] and detail["verifications"] == []
    metrics = client.get("/api/v1/metrics").json()
    assert metrics["verification_latency"] == {
        "count": 0,
        "unit": "seconds",
        "p50": None,
        "p90": None,
        "mean": None,
        "max": None,
    }
