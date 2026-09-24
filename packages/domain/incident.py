"""The Incident aggregate (read model).

Persisted exclusively by incident-core. See
docs/architecture/03-domain-model.md and
docs/architecture/04-incident-state-machine.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from packages.domain.enums import AlertSeverity, IncidentStatus


class Incident(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    status: IncidentStatus
    severity: AlertSeverity
    service: str
    environment: str
    correlation_key: str
    attempt_count: int
    version: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
