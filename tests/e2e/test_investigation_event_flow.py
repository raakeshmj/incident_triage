"""Event-driven start, end to end on real Postgres + real Redis:

    alert -> incident (TRIAGING) -> scheduler (debounce elapsed)
      -> InvestigationStarted in the outbox -> relay -> Redis Streams
      -> investigation worker's consumer group -> engine -> RCA_READY

with the scripted model and canned telemetry backends -- and a redelivered
event that must not start or run anything twice. No model API is called.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from apps.worker.config import WorkerSettings
from apps.worker.investigation_main import build_consumers, build_runtime
from apps.worker.main import relay_once
from packages.agents.config import InvestigationSettings
from packages.agents.fake import FakeInvestigationModel
from packages.domain.investigation import InvestigationStatus
from packages.events.envelope import OutboxEventEnvelope
from packages.events.publisher import RedisStreamEventPublisher
from packages.incident.db.models import IncidentRow, InvestigationRow, OutboxEventRow
from packages.incident.service import IncidentCoreService
from simulator.changes.registry import ChangeRegistry
from tests import investigation_support as support


def test_investigation_starts_from_the_event_and_survives_redelivery(
    engine, session_factory, evidence_session_factory, test_redis
):
    import os

    registry = ChangeRegistry(test_redis)
    registry.seed()
    registry.record_deployment(service="checkout-service", version="1.1.0-bad")
    core = IncidentCoreService(session_factory)
    evidence = support.build_evidence_service(evidence_session_factory, core, test_redis)

    settings = InvestigationSettings(  # type: ignore[call-arg]
        _env_file=None,
        investigation_model="claude-haiku-4-5",
        investigation_debounce_seconds=0,
    )
    specs = []
    model = FakeInvestigationModel(support.happy_script())
    runtime = build_runtime(
        database_url=os.environ["INCIDENT_CORE_DATABASE_URL"],
        settings=settings,
        model_factory=lambda spec: specs.append(spec) or model,
        evidence_service=evidence,
        owner="e2e-worker",
    )
    prefix = f"stream:test-investigation:{uuid.uuid4().hex[:8]}"
    worker = WorkerSettings(  # type: ignore[call-arg]
        _env_file=None,
        incident_core_database_url=os.environ["INCIDENT_CORE_DATABASE_URL"],
        outbox_stream_prefix=prefix,
        outbox_shard_count=2,
    )
    publisher = RedisStreamEventPublisher(test_redis, stream_prefix=prefix, shard_count=2)
    consumers = build_consumers(runtime, worker, test_redis)

    incident_id = support.open_incident(core)
    [investigation_id] = runtime.schedule_due()
    assert runtime.schedule_due() == []  # already INVESTIGATING: nothing new

    relay_once(session_factory, publisher)
    sum(consumer.run_once() for consumer in consumers)

    with session_factory() as session:
        incident = session.get(IncidentRow, incident_id)
        investigation = session.get(InvestigationRow, investigation_id)
        started_events = (
            session.execute(
                select(OutboxEventRow).where(OutboxEventRow.event_type == "InvestigationStarted")
            )
            .scalars()
            .all()
        )
    assert investigation.status == InvestigationStatus.COMPLETED.value
    assert incident.status == "RCA_READY"
    # the configured model was stamped on the investigation and handed to the factory
    assert investigation.model_name == "claude-haiku-4-5"
    assert [s.model for s in specs] == ["claude-haiku-4-5"]

    # redelivery: the same InvestigationStarted event again
    duplicate = OutboxEventEnvelope.model_validate(
        {
            "event_id": started_events[0].event_id,
            "event_type": started_events[0].event_type,
            "schema_version": 1,
            "aggregate_type": started_events[0].aggregate_type,
            "aggregate_id": started_events[0].aggregate_id,
            "correlation_id": started_events[0].correlation_id,
            "causation_id": None,
            "producer": "incident-core",
            "occurred_at": started_events[0].occurred_at,
            "payload": started_events[0].payload,
        }
    )
    publisher.publish(duplicate)
    sum(consumer.run_once() for consumer in consumers)
    assert model.remaining == 0 and len(specs) == 1  # nothing ran twice
    with session_factory() as session:
        count = (
            session.execute(
                select(InvestigationRow).where(InvestigationRow.incident_id == incident_id)
            )
            .scalars()
            .all()
        )
    assert len(count) == 1
