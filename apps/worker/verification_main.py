"""The verification worker (Phase 8). Deterministic code only; no model.

1. **Start** -- `VerificationRequested` (emitted in the same transaction
   that marks a remediation EXECUTED) moves the verification PENDING ->
   RUNNING: grace period, deadline, first poll time. Duplicate deliveries
   find it already running.
2. **Tick** -- every loop, each due verification gets at most one
   observation through evidence-service, recorded and decided by
   incident-core (fenced by lease + claim attempt). A verdict moves the
   incident: RESOLVED, VERIFICATION_FAILED (-> re-investigation by the
   investigation worker's scheduler) or ESCALATED.
3. **Recover** -- `due()` includes verifications whose worker died (lease
   lapsed) and PENDING ones whose event was lost; they continue from
   their persisted streak.
"""

from __future__ import annotations

import os
import socket
import time
import uuid

import redis as redis_lib
from pydantic_settings import BaseSettings, SettingsConfigDict

from apps.worker.config import WorkerSettings
from apps.worker.consumer_main import make_is_duplicate, make_mark_processed
from packages.domain.events import EVENT_TYPE_VERIFICATION_REQUESTED
from packages.events.consumer import RedisStreamConsumer
from packages.events.envelope import OutboxEventEnvelope
from packages.events.streams import all_stream_names, consumer_group_name, consumer_identity
from packages.evidence.service import EvidenceService
from packages.incident.db.base import make_engine, make_session_factory
from packages.incident.verifications import VerificationCoreService
from packages.telemetry.heartbeat import Heartbeat
from packages.telemetry.logging import configure_logging, get_logger
from packages.verification.engine import VerificationEngine
from packages.verification.observer import EvidenceObserver

log = get_logger(__name__)

CONSUMER_PURPOSE = "verification-worker"


class VerificationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    verification_lease_seconds: float = 60
    # Investigation attempts per incident before a failed verification
    # escalates instead of re-investigating (04-incident-state-machine.md).
    max_investigation_attempts: int = 2


def build_engine(
    *,
    database_url: str | None = None,
    evidence_service: EvidenceService | None = None,
    settings: VerificationSettings | None = None,
    owner: str | None = None,
) -> tuple[VerificationCoreService, VerificationEngine]:
    settings = settings or VerificationSettings()
    core = VerificationCoreService(
        make_session_factory(make_engine(database_url)),
        max_investigation_attempts=settings.max_investigation_attempts,
    )
    if evidence_service is None:
        from apps.evidence.dependencies import get_evidence_service

        evidence_service = get_evidence_service()
    engine = VerificationEngine(
        core,
        EvidenceObserver(evidence_service),
        owner=owner or f"{socket.gethostname()}:{os.getpid()}",
        lease_seconds=settings.verification_lease_seconds,
    )
    return core, engine


def build_consumers(
    engine: VerificationEngine, worker: WorkerSettings, redis_client: redis_lib.Redis
) -> list[RedisStreamConsumer]:
    session_factory = make_session_factory(make_engine(worker.incident_core_database_url))

    def handle(envelope: OutboxEventEnvelope) -> None:
        if envelope.event_type == EVENT_TYPE_VERIFICATION_REQUESTED:
            engine.start(uuid.UUID(str(envelope.payload["verification_id"])))

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


def tick_due(core: VerificationCoreService, engine: VerificationEngine) -> int:
    ticked = 0
    for verification_id in core.due():
        if engine.tick(verification_id) is not None:
            ticked += 1
    return ticked


def run_forever(idle_sleep_seconds: float = 1.0) -> None:
    worker = WorkerSettings()  # type: ignore[call-arg]
    configure_logging(worker.log_level)
    core, engine = build_engine(database_url=worker.incident_core_database_url)
    redis_client = redis_lib.from_url(worker.redis_url, decode_responses=True)
    consumers = build_consumers(engine, worker, redis_client)
    heartbeat = Heartbeat(redis_client, CONSUMER_PURPOSE)
    log.info("verification_worker.started")
    while True:
        heartbeat.beat()
        handled = sum(consumer.run_once() for consumer in consumers)
        ticked = tick_due(core, engine)
        if handled == 0 and ticked == 0:
            time.sleep(idle_sleep_seconds)


if __name__ == "__main__":
    run_forever()
