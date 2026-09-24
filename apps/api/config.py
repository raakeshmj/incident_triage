from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    incident_core_database_url: str
    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
