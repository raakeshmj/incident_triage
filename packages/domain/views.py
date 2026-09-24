"""Read-model views returned by incident-core's query API.

Kept separate from `Alert`/`Incident` (the persisted entities) so the API
response shape can evolve independently of storage -- e.g. nesting alerts
under their incident, which is not how the two are related internally.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AlertView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    external_id: str | None
    source: str
    fingerprint: str
    labels: dict[str, str]
    annotations: dict[str, str]
    severity: str
    status: str
    received_at: datetime


class IncidentView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    status: str
    severity: str
    service: str
    environment: str
    correlation_key: str
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    alerts: list[AlertView]
