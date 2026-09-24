"""Publishers that hand an already-persisted outbox event to a transport.

Deliberately minimal for Phase 1 (see
docs/architecture/12-local-development.md and ADR-0003): no consumer
groups, no per-incident sharding, no replay/dedup logic here -- those are
documented, explicitly-deferred design decisions for when Redis Streams
fan-out actually needs to support multiple consumers
(docs/review/critical-review.md, "Event ordering"). This module only
proves the abstraction and the Redis plumbing work end to end.
"""

from __future__ import annotations

import json
from typing import Protocol

from packages.events.envelope import OutboxEventEnvelope
from packages.telemetry.logging import get_logger

log = get_logger(__name__)


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
    """Minimal, working publish to a single Redis Stream via XADD.

    No consumer groups, no sharding by aggregate id, no dedup -- this is
    intentionally the smallest possible thing that proves the Redis
    infrastructure works, not the final fan-out design (ADR-0003 and the
    critical review's open item on stream sharding are unchanged by this).
    """

    def __init__(self, redis_client, stream_key: str) -> None:
        self._redis = redis_client
        self._stream_key = stream_key

    def publish(self, event: OutboxEventEnvelope) -> None:
        self._redis.xadd(
            self._stream_key,
            {"event": json.dumps(event.model_dump(mode="json"))},
        )
        log.info(
            "event.published",
            event_id=str(event.event_id),
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=str(event.aggregate_id),
            stream=self._stream_key,
        )
