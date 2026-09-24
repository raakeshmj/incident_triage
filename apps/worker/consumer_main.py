"""A concrete, durable event consumer: records observability metrics for
correlation decisions and incident creation rate (Phase 2 requirement 8),
built on the reusable `RedisStreamConsumer` abstraction (Phase 2
requirement 3).

This consumer **never mutates Incident/Alert state** -- it only records
metrics and its own `consumed_events` ledger row. PostgreSQL/domain state
remains authoritative regardless of what any consumer does; Redis and this
consumer are pure observers of facts `incident-core` already committed
(Phase 2 requirement 6, "Do not let Redis become the source of truth").

One `RedisStreamConsumer` per shard stream, since each Redis Stream needs
its own consumer-group registration. All shards share one consumer
*group* name (the logical purpose, e.g. "metrics-consumer") but each
process reads every shard -- fine at this scale (Phase 1/2 volume), and
the natural place to introduce per-shard worker processes later without
changing this module's shape, just how many of it you run.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import redis as redis_lib
from sqlalchemy.orm import Session

from apps.worker.config import WorkerSettings
from packages.domain.events import (
    EVENT_TYPE_ALERT_CORRELATED,
    EVENT_TYPE_ALERT_RECEIVED,
    EVENT_TYPE_INCIDENT_CREATED,
)
from packages.events.consumer import RedisStreamConsumer
from packages.events.envelope import OutboxEventEnvelope
from packages.events.streams import all_stream_names, consumer_group_name, consumer_identity
from packages.incident import repository
from packages.incident.db.base import make_engine, make_session_factory
from packages.telemetry.logging import configure_logging, get_logger
from packages.telemetry.metrics import get_metrics

log = get_logger(__name__)
metrics = get_metrics()


def make_handler() -> Callable[[OutboxEventEnvelope], None]:
    def handle(envelope: OutboxEventEnvelope) -> None:
        if envelope.event_type == EVENT_TYPE_INCIDENT_CREATED:
            metrics.increment(
                "incidents.created_total",
                service=envelope.payload.get("service", "unknown"),
                environment=envelope.payload.get("environment", "unknown"),
            )
            metrics.observe(
                "correlation.best_candidate_score",
                float(envelope.payload.get("best_candidate_score", 0.0)),
            )
        elif envelope.event_type == EVENT_TYPE_ALERT_CORRELATED:
            metrics.increment("alerts.correlated_total")
            metrics.observe("correlation.score", float(envelope.payload.get("score", 0.0)))
        elif envelope.event_type == EVENT_TYPE_ALERT_RECEIVED:
            metrics.increment("alerts.received_total")

        log.info(
            "event_consumer.handled_event",
            event_type=envelope.event_type,
            event_id=str(envelope.event_id),
            aggregate_id=str(envelope.aggregate_id),
        )

    return handle


def make_is_duplicate(
    session_factory: Callable[[], Session], ledger_consumer_name: str
) -> Callable[[uuid.UUID], bool]:
    def is_duplicate(event_id: uuid.UUID) -> bool:
        with session_factory() as session:
            return repository.has_consumed_event(
                session, consumer_name=ledger_consumer_name, event_id=event_id
            )

    return is_duplicate


def make_mark_processed(
    session_factory: Callable[[], Session], ledger_consumer_name: str
) -> Callable[[uuid.UUID], None]:
    def mark_processed(event_id: uuid.UUID) -> None:
        with session_factory() as session:
            repository.mark_event_consumed(
                session, consumer_name=ledger_consumer_name, event_id=event_id
            )
            session.commit()

    return mark_processed


def build_consumers(
    settings: WorkerSettings,
    redis_client=None,
    session_factory=None,
    *,
    block_ms: int = 200,
) -> list[RedisStreamConsumer]:
    """`block_ms` defaults low (200ms) because this process round-robins
    every shard stream in a single thread (`run_forever` below) -- each
    stream's `XREADGROUP` blocks for up to `block_ms` when it has nothing
    new, so a large `block_ms` here would mean an idle shard stalls the
    whole cycle instead of quickly yielding to the next one. A dedicated
    per-shard consumer process could safely use a much larger block time;
    this one can't.
    """
    if session_factory is None:
        engine = make_engine(settings.incident_core_database_url)
        session_factory = make_session_factory(engine)
    if redis_client is None:
        redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)

    # The ledger key (`consumed_events.consumer_name`) is the *logical*
    # consumer identity and must stay stable across restarts, or a
    # redelivered message processed by a new process instance (different
    # pid) would look "new" and be processed again. `consumer_identity()`
    # (host:pid) is deliberately NOT used here -- it's only for Redis's own
    # per-consumer pending-entry tracking below.
    ledger_consumer_name = settings.consumer_purpose
    group = consumer_group_name(settings.consumer_purpose)
    redis_consumer_name = consumer_identity()

    handler = make_handler()
    is_duplicate = make_is_duplicate(session_factory, ledger_consumer_name)
    mark_processed = make_mark_processed(session_factory, ledger_consumer_name)

    streams = all_stream_names(
        prefix=settings.outbox_stream_prefix, shard_count=settings.outbox_shard_count
    )
    return [
        RedisStreamConsumer(
            redis_client,
            stream=stream,
            group=group,
            consumer_name=redis_consumer_name,
            handler=handler,
            is_duplicate=is_duplicate,
            mark_processed=mark_processed,
            max_deliveries=settings.consumer_max_deliveries,
            claim_min_idle_ms=settings.consumer_claim_min_idle_ms,
            block_ms=block_ms,
        )
        for stream in streams
    ]


def run_forever(settings: WorkerSettings, idle_sleep_seconds: float = 1.0) -> None:
    consumers = build_consumers(settings)
    log.info(
        "event_consumer.started",
        purpose=settings.consumer_purpose,
        shard_count=len(consumers),
    )
    while True:
        handled = sum(consumer.run_once() for consumer in consumers)
        if handled == 0:
            time.sleep(idle_sleep_seconds)


def main() -> None:
    settings = WorkerSettings()  # type: ignore[call-arg]  # fields are sourced from the environment
    configure_logging(settings.log_level)
    run_forever(settings)


if __name__ == "__main__":
    main()
