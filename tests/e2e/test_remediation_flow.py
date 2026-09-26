"""Phase 7 event flow on real Postgres + real Redis (DB 15), through the
real API for the human step:

    RCA_READY -> InvestigationCompleted (outbox -> relay -> Redis Streams)
      -> remediation worker: planner -> proposal -> policy -> AWAITING_APPROVAL
      -> POST /api/v1/remediations/{id}/approval (operator token, roster role)
      -> RemediationApproved (outbox -> relay -> Redis Streams)
      -> remediation worker: runner -> simulator executor -> EXECUTED
      -> VerificationRequested

with redelivered events that must not propose or execute twice. The
investigation uses the scripted model; no model API is called."""

from __future__ import annotations

import json
import os
import uuid

import pytest

from apps.api.dependencies import get_remediation_service
from apps.worker.config import WorkerSettings
from apps.worker.main import relay_once
from apps.worker.remediation_main import build_consumers, build_runtime
from packages.agents.fake import FakeInvestigationModel
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
TOKEN = "e2e-operator-token"


@pytest.fixture
def operator_api(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_TOKEN", TOKEN)
    monkeypatch.setenv("REMEDIATION_APPROVERS", "alice=service_owner,dave=on_call_engineer")
    get_remediation_service.cache_clear()
    yield {"Authorization": f"Bearer {TOKEN}"}
    get_remediation_service.cache_clear()


@pytest.fixture
def world(session_factory, evidence_session_factory, test_redis):
    registry = ChangeRegistry(test_redis)
    registry.seed()
    registry.record_deployment(service=SERVICE, version="1.1.0-bad")
    test_redis.set(
        f"chaos:{SERVICE}",
        json.dumps(
            {"scenario": "bad-deployment", "params": {}, "started_at": 0, "expires_at": 9e9}
        ),
    )
    core = IncidentCoreService(session_factory)
    investigations = InvestigationCoreService(session_factory)
    evidence = support.build_evidence_service(evidence_session_factory, core, test_redis)
    incident_id = support.open_incident(core)
    investigation_id = support.start(investigations, incident_id)
    support.make_engine(
        investigations, core, evidence, FakeInvestigationModel(support.happy_script())
    ).run(investigation_id)

    prefix = f"stream:test-remediation:{uuid.uuid4().hex[:8]}"
    worker = WorkerSettings(  # type: ignore[call-arg]
        _env_file=None,
        incident_core_database_url=os.environ["INCIDENT_CORE_DATABASE_URL"],
        outbox_stream_prefix=prefix,
        outbox_shard_count=2,
    )
    runtime = build_runtime(
        database_url=os.environ["INCIDENT_CORE_DATABASE_URL"],
        executor=SimulatorRemediationExecutor(test_redis),
        evidence=EvidenceStoreReader(evidence_session_factory),
        catalog=support.CATALOG,
        owner="e2e-remediation-worker",
        evidence_service=evidence,  # canned backends: no live telemetry
    )
    publisher = RedisStreamEventPublisher(test_redis, stream_prefix=prefix, shard_count=2)
    consumers = build_consumers(runtime, worker, test_redis)

    def pump() -> None:
        relay_once(session_factory, publisher)
        for consumer in consumers:
            consumer.run_once()

    return {
        "incident_id": incident_id,
        "runtime": runtime,
        "pump": pump,
        "publisher": publisher,
        "consumers": consumers,
        "redis": test_redis,
    }


def _status(session_factory, incident_id) -> str:
    with session_factory() as session:
        return session.get(IncidentRow, incident_id).status


def _redeliver(session_factory, world, event_type) -> None:
    with session_factory() as session:
        row = session.query(OutboxEventRow).filter(OutboxEventRow.event_type == event_type).one()
        envelope = OutboxEventEnvelope.model_validate(
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
    world["publisher"].publish(envelope)
    for consumer in world["consumers"]:
        consumer.run_once()


def test_rca_to_executed_rollback_through_events_and_the_api(
    world, client, operator_api, session_factory
):
    incident_id = world["incident_id"]
    world["pump"]()  # InvestigationCompleted -> planner -> proposal -> policy
    listed = client.get(f"/api/v1/incidents/{incident_id}/remediations").json()
    assert len(listed) == 1
    remediation = listed[0]
    assert remediation["status"] == "AWAITING_APPROVAL"
    assert remediation["action_id"] == "rollback_deployment"
    assert _status(session_factory, incident_id) == "AWAITING_APPROVAL"

    _redeliver(session_factory, world, "InvestigationCompleted")  # no second proposal
    assert len(client.get(f"/api/v1/incidents/{incident_id}/remediations").json()) == 1

    detail = client.get(f"/api/v1/remediations/{remediation['id']}").json()
    decision = detail["policy_decisions"][0]
    assert decision["decision"] == "REQUIRE_APPROVAL" and decision["required_approver_roles"] == [
        "service_owner"
    ]

    approval = {
        "approver": "alice",
        "decision": "approve",
        "proposal_hash": remediation["proposal_hash"],
        "policy_decision_id": remediation["policy_decision_id"],
    }
    response = client.post(
        f"/api/v1/remediations/{remediation['id']}/approval", json=approval, headers=operator_api
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "APPROVED"

    world["pump"]()  # RemediationApproved -> runner -> executor
    final = client.get(f"/api/v1/remediations/{remediation['id']}").json()
    assert final["remediation"]["status"] == "EXECUTED"
    assert final["executions"][0]["status"] == "SUCCEEDED"
    assert final["timeline"][-1]["event"] == "verification_requested"
    assert _status(session_factory, incident_id) == "VERIFYING"
    assert world["redis"].get(f"chaos:{SERVICE}") is None

    _redeliver(session_factory, world, "RemediationApproved")  # no second execution
    assert len(client.get(f"/api/v1/remediations/{remediation['id']}").json()["executions"]) == 1
    assert world["redis"].llen(OPS_LOG_KEY.format(service=SERVICE)) == 1


def test_operator_endpoints_are_authenticated_and_bound(world, client, operator_api):
    world["pump"]()
    remediation = client.get(f"/api/v1/incidents/{world['incident_id']}/remediations").json()[0]
    url = f"/api/v1/remediations/{remediation['id']}/approval"
    body = {
        "approver": "alice",
        "decision": "approve",
        "proposal_hash": remediation["proposal_hash"],
        "policy_decision_id": remediation["policy_decision_id"],
    }
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.post(url, json={**body, "approver": "mallory"}, headers=operator_api).status_code
        == 403
    )
    assert (
        client.post(url, json={**body, "approver": "dave"}, headers=operator_api).status_code == 403
    )  # role
    stale = {**body, "proposal_hash": "sha256:" + "0" * 64}
    assert client.post(url, json=stale, headers=operator_api).status_code == 409
    assert (
        client.post(
            url, json={**body, "roles": ["service_owner"]}, headers=operator_api
        ).status_code
        == 422
    )
    assert (
        client.get(f"/api/v1/remediations/{remediation['id']}").json()["remediation"]["status"]
        == "AWAITING_APPROVAL"
    )


def test_kill_switch_endpoint_blocks_execution(world, client, operator_api, session_factory):
    world["pump"]()
    remediation = client.get(f"/api/v1/incidents/{world['incident_id']}/remediations").json()[0]
    client.post(
        f"/api/v1/remediations/{remediation['id']}/approval",
        json={
            "approver": "alice",
            "decision": "approve",
            "proposal_hash": remediation["proposal_hash"],
            "policy_decision_id": remediation["policy_decision_id"],
        },
        headers=operator_api,
    )
    response = client.put(
        "/api/v1/kill-switches/global",
        json={"engaged": True, "actor": "carol", "reason": "freeze"},
        headers=operator_api,
    )
    assert response.status_code == 200 and response.json() == {"global": True}
    world["pump"]()
    final = client.get(f"/api/v1/remediations/{remediation['id']}").json()
    assert final["remediation"]["status"] == "CANCELLED" and final["executions"] == []
    assert world["redis"].get(f"chaos:{SERVICE}") is not None


def test_operator_endpoints_are_off_without_a_token(client, monkeypatch):
    monkeypatch.delenv("OPERATOR_API_TOKEN", raising=False)
    monkeypatch.setenv("OPERATOR_API_TOKEN", "")
    response = client.put("/api/v1/kill-switches/global", json={"engaged": True, "actor": "x"})
    assert response.status_code == 403
    assert client.get("/api/v1/action-catalog").json()["version"]
