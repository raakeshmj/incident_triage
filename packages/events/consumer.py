"""Reusable Redis Streams consumer-group abstraction (Phase 2).

One `RedisStreamConsumer` instance reads one stream, within one consumer
group, under one consumer identity (`packages.events.streams`). It is
deliberately generic over *what* processing means -- the `handler`
callback and the `is_duplicate` / `mark_processed` collaborators are
injected, so this class has no dependency on incident-core's domain at
all. See docs/adr/0014-redis-streams-transport.md.

Delivery semantics this class implements (see also
docs/architecture/05-event-model.md, "Delivery semantics"):

- **At-least-once, never exactly-once.** A message is only ACKed after
  `mark_processed` has durably recorded it -- if the process crashes
  between a successful `handler` call and the ACK, the message is
  redelivered (via `XAUTOCLAIM` once it's been idle past
  `claim_min_idle_ms`) and reprocessed. `is_duplicate` is what makes that
  redelivery safe: it is checked *before* the handler runs, so a handler
  that already succeeded once is never invoked twice for the same
  `event_id`, even though Redis itself does deliver the message twice.
- **Poison detection via Redis's own delivery count.** Redis tracks how
  many times a pending entry has been delivered (`XPENDING`'s
  `times_delivered`). Once that exceeds `max_deliveries`, the message is
  moved to the dead-letter stream instead of being retried again --
  Redis's own counter is the source of truth for "how many times has this
  actually been attempted," not a counter this class keeps itself, which
  would be lost on a crash.
- **A handler can also force immediate dead-lettering** by raising
  `PermanentProcessingError` (e.g. the payload itself is malformed --
  retrying won't help).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from redis.exceptions import ResponseError

from packages.events.envelope import OutboxEventEnvelope
from packages.events.streams import dead_letter_stream_name
from packages.telemetry.context import bind_context
from packages.telemetry.logging import get_logger
from packages.telemetry.metrics import get_metrics

log = get_logger(__name__)
metrics = get_metrics()

DEFAULT_MAX_DELIVERIES = 5
DEFAULT_CLAIM_MIN_IDLE_MS = 30_000
DEFAULT_BLOCK_MS = 5_000
DEFAULT_BATCH_SIZE = 10

EventHandler = Callable[[OutboxEventEnvelope], None]
IsDuplicate = Callable[[uuid.UUID], bool]
MarkProcessed = Callable[[uuid.UUID], None]


class PermanentProcessingError(Exception):
    """Raised by a handler to force immediate dead-lettering, skipping the
    normal retry budget -- e.g. the event payload itself doesn't match the
    schema the handler expects.
    """


def classify_delivery(delivery_count: int, max_deliveries: int) -> str:
    """Pure retry-classification decision, factored out of
    `RedisStreamConsumer._handle_message` so it's unit-testable without a
    Redis connection. Redis's own delivery counter (via `XPENDING`) is the
    input -- see the module docstring's "Poison detection" note on why
    that counter, not one this class keeps itself, is the source of truth.

    Returns "poison" once `delivery_count` exceeds `max_deliveries`,
    "process" otherwise. The boundary is inclusive of `max_deliveries`
    itself: the Nth attempt still processes normally, only the (N+1)th
    is classified as poison.
    """
    return "poison" if delivery_count > max_deliveries else "process"


@dataclass
class ConsumerStats:
    """Process-local counters, primarily for tests. `packages.telemetry.metrics`
    is the source of truth for anything meant to be observed externally.
    """

    processed: int = 0
    duplicates_skipped: int = 0
    failures: int = 0
    dead_lettered: int = 0


class RedisStreamConsumer:
    """Requires a Redis client constructed with `decode_responses=True` --
    every stream field, id, and group name this class handles is treated
    as `str`, never `bytes`.
    """

    def __init__(
        self,
        redis_client,
        *,
        stream: str,
        group: str,
        consumer_name: str,
        handler: EventHandler,
        is_duplicate: IsDuplicate,
        mark_processed: MarkProcessed,
        max_deliveries: int = DEFAULT_MAX_DELIVERIES,
        claim_min_idle_ms: int = DEFAULT_CLAIM_MIN_IDLE_MS,
        dead_letter_stream: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        block_ms: int = DEFAULT_BLOCK_MS,
    ) -> None:
        self._redis = redis_client
        self._stream = stream
        self._group = group
        self._consumer_name = consumer_name
        self._handler = handler
        self._is_duplicate = is_duplicate
        self._mark_processed = mark_processed
        self._max_deliveries = max_deliveries
        self._claim_min_idle_ms = claim_min_idle_ms
        self._dead_letter_stream = dead_letter_stream or dead_letter_stream_name()
        self._batch_size = batch_size
        self._block_ms = block_ms
        self.stats = ConsumerStats()
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def run_once(self) -> int:
        """Reclaim stale pending entries, then read and process one batch
        of new entries. Returns the number of messages handled (processed,
        deduped, or dead-lettered -- not counting ones left pending after a
        retryable failure), so callers/tests can poll without sleeping.
        """
        handled = self._reclaim_stale()
        handled += self._read_new()
        return handled

    def run_forever(self, idle_sleep_seconds: float = 1.0) -> None:
        log.info(
            "consumer.started",
            stream=self._stream,
            group=self._group,
            consumer=self._consumer_name,
        )
        while True:
            if self.run_once() == 0:
                time.sleep(idle_sleep_seconds)

    # --- internals ---------------------------------------------------------

    def _reclaim_stale(self) -> int:
        handled = 0
        cursor = "0-0"
        while True:
            next_cursor, claimed, _deleted = self._redis.xautoclaim(
                self._stream,
                self._group,
                self._consumer_name,
                min_idle_time=self._claim_min_idle_ms,
                start_id=cursor,
                count=self._batch_size,
            )
            for message_id, fields in claimed:
                handled += 1
                self._handle_message(message_id, fields, reclaimed=True)
            cursor = next_cursor
            if not claimed or cursor == "0-0":
                break
        return handled

    def _read_new(self) -> int:
        response = self._redis.xreadgroup(
            self._group,
            self._consumer_name,
            {self._stream: ">"},
            count=self._batch_size,
            block=self._block_ms,
        )
        if not response:
            return 0
        handled = 0
        for _stream_name, messages in response:
            for message_id, fields in messages:
                handled += 1
                self._handle_message(message_id, fields, reclaimed=False)
        return handled

    def _delivery_count(self, message_id: str) -> int:
        pending = self._redis.xpending_range(self._stream, self._group, message_id, message_id, 1)
        if not pending:
            return 1
        return int(pending[0]["times_delivered"])

    def _handle_message(self, message_id: str, fields: dict, *, reclaimed: bool) -> None:
        envelope = OutboxEventEnvelope.model_validate(json.loads(fields["event"]))
        delivery_count = self._delivery_count(message_id)

        with bind_context(event_id=str(envelope.event_id)):
            if classify_delivery(delivery_count, self._max_deliveries) == "poison":
                self._dead_letter(message_id, envelope, reason="max_deliveries_exceeded")
                return

            if self._is_duplicate(envelope.event_id):
                log.info("consumer.duplicate_skipped", event_type=envelope.event_type)
                self.stats.duplicates_skipped += 1
                metrics.increment("consumer.duplicate_skipped", event_type=envelope.event_type)
                self._redis.xack(self._stream, self._group, message_id)
                return

            start = time.monotonic()
            try:
                self._handler(envelope)
            except PermanentProcessingError as exc:
                self._dead_letter(message_id, envelope, reason=f"permanent_error: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 -- any handler failure is retryable by default
                self.stats.failures += 1
                metrics.increment("consumer.processing_failure", event_type=envelope.event_type)
                log.warning(
                    "consumer.processing_failed",
                    event_type=envelope.event_type,
                    delivery_count=delivery_count,
                    error=str(exc),
                )
                # No ack: the message stays pending and is retried, either
                # immediately (if still owned by this consumer's next read)
                # or via _reclaim_stale once it's been idle long enough.
                return

            duration = time.monotonic() - start
            metrics.observe(
                "consumer.processing_duration_seconds", duration, event_type=envelope.event_type
            )
            self._mark_processed(envelope.event_id)
            self._redis.xack(self._stream, self._group, message_id)
            self.stats.processed += 1
            metrics.increment("consumer.processed", event_type=envelope.event_type)
            log.info("consumer.processed", event_type=envelope.event_type, reclaimed=reclaimed)

    def _dead_letter(self, message_id: str, envelope: OutboxEventEnvelope, *, reason: str) -> None:
        self._redis.xadd(
            self._dead_letter_stream,
            {
                "event": json.dumps(envelope.model_dump(mode="json")),
                "original_stream": self._stream,
                "original_group": self._group,
                "reason": reason,
            },
        )
        self._redis.xack(self._stream, self._group, message_id)
        self.stats.dead_lettered += 1
        metrics.increment("consumer.dead_lettered", event_type=envelope.event_type, reason=reason)
        log.error(
            "consumer.dead_lettered",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
            reason=reason,
        )
