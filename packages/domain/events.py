"""Domain events owned and emitted by incident-core.

Payload shapes only -- the generic outbox envelope (event_id, sequence,
occurred_at, ...) lives in packages/events, since that's delivery
mechanics rather than business meaning. See
docs/architecture/05-event-model.md, "Event catalog (initial)".

Phase 2 note: `AlertLinked` (Phase 1) is renamed to `AlertCorrelated` and
its payload enriched with the correlation decision (matched signals,
score) -- see docs/adr/0015-deterministic-correlation-scoring-engine.md.
This is the same event (an alert joined an existing incident instead of
creating one), made explainable rather than a new concept.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from packages.domain.enums import AlertSeverity, AlertSource

PRODUCER_INCIDENT_CORE = "incident-core"

EVENT_TYPE_ALERT_RECEIVED = "AlertReceived"
EVENT_TYPE_INCIDENT_CREATED = "IncidentCreated"
EVENT_TYPE_ALERT_CORRELATED = "AlertCorrelated"

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
    # Explainability for the *negative* case: the best score found among
    # any candidate incidents considered, even though it fell below the
    # correlation threshold (or there were no candidates at all, in which
    # case this is 0.0 and matched_signals is empty). See
    # docs/architecture/05-event-model.md's Phase 2 addendum.
    best_candidate_score: float = 0.0
    matched_signals: tuple[str, ...] = ()


class AlertCorrelatedPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    alert_id: uuid.UUID
    incident_id: uuid.UUID
    score: float
    matched_signals: tuple[str, ...]
