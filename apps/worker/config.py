from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    incident_core_database_url: str
    redis_url: str = "redis://localhost:6379/0"
    outbox_stream_prefix: str = "stream:events"
    outbox_shard_count: int = 8
    log_level: str = "INFO"
    poll_interval_seconds: float = 2.0
    outbox_batch_size: int = 100
    outbox_max_publish_attempts: int = 3
    outbox_retry_backoff_seconds: float = 0.5

    # Consumer settings (apps/worker/consumer_main.py)
    consumer_purpose: str = "metrics-consumer"
    consumer_max_deliveries: int = 5
    consumer_claim_min_idle_ms: int = 30_000
