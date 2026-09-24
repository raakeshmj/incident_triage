"""The outbox relay: reads unpublished outbox_events and publishes them.

## Delivery semantics (read this before changing anything here)

This relay is **at-least-once, never exactly-once**, and that is a design
decision, not a limitation to be apologized for -- see
docs/architecture/05-event-model.md, "Delivery semantics", and ADR-0014.
Three crash points, and what happens at each:

1. **Crash before the Redis publish call.** The event is still
   `published_at IS NULL` in Postgres. Nothing downstream ever saw it. The
   next relay pass (this process restarting, or another instance) picks it
   up and publishes it for the first time. Fully safe.
2. **Crash after the Redis publish succeeds but before the DB commit that
   sets `published_at`.** Redis already has the message; Postgres still
   thinks it's unpublished. The next relay pass **publishes it again** --
   a genuine duplicate on the stream. This is not prevented, because
   preventing it would require an atomic cross-system transaction across
   Postgres and Redis, which doesn't exist. It is handled downstream:
   every consumer built on `packages.events.consumer.RedisStreamConsumer`
   deduplicates by `event_id` against a database-backed ledger
   (`consumed_events`) before processing, so a duplicate on the stream
   does not become a duplicate side effect.
3. **Crash mid-batch** (event 40 of 100 published, then the process dies).
   Events 1-39 are already committed as published and are not touched
   again. Events 40-100 are retried from scratch on the next pass -- event
   40 specifically hits crash scenario 2 above.

**Retries within one pass** (a single event's publish raising, e.g. a
transient Redis connection blip) are retried up to
`outbox_max_publish_attempts` times with a short linear backoff before the
relay gives up on that event *for this pass* and moves on to the rest of
the batch, recording the failure on the row
(`outbox_events.publish_attempts` / `last_publish_error`) purely for
diagnosis. The event remains unpublished and is retried again on the next
poll -- there is no upper bound on total retries across polls, because
there is no safe terminal "give up" state for an event that must
eventually reach the stream (unlike a consumer's poison-message handling,
which has a dead-letter stream to fall back to; the outbox has no
equivalent "give up" target, since these events are internally generated
and not attacker-controlled input).
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
from packages.incident.db.models import OutboxEventRow
from packages.telemetry.context import bind_context
from packages.telemetry.logging import configure_logging, get_logger
from packages.telemetry.metrics import get_metrics

log = get_logger(__name__)
metrics = get_metrics()

DEFAULT_MAX_PUBLISH_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5


def _envelope_from_row(row: OutboxEventRow) -> OutboxEventEnvelope:
    return OutboxEventEnvelope(
        event_id=row.event_id,
        event_type=row.event_type,
        schema_version=row.schema_version,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        producer=row.producer,
        payload=row.payload,
        occurred_at=row.occurred_at,
    )


def _publish_with_retry(
    publisher: EventPublisher,
    envelope: OutboxEventEnvelope,
    *,
    max_attempts: int,
    backoff_seconds: float,
) -> tuple[bool, str | None]:
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        start = time.monotonic()
        try:
            publisher.publish(envelope)
        except Exception as exc:  # noqa: BLE001 -- any publish failure is retryable here
            last_error = str(exc)
            metrics.increment(
                "outbox.publish_retry", event_type=envelope.event_type, attempt=attempt
            )
            log.warning(
                "outbox_relay.publish_attempt_failed",
                event_id=str(envelope.event_id),
                attempt=attempt,
                max_attempts=max_attempts,
                error=last_error,
            )
            if attempt < max_attempts:
                time.sleep(backoff_seconds * attempt)
        else:
            metrics.observe(
                "outbox.publish_latency_seconds",
                time.monotonic() - start,
                event_type=envelope.event_type,
            )
            return True, None
    return False, last_error


def relay_once(
    session_factory: Callable[[], Session],
    publisher: EventPublisher,
    batch_size: int = 100,
    *,
    max_publish_attempts: int = DEFAULT_MAX_PUBLISH_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> int:
    """Publish and mark one batch of unpublished outbox events.

    Returns the number of events **successfully published** in this pass
    (not merely examined) -- `run_forever` uses this to decide whether to
    poll again immediately or back off, so a batch that's all transient
    failures correctly triggers the idle sleep rather than busy-looping
    against a down Redis.
    """
    published_count = 0
    with session_factory() as session:
        events = repository.get_unpublished_outbox_events(session, limit=batch_size)
        for row in events:
            envelope = _envelope_from_row(row)
            with bind_context(event_id=str(row.event_id)):
                success, error = _publish_with_retry(
                    publisher,
                    envelope,
                    max_attempts=max_publish_attempts,
                    backoff_seconds=retry_backoff_seconds,
                )
                if success:
                    repository.mark_outbox_event_published(session, row)
                    session.commit()
                    published_count += 1
                    metrics.increment("outbox.published", event_type=row.event_type)
                else:
                    repository.mark_outbox_event_publish_failed(
                        session, row, error or "unknown error"
                    )
                    session.commit()
                    metrics.increment("outbox.publish_failed", event_type=row.event_type)
                    log.error(
                        "outbox_relay.publish_failed_this_pass",
                        event_id=str(row.event_id),
                        attempts=max_publish_attempts,
                        error=error,
                    )
        return published_count


def run_forever(settings: WorkerSettings) -> None:
    engine = make_engine(settings.incident_core_database_url)
    session_factory = make_session_factory(engine)
    redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
    publisher = RedisStreamEventPublisher(
        redis_client,
        stream_prefix=settings.outbox_stream_prefix,
        shard_count=settings.outbox_shard_count,
    )

    log.info(
        "outbox_relay.started",
        stream_prefix=settings.outbox_stream_prefix,
        shard_count=settings.outbox_shard_count,
    )
    while True:
        published = relay_once(
            session_factory,
            publisher,
            batch_size=settings.outbox_batch_size,
            max_publish_attempts=settings.outbox_max_publish_attempts,
            retry_backoff_seconds=settings.outbox_retry_backoff_seconds,
        )
        if published == 0:
            time.sleep(settings.poll_interval_seconds)


def main() -> None:
    settings = WorkerSettings()  # type: ignore[call-arg]  # fields are sourced from the environment
    configure_logging(settings.log_level)
    run_forever(settings)


if __name__ == "__main__":
    main()
