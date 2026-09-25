from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class EvidenceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    evidence_database_url: str
    # V1 in-process composition only: the IncidentGateway is IncidentCoreService
    # itself (same pattern as apps/api's alert-ingestion). In a split
    # deployment this becomes incident-core's URL, not a database credential.
    incident_core_database_url: str
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    tempo_url: str = "http://localhost:3200"
    redis_url: str = "redis://localhost:6379/0"
    evidence_git_repo_path: str = "."
    evidence_service_catalog_path: str = "infrastructure/evidence/service-catalog.json"
    log_level: str = "INFO"


@lru_cache
def get_evidence_settings() -> EvidenceSettings:
    return EvidenceSettings()  # type: ignore[call-arg]
