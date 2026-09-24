from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from packages.events.consumer import PermanentProcessingError, RedisStreamConsumer
from packages.events.envelope import OutboxEventEnvelope
from tests.fakes import FakeRedisStreams


def _make_envelope(**overrides) -> OutboxEventEnvelope:
    defaults = dict(
        event_id=uuid.uuid4(),
        event_type="AlertReceived",
        aggregate_type="Alert",
        aggregate_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        producer="test",
        payload={},
    )
    defaults.update(overrides)
    return OutboxEventEnvelope(**defaults)


def _publish(redis: FakeRedisStreams, stream: str, envelope: OutboxEventEnvelope) -> None:
    redis.xadd(stream, {"event": json.dumps(envelope.model_dump(mode="json"))})


def test_consumer_skips_duplicate_without_calling_handler():
    redis = FakeRedisStreams()
    stream, group = "test-stream", "cg:test"
    envelope = _make_envelope()
    _publish(redis, stream, envelope)

    handled: list[uuid.UUID] = []
    consumer = RedisStreamConsumer(
        redis,
        stream=stream,
        group=group,
        consumer_name="c1",
        handler=lambda e: handled.append(e.event_id),
        is_duplicate=lambda event_id: event_id == envelope.event_id,  # already consumed
        mark_processed=lambda event_id: None,
    )

    count = consumer.run_once()

    assert count == 1
    assert handled == []  # handler never invoked for a known duplicate
    assert consumer.stats.duplicates_skipped == 1
    assert redis._pending[(stream, group)] == {}  # still acked, so it won't be redelivered forever


def test_consumer_marks_processed_and_acks_on_success():
    redis = FakeRedisStreams()
    stream, group = "test-stream", "cg:test"
    envelope = _make_envelope()
    _publish(redis, stream, envelope)

    marked: list[uuid.UUID] = []
    consumer = RedisStreamConsumer(
        redis,
        stream=stream,
        group=group,
        consumer_name="c1",
        handler=lambda e: None,
        is_duplicate=lambda event_id: False,
        mark_processed=lambda event_id: marked.append(event_id),
    )

    count = consumer.run_once()

    assert count == 1
    assert marked == [envelope.event_id]
    assert consumer.stats.processed == 1
    assert redis._pending[(stream, group)] == {}


def test_consumer_leaves_message_pending_on_retryable_failure():
    redis = FakeRedisStreams()
    stream, group = "test-stream", "cg:test"
    envelope = _make_envelope()
    _publish(redis, stream, envelope)

    def failing_handler(_: OutboxEventEnvelope) -> None:
        raise RuntimeError("transient boom")

    consumer = RedisStreamConsumer(
        redis,
        stream=stream,
        group=group,
        consumer_name="c1",
        handler=failing_handler,
        is_duplicate=lambda event_id: False,
        mark_processed=lambda event_id: None,
    )

    count = consumer.run_once()

    assert count == 1  # "handled" = attempted, not necessarily acked
    assert consumer.stats.failures == 1
    assert len(redis._pending[(stream, group)]) == 1  # NOT acked -- eligible for redelivery


def test_consumer_dead_letters_once_max_deliveries_exceeded():
    redis = FakeRedisStreams()
    stream, group = "test-stream", "cg:test"
    envelope = _make_envelope()
    _publish(redis, stream, envelope)

    consumer = RedisStreamConsumer(
        redis,
        stream=stream,
        group=group,
        consumer_name="c1",
        handler=lambda e: None,
        is_duplicate=lambda event_id: False,
        mark_processed=lambda event_id: None,
        max_deliveries=0,  # even the first delivery (count=1) exceeds this
    )

    count = consumer.run_once()

    assert count == 1
    assert consumer.stats.dead_lettered == 1
    assert len(redis._streams["stream:events:dlq"]) == 1
    dlq_message = redis._streams["stream:events:dlq"][0][1]
    dlq_payload = json.loads(dlq_message["event"])
    assert dlq_payload["event_id"] == str(envelope.event_id)  # original event_id preserved
    assert dlq_message["reason"] == "max_deliveries_exceeded"
    assert redis._pending[(stream, group)] == {}  # acked off the original stream


def test_consumer_dead_letters_on_permanent_processing_error():
    redis = FakeRedisStreams()
    stream, group = "test-stream", "cg:test"
    envelope = _make_envelope()
    _publish(redis, stream, envelope)

    def handler(_: OutboxEventEnvelope) -> None:
        raise PermanentProcessingError("payload will never parse")

    consumer = RedisStreamConsumer(
        redis,
        stream=stream,
        group=group,
        consumer_name="c1",
        handler=handler,
        is_duplicate=lambda event_id: False,
        mark_processed=lambda event_id: None,
    )

    count = consumer.run_once()

    assert count == 1
    assert consumer.stats.dead_lettered == 1
    assert len(redis._streams["stream:events:dlq"]) == 1
