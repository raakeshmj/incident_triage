from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    incident_core_database_url: str
    log_level: str = "INFO"
    # Shared-secret auth for POST /api/v1/alerts/alertmanager (13-security-boundaries.md's
    # "HMAC-signed / shared-secret webhook auth" for Zone 0 -> Zone 1). Empty
    # string disables the check -- never do that outside local dev.
    alertmanager_webhook_token: str = "dev-local-alertmanager-token"


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
