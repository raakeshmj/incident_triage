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
