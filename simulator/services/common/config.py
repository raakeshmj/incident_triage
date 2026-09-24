"""Environment-driven configuration for a simulated service.

12-factor: every knob comes from the environment, set in docker-compose.yml
per service. No shared config file (mirrors docs/architecture/12-local-development.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceConfig:
    service_name: str
    port: int
    environment: str
    region: str
    version: str
    previous_version: str
    deployed_at: str
    otlp_endpoint: str
    redis_url: str

    @classmethod
    def from_env(cls) -> ServiceConfig:
        return cls(
            service_name=os.environ["SERVICE_NAME"],
            port=int(os.environ.get("SERVICE_PORT", "8000")),
            environment=os.environ.get("ENVIRONMENT", "production"),
            region=os.environ.get("REGION", "us-east-1"),
            version=os.environ.get("SERVICE_VERSION", "1.0.0"),
            previous_version=os.environ.get("SERVICE_PREVIOUS_VERSION", "0.9.0"),
            deployed_at=os.environ.get("SERVICE_DEPLOYED_AT", "1970-01-01T00:00:00Z"),
            otlp_endpoint=os.environ.get(
                "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        )
