"""Engine/session construction for the `evidence` schema.

evidence-service connects as its own role (`evidence_service_role`), which
has grants on the `evidence` schema only -- it can neither read nor write
`incident_core`, and incident-core's role can't touch `evidence` (ADR-0013).
Everything evidence-service needs from incident-core goes through the
`IncidentGateway` interface (packages/evidence/scope.py), never SQL.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

SCHEMA = "evidence"


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None) -> Engine:
    url = database_url or os.environ["EVIDENCE_DATABASE_URL"]
    return create_engine(url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
