"""The Alert entity (read model).

Persisted exclusively by incident-core -- see
docs/architecture/03-domain-model.md and 02-component-boundaries.md.
This module only describes the shape of an already-validated, persisted
alert; validation of *inbound* data happens in commands.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.enums import AlertSeverity, AlertSource, AlertStatus


class Alert(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    external_id: str | None
    source: AlertSource
    fingerprint: str
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    severity: AlertSeverity
    status: AlertStatus
    incident_id: uuid.UUID | None
    raw_payload: dict
    received_at: datetime
