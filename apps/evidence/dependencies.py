"""Composition root for evidence-service.

The only place that knows concrete endpoints and credentials. Everything
below it (packages/evidence) receives already-built clients and never reads
the environment -- and nothing reachable by a future agent receives any of
this (docs/architecture/13-security-boundaries.md).
"""

from __future__ import annotations

from functools import lru_cache

import httpx
import redis

from apps.evidence.config import get_evidence_settings
from packages.evidence import limits
from packages.evidence.adapters.changes import ChangeRegistryAdapter
from packages.evidence.adapters.git import GitAdapter
from packages.evidence.adapters.loki import LokiAdapter
from packages.evidence.adapters.prometheus import PrometheusAdapter
from packages.evidence.adapters.tempo import TempoAdapter
from packages.evidence.db.base import make_engine, make_session_factory
from packages.evidence.scope import ServiceCatalog
from packages.evidence.service import EvidenceService
from packages.incident.db.base import make_engine as make_incident_engine
from packages.incident.db.base import make_session_factory as make_incident_session_factory
from packages.incident.service import IncidentCoreService


def _http(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=limits.BACKEND_TIMEOUT_SECONDS)


@lru_cache
def get_evidence_service() -> EvidenceService:
    settings = get_evidence_settings()
    gateway = IncidentCoreService(
        make_incident_session_factory(make_incident_engine(settings.incident_core_database_url))
    )
    return EvidenceService(
        session_factory=make_session_factory(make_engine(settings.evidence_database_url)),
        gateway=gateway,
        catalog=ServiceCatalog.load(settings.evidence_service_catalog_path),
        prometheus=PrometheusAdapter(_http(settings.prometheus_url)),
        loki=LokiAdapter(_http(settings.loki_url)),
        tempo=TempoAdapter(_http(settings.tempo_url)),
        changes=ChangeRegistryAdapter(
            redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_timeout=limits.BACKEND_TIMEOUT_SECONDS,
            )
        ),
        git=GitAdapter(settings.evidence_git_repo_path),
    )
