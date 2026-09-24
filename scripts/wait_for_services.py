#!/usr/bin/env python3
"""Block until local Postgres and Redis (docker-compose) are accepting
connections. Used by `make infra-up` so subsequent `make migrate` /
`make run-api` calls don't race container startup.
"""

from __future__ import annotations

import os
import sys
import time

import psycopg
import redis

DEFAULT_TIMEOUT_SECONDS = 60


def wait_for_postgres(timeout: float) -> None:
    dsn = (
        f"host=localhost port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'incident_intelligence')} "
        f"user={os.environ.get('POSTGRES_SUPERUSER', 'postgres')} "
        f"password={os.environ.get('POSTGRES_SUPERUSER_PASSWORD', 'postgres')}"
    )
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2):
                print("postgres: ready")
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1)
    raise TimeoutError(f"postgres not ready after {timeout}s: {last_error}")


def wait_for_redis(timeout: float) -> None:
    url = os.environ.get("REDIS_URL", f"redis://localhost:{os.environ.get('REDIS_PORT', '6379')}/0")
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client = redis.from_url(url, socket_connect_timeout=2)
            if client.ping():
                print("redis: ready")
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1)
    raise TimeoutError(f"redis not ready after {timeout}s: {last_error}")


def main() -> None:
    timeout = float(os.environ.get("WAIT_FOR_SERVICES_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    wait_for_postgres(timeout)
    wait_for_redis(timeout)


if __name__ == "__main__":
    try:
        main()
    except TimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
