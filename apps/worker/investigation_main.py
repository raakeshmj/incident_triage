"""The investigation worker (Phase 5).

One process, three duties, each safe under at-least-once delivery:

1. **Schedule** -- `due_for_investigation`: TRIAGING incidents past the
   debounce window with a firing alert are moved to INVESTIGATING by
   incident-core, which creates the Investigation and emits
   `InvestigationStarted` through the outbox (04-incident-state-machine.md).
2. **Consume** -- `InvestigationStarted` events arrive over the Phase 2
   Redis Streams consumer group (`cg:investigation-worker`), deduplicated by
   the `consumed_events` ledger; each runs the engine. A duplicate delivery
   finds the investigation already claimed or finished and does nothing.
3. **Resume** -- `resumable()` finds open investigations whose lease has
   lapsed (a crashed worker) and runs them from their persisted trace. This,
   not event redelivery, is the liveness guarantee.

V1 composition: the worker builds incident-core, evidence-service and the
tool layer in-process, the same code-level-boundary pattern as apps/api and
apps/evidence (ADR-0018). The model inside the loop gets none of it: it sees
tool definitions and results, nothing else.
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from apps.evidence.dependencies import get_evidence_service
from apps.worker.config import WorkerSettings
from apps.worker.consumer_main import make_is_duplicate, make_mark_processed
from packages.agents.config import InvestigationSettings, resolve_model_spec
from packages.agents.engine import InvestigationEngine, RetryPolicy
from packages.agents.factory import ModelFactory, build_investigation_model
from packages.domain.errors import ConcurrentModificationError, InvalidIncidentTransitionError
from packages.domain.events import EVENT_TYPE_INVESTIGATION_STARTED
from packages.events.consumer import RedisStreamConsumer
from packages.events.envelope import OutboxEventEnvelope
from packages.events.streams import all_stream_names, consumer_group_name, consumer_identity
from packages.evidence.scope import ServiceCatalog
from packages.incident.db.base import make_engine, make_session_factory
from packages.incident.investigations import InvestigationCoreService
from packages.incident.service import IncidentCoreService
from packages.telemetry.heartbeat import Heartbeat
from packages.telemetry.logging import configure_logging, get_logger

log = get_logger(__name__)

CONSUMER_PURPOSE = "investigation-worker"
HEURISTIC_PROVIDER = "heuristic"


@dataclass
class InvestigationRuntime:
    settings: InvestigationSettings
    core: IncidentCoreService
    investigations: InvestigationCoreService
    engine: InvestigationEngine

    def start(self, incident_id: uuid.UUID) -> uuid.UUID:
        """Start (or find the open) investigation for an incident, stamped
        with the currently configured model."""
        spec = resolve_model_spec(self.settings)
        started = self.investigations.request_investigation(
            incident_id,
            model_provider=spec.provider,
            model_name=spec.model,
            model_settings=spec.settings(),
            budget=self.settings.budget(),
        )
        return started.investigation_id

    def schedule_due(self) -> list[uuid.UUID]:
        started = []
        for incident_id in self.investigations.due_for_investigation(
            debounce_seconds=self.settings.investigation_debounce_seconds
        ):
            try:
                started.append(self.start(incident_id))
            except (InvalidIncidentTransitionError, ConcurrentModificationError) as exc:
                log.info(
                    "investigation.schedule_skipped", incident_id=str(incident_id), why=str(exc)
                )
        return started

    def resume_stale(self) -> int:
        ran = 0
        for investigation_id in self.investigations.resumable():
            if self.engine.run(investigation_id) is not None:
                ran += 1
        return ran


def build_runtime(
    *,
    database_url: str | None = None,
    settings: InvestigationSettings | None = None,
    model_factory: ModelFactory = build_investigation_model,
    evidence_service=None,
    catalog_path: str | None = None,
    owner: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> InvestigationRuntime:
    settings = settings or InvestigationSettings()
    if (
        settings.investigation_model_provider == HEURISTIC_PROVIDER
        and model_factory is build_investigation_model
    ):
        # Offline mode: the deterministic rule-based investigator from the
        # evaluation harness -- not a model, no credential, no API call.
        # Explicit opt-in (INVESTIGATION_PROVIDER=heuristic); see
        # docs/operations.md.
        from packages.evaluation.heuristic import HeuristicInvestigator

        model_factory = lambda spec: HeuristicInvestigator()  # noqa: E731
    session_factory = make_session_factory(make_engine(database_url))
    core = IncidentCoreService(session_factory)
    investigations = InvestigationCoreService(session_factory, criteria=settings.criteria())
    catalog = ServiceCatalog.load(
        catalog_path
        or os.environ.get(
            "EVIDENCE_SERVICE_CATALOG_PATH", "infrastructure/evidence/service-catalog.json"
        )
    )
    engine = InvestigationEngine(
        gateway=investigations,
        incidents=core,
        evidence=evidence_service or get_evidence_service(),
        catalog=catalog,
        model_factory=model_factory,
        owner=owner or f"{socket.gethostname()}:{os.getpid()}",
        criteria=settings.criteria(),
        lease_seconds=settings.investigation_lease_seconds,
        retry=RetryPolicy(
            attempts=settings.investigation_model_attempts,
            backoff_seconds=settings.investigation_retry_backoff_seconds,
        ),
        sleep=sleep,
    )
    return InvestigationRuntime(settings, core, investigations, engine)


def build_consumers(
    runtime: InvestigationRuntime, worker: WorkerSettings, redis_client
) -> list[RedisStreamConsumer]:
    session_factory = make_session_factory(make_engine(worker.incident_core_database_url))

    def handle(envelope: OutboxEventEnvelope) -> None:
        if envelope.event_type != EVENT_TYPE_INVESTIGATION_STARTED:
            return
        investigation_id = uuid.UUID(str(envelope.payload["investigation_id"]))
        outcome = runtime.engine.run(investigation_id)
        log.info(
            "investigation.event_handled",
            investigation_id=str(investigation_id),
            outcome=outcome.value if outcome else "not_claimed",
        )

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
    import redis as redis_lib

    worker = WorkerSettings()  # type: ignore[call-arg]
    configure_logging(worker.log_level)
    runtime = build_runtime(database_url=worker.incident_core_database_url)
    consumers = build_consumers(
        runtime, worker, redis_lib.from_url(worker.redis_url, decode_responses=True)
    )
    log.info(
        "investigation_worker.started",
        model=runtime.settings.investigation_model,
        debounce_seconds=runtime.settings.investigation_debounce_seconds,
    )
    heartbeat = Heartbeat(
        redis_lib.from_url(worker.redis_url, decode_responses=True), CONSUMER_PURPOSE
    )
    while True:
        heartbeat.beat(model=runtime.settings.investigation_model)
        runtime.schedule_due()
        handled = sum(consumer.run_once() for consumer in consumers)
        resumed = runtime.resume_stale()
        if handled == 0 and resumed == 0:
            time.sleep(idle_sleep_seconds)


if __name__ == "__main__":
    run_forever()
