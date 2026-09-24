"""The outbox event envelope.

Mirrors docs/architecture/05-event-model.md's "Event envelope" shape.
This is what gets serialized into `outbox_events.payload`'s sibling
columns and what a future Redis Streams consumer will actually receive.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class OutboxEventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID
    event_type: str
    schema_version: int = 1
    aggregate_type: str
    aggregate_id: uuid.UUID
    occurred_at: datetime
    correlation_id: uuid.UUID | None = None
    causation_id: uuid.UUID | None = None
    producer: str
    payload: dict
