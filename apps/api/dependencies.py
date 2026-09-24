"""Composition root: wires concrete implementations for the process.

This is the one place in apps/api allowed to import packages.incident.db --
it's infrastructure wiring for the whole process, not the alert-ingestion
request-handling code path itself (see packages/incident/README.md,
"Why other services don't import db/* directly", and Phase 1 requirement
6). apps/api/routers/alerts.py only ever depends on `IncidentCoreService`.
"""

from __future__ import annotations

from functools import lru_cache

from apps.api.config import get_settings
from packages.incident.db.base import make_engine, make_session_factory
from packages.incident.service import IncidentCoreService


@lru_cache
def get_incident_core_service() -> IncidentCoreService:
    settings = get_settings()
    engine = make_engine(settings.incident_core_database_url)
    session_factory = make_session_factory(engine)
    return IncidentCoreService(session_factory)
