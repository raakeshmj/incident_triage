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
EVENT_TYPE_ALERT_RESOLVED = "AlertResolved"
EVENT_TYPE_INCIDENT_STATUS_CHANGED = "IncidentStatusChanged"
EVENT_TYPE_EVIDENCE_REF_REGISTERED = "EvidenceRefRegistered"

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


class AlertResolvedPayload(BaseModel):
    """A linked alert's firing episode ended (Phase 4).

    `firing_alerts_remaining` is the number of the incident's linked alerts
    still firing *after* this one resolved -- the exact fact the
    `TRIAGING -> CANCELLED` guard evaluates, carried in the event so a
    consumer can see why the incident did or didn't transition.
    """

    model_config = ConfigDict(frozen=True)

    alert_id: uuid.UUID
    incident_id: uuid.UUID
    firing_alerts_remaining: int


class IncidentStatusChangedPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    incident_id: uuid.UUID
    from_status: str
    to_status: str
    reason: str
    version: int


class EvidenceRefRegisteredPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: uuid.UUID
    incident_id: uuid.UUID
    evidence_type: str
    source_system: str
    content_hash: str


# --- Phase 5: investigations --------------------------------------------------------

EVENT_TYPE_INVESTIGATION_STARTED = "InvestigationStarted"
EVENT_TYPE_INVESTIGATION_COMPLETED = "InvestigationCompleted"
EVENT_TYPE_INVESTIGATION_FAILED = "InvestigationFailed"
AGGREGATE_TYPE_INVESTIGATION = "Investigation"


class InvestigationStartedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, protected_namespaces=())

    investigation_id: uuid.UUID
    incident_id: uuid.UUID
    attempt_number: int
    model_provider: str
    model_name: str


class InvestigationCompletedPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    investigation_id: uuid.UUID
    incident_id: uuid.UUID
    selected_hypothesis_id: uuid.UUID
    rca_report_id: uuid.UUID


class InvestigationFailedPayload(BaseModel):
    """Both `FAILED` (technical) and `ESCALATED` (inconclusive / budget)
    outcomes: either way no usable root cause, and the incident escalates."""

    model_config = ConfigDict(frozen=True)

    investigation_id: uuid.UUID
    incident_id: uuid.UUID
    outcome: str
    reason_code: str
