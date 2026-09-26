"""Composition root: wires concrete implementations for the process.

This is the one place in apps/api allowed to import packages.incident.db --
it's infrastructure wiring for the whole process, not the alert-ingestion
request-handling code path itself (see packages/incident/README.md,
"Why other services don't import db/* directly", and Phase 1 requirement
6). apps/api/routers/alerts.py only ever depends on `IncidentCoreService`.
"""

from __future__ import annotations

from functools import lru_cache

import redis

from apps.api.config import get_settings
from packages.evaluation.recording import EvidenceStoreReader
from packages.evidence.db.base import make_engine as make_evidence_engine
from packages.evidence.db.base import make_session_factory as make_evidence_sessions
from packages.evidence.scope import ServiceCatalog
from packages.incident.db.base import make_engine, make_session_factory
from packages.incident.investigations import InvestigationCoreService
from packages.incident.queries import IncidentQueryService
from packages.incident.remediations import RemediationCoreService
from packages.incident.service import IncidentCoreService
from packages.incident.verifications import VerificationCoreService


@lru_cache
def get_incident_core_service() -> IncidentCoreService:
    settings = get_settings()
    engine = make_engine(settings.incident_core_database_url)
    session_factory = make_session_factory(engine)
    return IncidentCoreService(session_factory)


@lru_cache
def get_remediation_service() -> RemediationCoreService:
    settings = get_settings()
    engine = make_engine(settings.incident_core_database_url)
    return RemediationCoreService(
        make_session_factory(engine),
        topology=ServiceCatalog.load(settings.evidence_service_catalog_path),
    )


@lru_cache
def get_query_service() -> IncidentQueryService:
    settings = get_settings()
    sessions = make_session_factory(make_engine(settings.incident_core_database_url))
    return IncidentQueryService(
        sessions,
        investigations=InvestigationCoreService(sessions),
        remediations=get_remediation_service(),
        verifications=VerificationCoreService(sessions),
    )


@lru_cache
def get_evidence_reader() -> EvidenceStoreReader:
    settings = get_settings()
    return EvidenceStoreReader(
        make_evidence_sessions(make_evidence_engine(settings.evidence_database_url))
    )


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
