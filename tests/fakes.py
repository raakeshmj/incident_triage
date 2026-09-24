"""Fakes and fault-injection helpers shared across the test suite.

Not a general-purpose Redis fake -- just enough of the Streams command
surface for `packages.events.consumer.RedisStreamConsumer` and
`packages.events.publisher.RedisStreamEventPublisher` to run against it,
so their orchestration logic (ack/no-ack, dedup skip, dead-lettering) is
unit-testable without a live Redis server.
"""

from __future__ import annotations

import itertools
from collections import defaultdict

from redis.exceptions import ResponseError

from packages.events.envelope import OutboxEventEnvelope


class FlakyPublisher:
    """Wraps a real `EventPublisher`, raising on the first `fail_times`
    calls before delegating -- used to test the outbox relay's retry
    behavior (apps/worker/main.py) deterministically, without needing to
    actually break Redis.
    """

    def __init__(self, delegate, fail_times: int = 0, error: Exception | None = None) -> None:
        self._delegate = delegate
        self._fail_times = fail_times
        self._error = error or RuntimeError("injected publish failure")
        self.attempts = 0

    def publish(self, event: OutboxEventEnvelope) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise self._error
        self._delegate.publish(event)


class AlwaysFailingPublisher:
    """Every call raises. Used to test the "give up on this pass, retry
    next poll" path -- the event must stay unpublished with its failure
    recorded, never marked published.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or RuntimeError("permanent publish failure")
        self.attempts = 0

    def publish(self, event: OutboxEventEnvelope) -> None:
        self.attempts += 1
        raise self._error


class FakeRedisStreams:
    def __init__(self) -> None:
        self._streams: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        self._groups: dict[tuple[str, str], dict] = {}
        self._pending: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
        self._id_counter = itertools.count(1)

    def xadd(self, stream: str, fields: dict) -> str:
        message_id = f"{next(self._id_counter)}-0"
        self._streams[stream].append((message_id, dict(fields)))
        return message_id

    def xgroup_create(self, stream: str, group: str, id: str = "0", mkstream: bool = False) -> None:
        key = (stream, group)
        if key in self._groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self._groups[key] = {"last_delivered_index": 0}

    def xreadgroup(self, groupname, consumername, streams, count=10, block=None):
        result = []
        for stream, _sentinel in streams.items():
            key = (stream, groupname)
            state = self._groups.get(key)
            if state is None:
                continue
            start = state["last_delivered_index"]
            entries = self._streams[stream][start : start + count]
            if not entries:
                continue
            state["last_delivered_index"] += len(entries)
            messages = []
            for message_id, fields in entries:
                self._pending[key][message_id] = {"consumer": consumername, "delivery_count": 1}
                messages.append((message_id, fields))
            result.append((stream, messages))
        return result

    def xack(self, stream: str, group: str, *message_ids: str) -> int:
        key = (stream, group)
        acked = 0
        for message_id in message_ids:
            if self._pending[key].pop(message_id, None) is not None:
                acked += 1
        return acked

    def xpending_range(self, stream, group, min, max, count, consumername=None):
        key = (stream, group)
        entry = self._pending[key].get(min)
        if entry is None:
            return []
        return [
            {
                "message_id": min,
                "consumer": entry["consumer"],
                "time_since_delivered": 0,
                "times_delivered": entry["delivery_count"],
            }
        ]

    def xautoclaim(
        self, stream, group, consumername, min_idle_time, start_id="0-0", count=10, justid=False
    ):
        # Simplified deliberately: this fake never reclaims. Tests that need
        # to exercise poison/retry behavior set a low `max_deliveries`
        # instead of relying on reclaim timing.
        return "0-0", [], []
