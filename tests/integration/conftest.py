from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from packages.incident.db.base import make_engine, make_session_factory
from packages.incident.service import IncidentCoreService


@pytest.fixture(scope="session")
def engine() -> Engine:
    eng = make_engine(os.environ["INCIDENT_CORE_DATABASE_URL"])
    try:
        with eng.connect():
            pass
    except OperationalError as exc:
        pytest.skip(
            f"Postgres not reachable at INCIDENT_CORE_DATABASE_URL ({exc}); "
            "run `make infra-up && make migrate` first"
        )
    return eng


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    def _truncate() -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE TABLE "
                    "incident_core.evidence_refs, "
                    "incident_core.outbox_events, "
                    "incident_core.processed_commands, "
                    "incident_core.consumed_events, "
                    "incident_core.alerts, "
                    "incident_core.incidents "
                    "RESTART IDENTITY CASCADE"
                )
            )

    _truncate()  # in case a prior run left state behind
    yield
    _truncate()


@pytest.fixture
def core(session_factory: sessionmaker[Session]) -> IncidentCoreService:
    return IncidentCoreService(session_factory)


# --- Phase 4: evidence store ---------------------------------------------------


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
