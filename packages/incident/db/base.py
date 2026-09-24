"""Engine/session construction for the `incident_core` schema.

incident-core connects with its own least-privilege Postgres role
(`incident_core_role`), scoped to the `incident_core` schema only -- see
ADR-0013 and docs/architecture/06-database-design.md. This module never
reads a superuser connection string; that's a migration-time concern
handled by Alembic's own config, not the running application.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

SCHEMA = "incident_core"


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None) -> Engine:
    url = database_url or os.environ["INCIDENT_CORE_DATABASE_URL"]
    return create_engine(url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
