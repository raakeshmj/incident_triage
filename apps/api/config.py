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
    # Phase 7 operator endpoints (approvals, operator proposals, kill switches).
    # A shared operator token plus a static approver roster stand in for a real
    # identity provider (documented limitation). Empty token disables the
    # operator endpoints entirely -- the safe default outside local dev.
    operator_api_token: str = ""
    # "alice=service_owner|on_call_engineer,bob=on_call_engineer"
    remediation_approvers: str = ""
    evidence_service_catalog_path: str = "infrastructure/evidence/service-catalog.json"
    # Phase 8 read model: the evidence store (evidence detail links) and Redis
    # (dead-letter stream length, worker heartbeats) for the operations console.
    evidence_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    outbox_stream_prefix: str = "stream:events"

    def approver_roles(self) -> dict[str, list[str]]:
        roster: dict[str, list[str]] = {}
        for item in filter(None, (p.strip() for p in self.remediation_approvers.split(","))):
            name, _, roles = item.partition("=")
            roster[name.strip()] = [r.strip() for r in roles.split("|") if r.strip()]
        return roster


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
