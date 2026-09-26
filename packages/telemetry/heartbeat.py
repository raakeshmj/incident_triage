"""Worker heartbeats: `heartbeat:{role}:{identity}` keys in Redis with a TTL.

Each long-running worker beats once per loop. A key that exists means the
worker ran a loop within `ttl_seconds`; an expired key means it stopped
(or is stuck). The operations overview reads these -- worker health shown
on the dashboard is exactly this, nothing inferred.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from typing import Any

import redis

PREFIX = "heartbeat:"


class Heartbeat:
    def __init__(self, client: redis.Redis, role: str, *, ttl_seconds: int = 30) -> None:
        self._redis = client
        self._key = f"{PREFIX}{role}:{socket.gethostname()}:{os.getpid()}"
        self._role = role
        self._ttl = ttl_seconds
        self._started = time.time()

    def beat(self, **details: Any) -> None:
        with contextlib.suppress(redis.RedisError):  # a heartbeat must never take a worker down
            self._redis.set(
                self._key,
                json.dumps(
                    {"role": self._role, "at": time.time(), "started": self._started, **details}
                ),
                ex=self._ttl,
            )


def read_heartbeats(client: redis.Redis) -> list[dict[str, Any]]:
    beats = []
    for key in client.scan_iter(f"{PREFIX}*"):
        raw = client.get(key)
        if raw:
            data = json.loads(raw)  # type: ignore[arg-type]
            data["key"] = key
            data["age_seconds"] = round(time.time() - float(data.get("at", 0)), 1)
            beats.append(data)
    return sorted(beats, key=lambda b: (b.get("role", ""), b["key"]))
