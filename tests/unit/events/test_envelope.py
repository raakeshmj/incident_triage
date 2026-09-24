from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.events.envelope import OutboxEventEnvelope


def _base_kwargs(**overrides):
    defaults = dict(
        event_id=uuid.uuid4(),
        event_type="AlertReceived",
        aggregate_type="Alert",
        aggregate_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        producer="incident-core",
        payload={"a": 1},
    )
    defaults.update(overrides)
    return defaults


def test_envelope_accepts_all_canonical_fields():
    envelope = OutboxEventEnvelope(
        **_base_kwargs(correlation_id=uuid.uuid4(), causation_id=uuid.uuid4())
    )
    assert envelope.schema_version == 1
    assert envelope.producer == "incident-core"


def test_envelope_requires_producer():
    kwargs = _base_kwargs()
    del kwargs["producer"]
    with pytest.raises(ValidationError):
        OutboxEventEnvelope(**kwargs)


def test_envelope_correlation_and_causation_default_to_none():
    envelope = OutboxEventEnvelope(**_base_kwargs())
    assert envelope.correlation_id is None
    assert envelope.causation_id is None


def test_envelope_is_frozen():
    envelope = OutboxEventEnvelope(**_base_kwargs())
    with pytest.raises((ValidationError, TypeError)):
        envelope.event_type = "Other"  # type: ignore[misc]


def test_envelope_round_trips_through_json():
    envelope = OutboxEventEnvelope(**_base_kwargs())
    restored = OutboxEventEnvelope.model_validate(envelope.model_dump(mode="json"))
    assert restored == envelope
