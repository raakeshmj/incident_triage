"""Real Redis: poison messages reach the dead-letter stream, with the
original event_id preserved -- Phase 2 requirement 3.
"""

from __future__ import annotations

import datetime
import json
import os
import uuid

import pytest
import redis as redis_lib

from packages.events.consumer import RedisStreamConsumer
from packages.events.envelope import OutboxEventEnvelope
from packages.events.streams import dead_letter_stream_name


@pytest.fixture
def redis_client():
    client = redis_lib.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        client.ping()
    except redis_lib.exceptions.ConnectionError as exc:
        pytest.skip(f"Redis not reachable ({exc}); run `make infra-up` first")
    yield client


def _make_envelope(**overrides) -> OutboxEventEnvelope:
    defaults = dict(
        event_id=uuid.uuid4(),
        event_type="AlertReceived",
        aggregate_type="Alert",
        aggregate_id=uuid.uuid4(),
        occurred_at=datetime.datetime.now(datetime.UTC),
        producer="test",
        payload={},
    )
    defaults.update(overrides)
    return OutboxEventEnvelope(**defaults)


def test_poison_message_reaches_dead_letter_stream_with_event_id_preserved(redis_client):
    stream = f"test:dlq:{uuid.uuid4()}"
    dlq_stream = dead_letter_stream_name(prefix=stream)
    envelope = _make_envelope()
    redis_client.xadd(stream, {"event": json.dumps(envelope.model_dump(mode="json"))})

    def always_fails(_: OutboxEventEnvelope) -> None:
        raise RuntimeError("this handler never succeeds")

    # max_deliveries=0 means even the first delivery (Redis's own delivery
    # count starts at 1) already exceeds the budget -- deterministic,
    # no need to actually drive multiple redelivery cycles against real
    # Redis (that's exercised by the retry-count boundary unit tests in
    # tests/unit/events/test_consumer_classification.py).
    consumer = RedisStreamConsumer(
        redis_client,
        stream=stream,
        group=f"cg:test-dlq-{uuid.uuid4()}",
        consumer_name="c1",
        handler=always_fails,
        is_duplicate=lambda event_id: False,
        mark_processed=lambda event_id: None,
        max_deliveries=0,
        dead_letter_stream=dlq_stream,
    )

    try:
        count = consumer.run_once()

        assert count == 1
        assert consumer.stats.dead_lettered == 1

        dlq_entries = redis_client.xrange(dlq_stream)
        assert len(dlq_entries) == 1
        _dlq_id, dlq_fields = dlq_entries[0]
        dlq_payload = json.loads(dlq_fields["event"])
        assert dlq_payload["event_id"] == str(envelope.event_id)
        assert dlq_fields["reason"] == "max_deliveries_exceeded"

        remaining = redis_client.xpending_range(stream, consumer._group, "-", "+", 10)
        assert remaining == []  # dead-lettered message was acked off the original stream
    finally:
        redis_client.delete(stream, dlq_stream)
