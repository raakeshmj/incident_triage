from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from packages.incident.db.base import make_session_factory

_ROOT = Path(__file__).resolve().parents[1]
# Load .env if present, then fall back to .env.example so the suite works
# out of the box against the documented docker-compose defaults.
load_dotenv(_ROOT / ".env", override=False)
load_dotenv(_ROOT / ".env.example", override=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        path = str(item.fspath)
        if "/tests/integration/" in path:
            item.add_marker(pytest.mark.integration)
        elif "/tests/e2e/" in path:
            item.add_marker(pytest.mark.e2e)


STACK_BACKENDS = {
    "prometheus": "PROMETHEUS_URL",
    "loki": "LOKI_URL",
    "tempo": "TEMPO_URL",
}
# Functional probes: single-binary Loki/Tempo serve queries while their ring-based
# /ready endpoints still report 503, so /ready is not a usable signal here.
_READY_PATHS = {"prometheus": "/-/ready", "loki": "/loki/api/v1/labels", "tempo": "/api/echo"}


@pytest.fixture(scope="session")
def stack_urls() -> dict[str, str]:
    """Base URLs of the live telemetry backends; skips unless all are ready."""
    import os

    import httpx

    urls = {name: os.environ.get(var, "") for name, var in STACK_BACKENDS.items()}
    for name, url in urls.items():
        try:
            ready = httpx.get(url + _READY_PATHS[name], timeout=3.0).status_code == 200
        except (httpx.HTTPError, ValueError):
            ready = False
        if not ready:
            pytest.skip(f"{name} not ready at {url!r}; run `make infra-up-full`")
    return urls


# --- shared by integration and e2e: evidence store + isolated Redis ------------------


@pytest.fixture(scope="session")
def evidence_engine() -> Engine:
    from packages.evidence.db.base import make_engine as make_evidence_engine

    eng = make_evidence_engine(os.environ["EVIDENCE_DATABASE_URL"])
    try:
        with eng.connect():
            pass
    except OperationalError as exc:
        pytest.skip(f"evidence database not reachable ({exc}); run `make migrate` first")
    return eng


@pytest.fixture
def evidence_session_factory(evidence_engine: Engine) -> Iterator[sessionmaker[Session]]:
    def _truncate() -> None:
        with evidence_engine.begin() as conn:
            # TRUNCATE doesn't fire the row-level immutability trigger --
            # test cleanup only; no application code path truncates.
            conn.execute(text("TRUNCATE TABLE evidence.evidence_records"))

    _truncate()
    yield make_session_factory(evidence_engine)
    _truncate()


@pytest.fixture
def test_redis():
    """Redis DB 15, flushed around each test -- never the live DB 0.

    The DB has to be set in the URL itself: `Redis.from_url(url, db=15)`
    lets the URL's own `/0` win, which is how an earlier version of this
    fixture flushed the live simulator registries and event streams.
    """
    from urllib.parse import urlsplit, urlunsplit

    import redis

    parts = urlsplit(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    client = redis.Redis.from_url(urlunsplit(parts._replace(path="/15")), decode_responses=True)
    assert client.connection_pool.connection_kwargs["db"] == 15, "refusing to flush a non-test DB"
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis not reachable ({exc})")
    client.flushdb()
    yield client
    client.flushdb()
