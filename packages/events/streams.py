"""Redis Streams topology: naming, sharding, and consumer identity.

This is the concrete resolution of the open design question left in
docs/review/critical-review.md ("Event ordering") and ADR-0003: how to get
per-incident ordering out of Redis Streams, which has no native
partitioning. See docs/adr/0014-redis-streams-transport.md for the
reasoning.

**Sharding.** A fixed number of streams (`SHARD_COUNT`), keyed by a
consistent hash of the event's `correlation_id` (falling back to
`aggregate_id` if there is no correlation id). Every event for a given
incident always lands in the same shard, so a consumer that processes one
shard's stream sequentially sees that incident's events in emission order.
Cross-incident ordering across shards is not guaranteed and is not
needed -- see docs/architecture/05-event-model.md.

**Consumer groups.** One consumer group per logical consumer *purpose*
(e.g. `cg:metrics-consumer`), created against every shard stream. Each
running process is one consumer *identity* within that group
(`hostname:pid`), which is what Redis uses to track per-consumer pending
entries for crash recovery via `XAUTOCLAIM`.

**Dead-letter stream.** One shared stream, `stream:events:dlq`, regardless
of which shard a poison message came from -- there is no ordering
requirement across dead letters, and a single stream is simpler to
monitor/drain than one per shard.
"""

from __future__ import annotations

import hashlib
import os
import socket
import uuid

DEFAULT_STREAM_PREFIX = "stream:events"
DEFAULT_SHARD_COUNT = 8
DLQ_SUFFIX = "dlq"


def shard_for_key(key: str, shard_count: int = DEFAULT_SHARD_COUNT) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count


def shard_key_for_event(correlation_id: uuid.UUID | None, aggregate_id: uuid.UUID) -> str:
    """The value events are sharded by: the incident, wherever we know it."""
    return str(correlation_id) if correlation_id is not None else str(aggregate_id)


def stream_name_for_shard(shard: int, *, prefix: str = DEFAULT_STREAM_PREFIX) -> str:
    return f"{prefix}:{shard}"


def stream_name_for_event(
    correlation_id: uuid.UUID | None,
    aggregate_id: uuid.UUID,
    *,
    prefix: str = DEFAULT_STREAM_PREFIX,
    shard_count: int = DEFAULT_SHARD_COUNT,
) -> str:
    key = shard_key_for_event(correlation_id, aggregate_id)
    return stream_name_for_shard(shard_for_key(key, shard_count), prefix=prefix)


def all_stream_names(
    *, prefix: str = DEFAULT_STREAM_PREFIX, shard_count: int = DEFAULT_SHARD_COUNT
) -> list[str]:
    return [stream_name_for_shard(i, prefix=prefix) for i in range(shard_count)]


def dead_letter_stream_name(*, prefix: str = DEFAULT_STREAM_PREFIX) -> str:
    return f"{prefix}:{DLQ_SUFFIX}"


def consumer_group_name(purpose: str) -> str:
    return f"cg:{purpose}"


def consumer_identity() -> str:
    """A reasonably stable identity for this process, used as the Redis
    Streams consumer name within a group. Not required to be globally
    unique across all time -- only unique among consumers concurrently
    reading the same group, which hostname+pid satisfies for our
    deployment shape (one consumer process per pid per host).
    """
    return f"{socket.gethostname()}:{os.getpid()}"
