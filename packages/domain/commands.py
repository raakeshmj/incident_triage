"""Commands accepted by incident-core.

A command is a request that incident-core may accept or reject -- distinct
from a domain event, which is a fact incident-core has already committed.
See docs/architecture/05-event-model.md, "Commands vs. domain events".
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.domain.enums import AlertSeverity, AlertSource, AlertStatus

REQUIRED_LABELS = ("service", "environment")

COMMAND_TYPE_ALERT_RECEIVED = "AlertReceivedCommand"


class AlertReceivedCommand(BaseModel):
    """Sent by alert-ingestion to incident-core.

    Idempotency key scoping is `(command_type, idempotency_key)` -- see
    docs/adr/0011-idempotency-optimistic-concurrency.md -- so this model
    intentionally does not carry `command_type` itself; callers pass
    `COMMAND_TYPE_ALERT_RECEIVED` explicitly wherever the ledger is keyed.
    """

    model_config = ConfigDict(frozen=True)

    idempotency_key: str = Field(min_length=1)
    source: AlertSource
    external_id: str | None = None
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    severity: AlertSeverity
    status: AlertStatus = AlertStatus.FIRING
    raw_payload: dict

    @field_validator("labels")
    @classmethod
    def _require_service_and_environment(cls, value: dict[str, str]) -> dict[str, str]:
        missing = [key for key in REQUIRED_LABELS if not value.get(key)]
        if missing:
            raise ValueError(f"labels missing required keys: {', '.join(missing)}")
        return value

    @property
    def service(self) -> str:
        return self.labels["service"]

    @property
    def environment(self) -> str:
        return self.labels["environment"]
