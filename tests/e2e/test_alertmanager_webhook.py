"""POST /api/v1/alerts/alertmanager: real Alertmanager webhook JSON shapes,
driven in-process (FastAPI TestClient) against real Postgres -- same
pattern as test_alert_flow.py.

Covers the payload-mapping and correlation behavior of the adapter; the
actual containerized Alertmanager -> host API wire integration (starting
the real `alertmanager` container from docker-compose) is
tests/e2e/test_alertmanager_container.py, per the Phase 3 requirement not
to fake that specific hop.
"""

from __future__ import annotations

import uuid

from packages.incident.db.models import AlertRow

WEBHOOK_TOKEN = "dev-local-alertmanager-token"
AUTH_HEADERS = {"Authorization": f"Bearer {WEBHOOK_TOKEN}"}


def _alertmanager_alert(
    *,
    alertname: str,
    service: str,
    environment: str = "production",
    region: str = "us-east-1",
    severity: str = "critical",
    alert_type: str = "availability",
    status: str = "firing",
    fingerprint: str | None = None,
    starts_at: str = "2024-06-01T12:00:00Z",
) -> dict:
    return {
        "status": status,
        "labels": {
            "alertname": alertname,
            "service": service,
            "environment": environment,
            "region": region,
            "severity": severity,
            "alert_type": alert_type,
        },
        "annotations": {
            "summary": f"{alertname} on {service}",
            "description": f"{alertname} fired for {service} in {environment}/{region}",
            "runbook_url": f"https://runbooks.example.com/incident-intelligence/{alertname.lower()}",
        },
        "startsAt": starts_at,
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": f"http://prometheus:9090/graph?g0.expr={alertname}",
        "fingerprint": fingerprint or uuid.uuid4().hex,
    }


def _webhook_payload(*alerts: dict) -> dict:
    return {
        "version": "4",
        "groupKey": '{}:{alertname="HighErrorRate"}',
        "status": "firing",
        "receiver": "incident-intelligence",
        "groupLabels": {"alertname": alerts[0]["labels"]["alertname"]},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "alerts": list(alerts),
    }


def test_single_alert_maps_to_incident_with_metadata_intact(client, session_factory):
    fingerprint = uuid.uuid4().hex
    alert = _alertmanager_alert(
        alertname="HighErrorRate", service="checkout-service", fingerprint=fingerprint
    )
    payload = _webhook_payload(alert)

    response = client.post("/api/v1/alerts/alertmanager", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 202
    body = response.json()
    assert len(body["accepted"]) == 1
    accepted = body["accepted"][0]
    assert accepted["incident_created"] is True

    incident = client.get(f"/api/v1/incidents/{accepted['incident_id']}").json()
    assert incident["service"] == "checkout-service"
    assert incident["environment"] == "production"
    assert incident["severity"] == "critical"
    assert incident["alerts"][0]["source"] == "prometheus"

    with session_factory() as session:
        alert_row = session.get(AlertRow, uuid.UUID(accepted["alert_id"]))
    assert alert_row is not None
    # `external_id` is Alertmanager's own fingerprint, reused verbatim for
    # idempotency; `fingerprint` is incident-core's *own* correlation
    # fingerprint (packages.domain.correlation.compute_fingerprint over
    # source+labels) -- a distinct concept, not expected to match.
    assert alert_row.external_id == fingerprint
    assert alert_row.labels["alert_type"] == "availability"
    assert alert_row.labels["region"] == "us-east-1"
    assert alert_row.annotations["runbook_url"].endswith("higherrorrate")
    assert alert_row.raw_payload["fingerprint"] == fingerprint


def test_multiple_alerts_same_service_correlate_into_one_incident(client):
    alerts = [
        _alertmanager_alert(
            alertname="HighErrorRate",
            service="payment-service",
            severity="critical",
            starts_at="2024-06-01T12:00:00Z",
        ),
        _alertmanager_alert(
            alertname="HighP95Latency",
            service="payment-service",
            severity="warning",
            alert_type="performance",
            starts_at="2024-06-01T12:00:10Z",
        ),
    ]
    response = client.post(
        "/api/v1/alerts/alertmanager", json=_webhook_payload(*alerts), headers=AUTH_HEADERS
    )
    assert response.status_code == 202
    accepted = response.json()["accepted"]
    assert len(accepted) == 2
    assert accepted[0]["incident_id"] == accepted[1]["incident_id"]
    assert accepted[0]["incident_created"] is True
    assert accepted[1]["incident_created"] is False


def test_alerts_for_unrelated_services_do_not_correlate(client):
    alerts = [
        _alertmanager_alert(alertname="HighErrorRate", service="checkout-service"),
        _alertmanager_alert(alertname="HighErrorRate", service="inventory-service"),
    ]
    response = client.post(
        "/api/v1/alerts/alertmanager", json=_webhook_payload(*alerts), headers=AUTH_HEADERS
    )
    assert response.status_code == 202
    accepted = response.json()["accepted"]
    assert accepted[0]["incident_id"] != accepted[1]["incident_id"]
    assert accepted[0]["incident_created"] is True
    assert accepted[1]["incident_created"] is True


def test_duplicate_webhook_delivery_is_idempotent_via_fingerprint(client):
    fingerprint = uuid.uuid4().hex
    alert = _alertmanager_alert(
        alertname="DependencyFailureRate", service="checkout-service", fingerprint=fingerprint
    )
    payload = _webhook_payload(alert)

    first = client.post("/api/v1/alerts/alertmanager", json=payload, headers=AUTH_HEADERS)
    second = client.post("/api/v1/alerts/alertmanager", json=payload, headers=AUTH_HEADERS)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()


def test_resolved_status_is_accepted_and_recorded(client):
    alert = _alertmanager_alert(
        alertname="HighErrorRate", service="checkout-service", status="resolved"
    )
    response = client.post(
        "/api/v1/alerts/alertmanager", json=_webhook_payload(alert), headers=AUTH_HEADERS
    )
    assert response.status_code == 202
    incident = client.get(
        f"/api/v1/incidents/{response.json()['accepted'][0]['incident_id']}"
    ).json()
    assert incident["alerts"][0]["status"] == "resolved"


def test_missing_required_label_returns_422(client):
    alert = _alertmanager_alert(alertname="HighErrorRate", service="checkout-service")
    del alert["labels"]["environment"]
    response = client.post(
        "/api/v1/alerts/alertmanager", json=_webhook_payload(alert), headers=AUTH_HEADERS
    )
    assert response.status_code == 422


def test_invalid_severity_label_returns_422(client):
    alert = _alertmanager_alert(
        alertname="HighErrorRate", service="checkout-service", severity="sev1-urgent"
    )
    response = client.post(
        "/api/v1/alerts/alertmanager", json=_webhook_payload(alert), headers=AUTH_HEADERS
    )
    assert response.status_code == 422


def test_missing_bearer_token_is_rejected(client):
    alert = _alertmanager_alert(alertname="HighErrorRate", service="checkout-service")
    response = client.post("/api/v1/alerts/alertmanager", json=_webhook_payload(alert))
    assert response.status_code == 401


def test_wrong_bearer_token_is_rejected(client):
    alert = _alertmanager_alert(alertname="HighErrorRate", service="checkout-service")
    response = client.post(
        "/api/v1/alerts/alertmanager",
        json=_webhook_payload(alert),
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
