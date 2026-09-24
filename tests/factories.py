"""Shared test builders. Not a test module itself (no `test_` prefix)."""

from __future__ import annotations

import uuid

from packages.domain.commands import AlertReceivedCommand
from packages.domain.enums import AlertSeverity, AlertSource, AlertStatus


def make_alert_command(
    *,
    idempotency_key: str | None = None,
    source: AlertSource = AlertSource.PROMETHEUS,
    external_id: str | None = None,
    service: str = "checkout",
    environment: str = "production",
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    status: AlertStatus = AlertStatus.FIRING,
    extra_labels: dict[str, str] | None = None,
) -> AlertReceivedCommand:
    labels = {"service": service, "environment": environment, "alertname": "HighErrorRate"}
    if extra_labels:
        labels.update(extra_labels)
    return AlertReceivedCommand(
        idempotency_key=idempotency_key or str(uuid.uuid4()),
        source=source,
        external_id=external_id,
        labels=labels,
        annotations={},
        severity=severity,
        status=status,
        raw_payload={"labels": labels, "source": source.value},
    )
