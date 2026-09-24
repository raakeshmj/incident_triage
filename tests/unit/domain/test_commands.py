from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.domain.commands import AlertReceivedCommand
from packages.domain.enums import AlertSeverity, AlertSource, AlertStatus


def _build(**overrides):
    defaults = dict(
        idempotency_key="k1",
        source=AlertSource.PROMETHEUS,
        external_id=None,
        labels={"service": "checkout", "environment": "production"},
        annotations={},
        severity=AlertSeverity.CRITICAL,
        status=AlertStatus.FIRING,
        raw_payload={},
    )
    defaults.update(overrides)
    return AlertReceivedCommand(**defaults)


def test_valid_command_exposes_service_and_environment():
    cmd = _build()
    assert cmd.service == "checkout"
    assert cmd.environment == "production"


@pytest.mark.parametrize(
    "labels",
    [
        {"environment": "production"},
        {"service": "checkout"},
        {"service": "", "environment": "production"},
        {"service": "checkout", "environment": ""},
        {},
    ],
)
def test_missing_or_empty_required_labels_rejected(labels):
    with pytest.raises(ValidationError):
        _build(labels=labels)


def test_invalid_severity_rejected():
    with pytest.raises(ValidationError):
        _build(severity="catastrophic")


def test_invalid_source_rejected():
    with pytest.raises(ValidationError):
        _build(source="datadog")


def test_empty_idempotency_key_rejected():
    with pytest.raises(ValidationError):
        _build(idempotency_key="")


def test_command_is_frozen():
    cmd = _build()
    with pytest.raises((ValidationError, TypeError)):
        cmd.severity = AlertSeverity.WARNING  # type: ignore[misc]
