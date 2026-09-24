"""The outbox relay: reads unpublished outbox_events and publishes them.

This is intentionally the "simple worker abstraction" Phase 1 asks for --
a polling loop, no consumer groups, no sharding by aggregate id (see
packages/events/publisher.py's docstring and
docs/review/critical-review.md, "Event ordering", for what's deliberately
deferred). It is part of incident-core's own infrastructure (it reads
`outbox_events` directly), not alert-ingestion.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import redis as redis_lib
from sqlalchemy.orm import Session

from apps.worker.config import WorkerSettings
from packages.events.envelope import OutboxEventEnvelope
from packages.events.publisher import EventPublisher, RedisStreamEventPublisher
from packages.incident import repository
from packages.incident.db.base import make_engine, make_session_factory
from packages.telemetry.context import bind_context
from packages.telemetry.logging import configure_logging, get_logger

log = get_logger(__name__)


def relay_once(
    session_factory: Callable[[], Session],
    publisher: EventPublisher,
    batch_size: int = 100,
) -> int:
    """Publish and mark one batch of unpublished outbox events.

    Returns the number of events processed. Marking `published_at` happens
    right after each individual publish call and is committed immediately,
    so a crash mid-batch leaves only the not-yet-published tail to retry --
    never a double-mark of an event that wasn't actually published.
    """
    with session_factory() as session:
        events = repository.get_unpublished_outbox_events(session, limit=batch_size)
        for row in events:
            envelope = OutboxEventEnvelope(
                event_id=row.event_id,
                event_type=row.event_type,
                schema_version=row.schema_version,
                aggregate_type=row.aggregate_type,
                aggregate_id=row.aggregate_id,
                correlation_id=row.correlation_id,
                causation_id=row.causation_id,
                payload=row.payload,
                occurred_at=row.occurred_at,
            )
            with bind_context(event_id=str(row.event_id)):
                publisher.publish(envelope)
                repository.mark_outbox_event_published(session, row)
                session.commit()
        return len(events)


def run_forever(settings: WorkerSettings) -> None:
    engine = make_engine(settings.incident_core_database_url)
    session_factory = make_session_factory(engine)
    redis_client = redis_lib.from_url(settings.redis_url)
    publisher = RedisStreamEventPublisher(redis_client, settings.outbox_redis_stream)

    log.info("outbox_relay.started", stream=settings.outbox_redis_stream)
    while True:
        published = relay_once(session_factory, publisher)
        if published == 0:
            time.sleep(settings.poll_interval_seconds)


def main() -> None:
    settings = WorkerSettings()  # type: ignore[call-arg]  # fields are sourced from the environment
    configure_logging(settings.log_level)
    run_forever(settings)


if __name__ == "__main__":
    main()
