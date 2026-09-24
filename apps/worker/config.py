from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    incident_core_database_url: str
    redis_url: str = "redis://localhost:6379/0"
    outbox_redis_stream: str = "stream:events"
    log_level: str = "INFO"
    poll_interval_seconds: float = 2.0
