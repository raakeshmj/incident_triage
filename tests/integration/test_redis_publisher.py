from __future__ import annotations

import datetime
import os
import uuid

import pytest
import redis as redis_lib

from packages.events.envelope import OutboxEventEnvelope
from packages.events.publisher import RedisStreamEventPublisher
from packages.events.streams import stream_name_for_shard


def test_redis_stream_publisher_xadds_event():
    client = redis_lib.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        client.ping()
    except redis_lib.exceptions.ConnectionError as exc:
        pytest.skip(f"Redis not reachable ({exc}); run `make infra-up` first")

    stream_prefix = f"test:stream:{uuid.uuid4()}"
    publisher = RedisStreamEventPublisher(client, stream_prefix=stream_prefix, shard_count=1)
    stream_key = stream_name_for_shard(0, prefix=stream_prefix)
    envelope = OutboxEventEnvelope(
        event_id=uuid.uuid4(),
        event_type="Test",
        aggregate_type="Alert",
        aggregate_id=uuid.uuid4(),
        producer="test",
        payload={"a": 1},
        occurred_at=datetime.datetime.now(datetime.UTC),
    )

    try:
        publisher.publish(envelope)
        entries = client.xrange(stream_key)
        assert len(entries) == 1
    finally:
        client.delete(stream_key)
