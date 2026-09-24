"""Publishers that hand an already-persisted outbox event to a transport.

Phase 2: `RedisStreamEventPublisher` now publishes into the sharded stream
topology defined in `packages/events/streams.py` (per-incident ordering,
see ADR-0014), instead of a single flat stream. Retrying a failed publish
is the caller's job (`apps/worker/main.py`'s relay loop) -- this class
raises on failure rather than swallowing it, so the relay can apply its
own backoff/retry policy and update its own metrics.
"""

from __future__ import annotations

import json
from typing import Protocol

from packages.events.envelope import OutboxEventEnvelope
from packages.events.streams import (
    DEFAULT_SHARD_COUNT,
    DEFAULT_STREAM_PREFIX,
    stream_name_for_event,
)
from packages.telemetry.logging import get_logger
from packages.telemetry.metrics import get_metrics

log = get_logger(__name__)
metrics = get_metrics()


class EventPublisher(Protocol):
    """What the outbox relay (apps/worker) depends on.

    Swapping the implementation must never change the relay's code -- see
    apps/worker/main.py.
    """

    def publish(self, event: OutboxEventEnvelope) -> None: ...


class LoggingEventPublisher:
    """Default for local dev / tests: publishing is just a structured log line.

    This is enough to prove the outbox -> relay -> "downstream" path
    without standing up a real consumer.
    """

    def publish(self, event: OutboxEventEnvelope) -> None:
        log.info(
            "event.published",
            event_id=str(event.event_id),
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=str(event.aggregate_id),
        )


class RedisStreamEventPublisher:
    """Publish to the sharded Redis Stream topology (`packages.events.streams`).

    Sharded by `correlation_id` (falling back to `aggregate_id`) so all
    events for one incident land on one stream and are read in emission
    order by whichever consumer owns that shard -- see ADR-0014. Does not
    catch or retry Redis errors; raises, so the relay's own retry/backoff
    policy is the single place that decides how to handle a transient
    Redis failure (docs/architecture/05-event-model.md's Phase 2
    addendum, "Delivery semantics").
    """

    def __init__(
        self,
        redis_client,
        *,
        stream_prefix: str = DEFAULT_STREAM_PREFIX,
        shard_count: int = DEFAULT_SHARD_COUNT,
    ) -> None:
        self._redis = redis_client
        self._stream_prefix = stream_prefix
        self._shard_count = shard_count

    def publish(self, event: OutboxEventEnvelope) -> None:
        stream_key = stream_name_for_event(
            event.correlation_id,
            event.aggregate_id,
            prefix=self._stream_prefix,
            shard_count=self._shard_count,
        )
        self._redis.xadd(
            stream_key,
            {"event": json.dumps(event.model_dump(mode="json"))},
        )
        metrics.increment("outbox.event_published", event_type=event.event_type, stream=stream_key)
        log.info(
            "event.published",
            event_id=str(event.event_id),
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=str(event.aggregate_id),
            producer=event.producer,
            stream=stream_key,
        )
