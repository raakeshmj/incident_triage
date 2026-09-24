"""Domain events owned and emitted by incident-core.

Payload shapes only -- the generic outbox envelope (event_id, sequence,
occurred_at, ...) lives in packages/events, since that's delivery
mechanics rather than business meaning. See
docs/architecture/05-event-model.md, "Event catalog (initial)".
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from packages.domain.enums import AlertSeverity, AlertSource

EVENT_TYPE_ALERT_RECEIVED = "AlertReceived"
EVENT_TYPE_INCIDENT_CREATED = "IncidentCreated"
EVENT_TYPE_ALERT_LINKED = "AlertLinked"

AGGREGATE_TYPE_ALERT = "Alert"
AGGREGATE_TYPE_INCIDENT = "Incident"


class AlertReceivedPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    alert_id: uuid.UUID
    fingerprint: str
    source: AlertSource
    incident_id: uuid.UUID


class IncidentCreatedPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    incident_id: uuid.UUID
    correlation_key: str
    initial_severity: AlertSeverity
    service: str
    environment: str


class AlertLinkedPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    alert_id: uuid.UUID
    incident_id: uuid.UUID
