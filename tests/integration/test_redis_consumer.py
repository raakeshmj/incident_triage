"""Real Redis + real Postgres: the consumer abstraction end to end,
including consumer-side idempotency backed by the `consumed_events` table
(not just Redis's own ack tracking) -- Phase 2 requirement 3.
"""

from __future__ import annotations

import json
import uuid

import pytest
import redis as redis_lib

from apps.worker.consumer_main import make_is_duplicate, make_mark_processed
from packages.events.consumer import RedisStreamConsumer
from packages.events.envelope import OutboxEventEnvelope
from packages.events.streams import consumer_group_name


@pytest.fixture
def redis_client():
    import os

    client = redis_lib.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        client.ping()
    except redis_lib.exceptions.ConnectionError as exc:
        pytest.skip(f"Redis not reachable ({exc}); run `make infra-up` first")
    yield client


def _make_envelope(**overrides) -> OutboxEventEnvelope:
    import datetime

    defaults = dict(
        event_id=uuid.uuid4(),
        event_type="AlertReceived",
        aggregate_type="Alert",
        aggregate_id=uuid.uuid4(),
        occurred_at=datetime.datetime.now(datetime.UTC),
        producer="test",
        payload={"hello": "world"},
    )
    defaults.update(overrides)
    return OutboxEventEnvelope(**defaults)


def _publish(redis_client, stream: str, envelope: OutboxEventEnvelope) -> None:
    redis_client.xadd(stream, {"event": json.dumps(envelope.model_dump(mode="json"))})


def test_consumer_processes_and_records_ledger_entry(redis_client, session_factory):
    stream = f"test:consumer:{uuid.uuid4()}"
    ledger_name = f"test-consumer-{uuid.uuid4()}"
    envelope = _make_envelope()
    _publish(redis_client, stream, envelope)

    handled = []
    consumer = RedisStreamConsumer(
        redis_client,
        stream=stream,
        group=consumer_group_name(ledger_name),
        consumer_name="c1",
        handler=lambda e: handled.append(e.event_id),
        is_duplicate=make_is_duplicate(session_factory, ledger_name),
        mark_processed=make_mark_processed(session_factory, ledger_name),
    )

    count = consumer.run_once()

    assert count == 1
    assert handled == [envelope.event_id]
    assert consumer.stats.processed == 1

    with session_factory() as session:
        from packages.incident import repository

        assert repository.has_consumed_event(
            session, consumer_name=ledger_name, event_id=envelope.event_id
        )


def test_duplicate_delivery_is_not_double_processed(redis_client, session_factory):
    """Simulates the exact scenario the outbox relay's own docstring
    describes as an accepted at-least-once duplicate: the same event_id
    delivered on the stream twice (e.g. relay crash after XADD, before
    marking published -- see apps/worker/main.py). The consumer's
    Postgres-backed dedup ledger, not Redis, is what prevents a second
    side effect.
    """
    stream = f"test:consumer:{uuid.uuid4()}"
    ledger_name = f"test-consumer-{uuid.uuid4()}"
    envelope = _make_envelope()
    _publish(redis_client, stream, envelope)

    handled = []

    def make_consumer():
        return RedisStreamConsumer(
            redis_client,
            stream=stream,
            group=consumer_group_name(ledger_name),
            consumer_name="c1",
            handler=lambda e: handled.append(e.event_id),
            is_duplicate=make_is_duplicate(session_factory, ledger_name),
            mark_processed=make_mark_processed(session_factory, ledger_name),
        )

    first_pass = make_consumer()
    assert first_pass.run_once() == 1
    assert len(handled) == 1

    # Simulate the relay re-publishing the identical event (same event_id,
    # new Redis message id) after a crash between publish and commit.
    _publish(redis_client, stream, envelope)

    second_pass = make_consumer()
    count = second_pass.run_once()

    assert count == 1  # the duplicate message was still handled (skipped + acked)
    assert len(handled) == 1  # but the handler was NOT invoked a second time
    assert second_pass.stats.duplicates_skipped == 1
