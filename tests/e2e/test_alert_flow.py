"""End-to-end: POST an alert, retrieve the resulting incident, verify
persisted state and the outbox event (Phase 1 requirement 9).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from packages.incident.db.models import OutboxEventRow


def test_post_alert_then_get_incident_and_outbox_event(client, session_factory):
    payload = {
        "source": "prometheus",
        "labels": {
            "service": "checkout",
            "environment": "production",
            "alertname": "HighErrorRate",
        },
        "annotations": {"summary": "error rate above threshold"},
        "severity": "critical",
        "status": "firing",
    }

    post_response = client.post(
        "/api/v1/alerts", json=payload, headers={"X-Request-ID": "e2e-test-1"}
    )
    assert post_response.status_code == 202
    accepted = post_response.json()
    assert accepted["incident_created"] is True

    incident_id = accepted["incident_id"]
    get_response = client.get(f"/api/v1/incidents/{incident_id}")
    assert get_response.status_code == 200
    incident = get_response.json()

    assert incident["id"] == incident_id
    assert incident["status"] == "TRIAGING"
    assert incident["service"] == "checkout"
    assert incident["environment"] == "production"
    assert incident["severity"] == "critical"
    assert len(incident["alerts"]) == 1
    assert incident["alerts"][0]["id"] == accepted["alert_id"]
    assert incident["alerts"][0]["source"] == "prometheus"

    with session_factory() as session:
        events = (
            session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.correlation_id == uuid.UUID(incident_id)
                )
            )
            .scalars()
            .all()
        )
    assert {e.event_type for e in events} == {"AlertReceived", "IncidentCreated"}


def test_missing_required_label_returns_422(client):
    payload = {
        "source": "generic",
        "labels": {"service": "checkout"},  # missing "environment"
        "severity": "warning",
        "status": "firing",
    }
    response = client.post("/api/v1/alerts", json=payload)
    assert response.status_code == 422


def test_get_unknown_incident_returns_404(client):
    response = client.get(f"/api/v1/incidents/{uuid.uuid4()}")
    assert response.status_code == 404


def test_duplicate_post_with_same_external_id_is_idempotent(client):
    payload = {
        "source": "generic",
        "external_id": "ext-e2e-1",
        "labels": {"service": "payments", "environment": "production"},
        "severity": "info",
        "status": "firing",
    }
    first = client.post("/api/v1/alerts", json=payload)
    second = client.post("/api/v1/alerts", json=payload)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
