"""The remediation worker (Phase 7).

One process, deterministic code only -- no model anywhere in it:

1. **Plan** -- `InvestigationCompleted` (an accepted RCA): the planner maps
   the RCA to at most one catalog proposal; incident-core records it,
   evaluates policy on it and routes it (rejected, or awaiting a human).
   Idempotent per investigation (`planner:{investigation_id}`).
2. **Execute** -- `RemediationApproved`: the runner claims an execution
   attempt from incident-core (which re-checks kill switches, the approval's
   binding, the catalog entry and attempt limits) and calls the executor.
3. **Sweep** -- expire approvals past their timeout (never auto-approve);
   pick up APPROVED remediations whose event was lost and EXECUTING ones
   whose worker died (reconciled via the executor's idempotency record).

Humans approve through the API (`POST /api/v1/remediations/{id}/approval`).
Every consumer is deduplicated by the `consumed_events` ledger.
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import redis as redis_lib
from pydantic_settings import BaseSettings, SettingsConfigDict

from apps.worker.config import WorkerSettings
from apps.worker.consumer_main import make_is_duplicate, make_mark_processed
from packages.domain.errors import (
    ConcurrentModificationError,
    IncidentNotFoundError,
    RemediationStateError,
)
from packages.domain.events import (
    EVENT_TYPE_INVESTIGATION_COMPLETED,
    EVENT_TYPE_REMEDIATION_APPROVED,
)
from packages.events.consumer import RedisStreamConsumer
from packages.events.envelope import OutboxEventEnvelope
from packages.events.streams import all_stream_names, consumer_group_name, consumer_identity
from packages.evidence.db.base import make_engine as make_evidence_engine
from packages.evidence.db.base import make_session_factory as make_evidence_sessions
from packages.evidence.scope import ServiceCatalog
from packages.evidence.service import EvidenceService
from packages.incident.db.base import make_engine, make_session_factory
from packages.incident.investigations import InvestigationCoreService
from packages.incident.remediations import RemediationCoreService
from packages.remediation.executor import RemediationExecutor, SimulatorRemediationExecutor
from packages.remediation.planner import EvidenceReader, RemediationPlanner
from packages.remediation.runner import RemediationRunner
from packages.telemetry.heartbeat import Heartbeat
from packages.telemetry.logging import configure_logging, get_logger
from packages.verification.engine import BaselineCollector
from packages.verification.observer import EvidenceObserver

log = get_logger(__name__)

CONSUMER_PURPOSE = "remediation-worker"


class RemediationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    remediation_approval_timeout_minutes: int = 30
    remediation_lease_seconds: int = 120
    remediation_service_catalog_path: str | None = None
    # Multiplies every verification window (grace, poll interval, window,
    # timeout) declared in the action catalog. 1.0 in production; tests and
    # demos shorten it. Recorded in each verification's persisted spec.
    verification_time_scale: float = 1.0


class _StoredEvidence:
    """Stored evidence records only -- the planner never queries a backend."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def get_incident_evidence(self, incident_id: uuid.UUID) -> list[Any]:
        from packages.evidence import repository

        with self._session_factory() as session:
            return repository.list_incident_records(session, incident_id)


@dataclass
class RemediationRuntime:
    remediations: RemediationCoreService
    investigations: InvestigationCoreService
    planner: RemediationPlanner
    runner: RemediationRunner

    def plan(self, investigation_id: uuid.UUID) -> uuid.UUID | None:
        trace = self.investigations.get_trace(investigation_id)
        rca = trace.get("rca_report")
        if not rca:
            return None
        incident_id = uuid.UUID(trace["investigation"]["incident_id"])
        proposal, why = self.planner.plan(incident_id, investigation_id, rca["report"])
        if proposal is None:
            log.info("remediation.no_proposal", investigation_id=str(investigation_id), why=why)
            return None
        try:
            view = self.remediations.propose(
                proposal, idempotency_key=f"planner:{investigation_id}", correlation_id=incident_id
            )
        except (ConcurrentModificationError, IncidentNotFoundError):
            raise  # retried by redelivery
        return view.id

    def execute(self, remediation_id: uuid.UUID) -> None:
        try:
            view = self.runner.run(remediation_id)
            log.info(
                "remediation.run", remediation_id=str(remediation_id), status=view.status.value
            )
        except RemediationStateError as exc:
            log.info("remediation.run_skipped", remediation_id=str(remediation_id), why=str(exc))

    def sweep(self) -> int:
        expired = self.remediations.expire_approvals()
        pending = self.remediations.pending_executions()
        for remediation_id in pending:
            self.execute(remediation_id)
        return len(expired) + len(pending)


def build_runtime(
    *,
    database_url: str | None = None,
    evidence_database_url: str | None = None,
    executor: RemediationExecutor | None = None,
    evidence: EvidenceReader | None = None,
    catalog: ServiceCatalog | None = None,
    settings: RemediationSettings | None = None,
    owner: str | None = None,
    evidence_service: EvidenceService | None = None,
) -> RemediationRuntime:
    settings = settings or RemediationSettings()
    sessions = make_session_factory(make_engine(database_url))
    catalog = catalog or ServiceCatalog.load(
        settings.remediation_service_catalog_path
        or os.environ.get(
            "EVIDENCE_SERVICE_CATALOG_PATH", "infrastructure/evidence/service-catalog.json"
        )
    )
    remediations = RemediationCoreService(
        sessions,
        topology=catalog,
        approval_timeout=timedelta(minutes=settings.remediation_approval_timeout_minutes),
        verification_time_scale=settings.verification_time_scale,
    )
    if evidence_service is None:
        from apps.evidence.dependencies import get_evidence_service

        evidence_service = get_evidence_service()
    if executor is None:
        executor = SimulatorRemediationExecutor(
            redis_lib.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True
            )
        )
    if evidence is None:
        evidence = _StoredEvidence(
            make_evidence_sessions(make_evidence_engine(evidence_database_url))
        )
    return RemediationRuntime(
        remediations=remediations,
        investigations=InvestigationCoreService(sessions),
        planner=RemediationPlanner(evidence),
        runner=RemediationRunner(
            remediations,
            executor,
            owner=owner or f"{socket.gethostname()}:{os.getpid()}",
            lease_seconds=settings.remediation_lease_seconds,
            baseline=BaselineCollector(remediations, EvidenceObserver(evidence_service)),
        ),
    )


def build_consumers(
    runtime: RemediationRuntime, worker: WorkerSettings, redis_client: redis_lib.Redis
) -> list[RedisStreamConsumer]:
    session_factory = make_session_factory(make_engine(worker.incident_core_database_url))

    def handle(envelope: OutboxEventEnvelope) -> None:
        if envelope.event_type == EVENT_TYPE_INVESTIGATION_COMPLETED:
            runtime.plan(uuid.UUID(str(envelope.payload["investigation_id"])))
        elif envelope.event_type == EVENT_TYPE_REMEDIATION_APPROVED:
            runtime.execute(uuid.UUID(str(envelope.payload["remediation_id"])))

    return [
        RedisStreamConsumer(
            redis_client,
            stream=stream,
            group=consumer_group_name(CONSUMER_PURPOSE),
            consumer_name=consumer_identity(),
            handler=handle,
            is_duplicate=make_is_duplicate(session_factory, CONSUMER_PURPOSE),
            mark_processed=make_mark_processed(session_factory, CONSUMER_PURPOSE),
            max_deliveries=worker.consumer_max_deliveries,
            claim_min_idle_ms=worker.consumer_claim_min_idle_ms,
            block_ms=200,
        )
        for stream in all_stream_names(
            prefix=worker.outbox_stream_prefix, shard_count=worker.outbox_shard_count
        )
    ]


def run_forever(idle_sleep_seconds: float = 2.0) -> None:
    worker = WorkerSettings()  # type: ignore[call-arg]
    configure_logging(worker.log_level)
    runtime = build_runtime(database_url=worker.incident_core_database_url)
    consumers = build_consumers(
        runtime, worker, redis_lib.from_url(worker.redis_url, decode_responses=True)
    )
    log.info("remediation_worker.started", policy=runtime.remediations.policy.version)
    heartbeat = Heartbeat(
        redis_lib.from_url(worker.redis_url, decode_responses=True), CONSUMER_PURPOSE
    )
    while True:
        heartbeat.beat(policy=runtime.remediations.policy.version)
        handled = sum(consumer.run_once() for consumer in consumers)
        swept = runtime.sweep()
        if handled == 0 and swept == 0:
            time.sleep(idle_sleep_seconds)


if __name__ == "__main__":
    run_forever()
