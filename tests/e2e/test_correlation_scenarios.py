"""The six Phase 2 e2e scenarios, driven through the real FastAPI app
(TestClient) against real Postgres and (for scenario 5) real Redis.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from apps.worker.main import relay_once
from packages.events.publisher import LoggingEventPublisher
from packages.incident.db.models import AlertRow, IncidentRow, OutboxEventRow
from tests.fakes import AlwaysFailingPublisher, FlakyPublisher


def _alert_payload(**overrides):
    payload = {
        "source": "prometheus",
        "labels": {
            "service": "checkout",
            "environment": "production",
            "alertname": "HighErrorRate",
            "region": "us-east",
        },
        "annotations": {},
        "severity": "critical",
        "status": "firing",
    }
    payload.update(overrides)
    return payload


# 1. One alert creates one incident.
def test_scenario_1_one_alert_creates_one_incident(client, session_factory):
    response = client.post("/api/v1/alerts", json=_alert_payload(external_id=f"s1-{uuid.uuid4()}"))
    assert response.status_code == 202
    body = response.json()
    assert body["incident_created"] is True

    with session_factory() as session:
        incidents = session.execute(select(IncidentRow)).scalars().all()
    assert len(incidents) == 1


# 2. Three related alerts produce one incident with three alerts.
def test_scenario_2_three_related_alerts_produce_one_incident_with_three_alerts(
    client, session_factory
):
    service = f"checkout-{uuid.uuid4()}"
    r1 = client.post(
        "/api/v1/alerts",
        json=_alert_payload(
            external_id="rel-1", labels={**_alert_payload()["labels"], "service": service}
        ),
    )
    r2 = client.post(
        "/api/v1/alerts",
        json=_alert_payload(
            external_id="rel-2", labels={**_alert_payload()["labels"], "service": service}
        ),
    )
    r3 = client.post(
        "/api/v1/alerts",
        json=_alert_payload(
            external_id="rel-3",
            labels={
                **_alert_payload()["labels"],
                "service": service,
                "alertname": "HighLatency",  # different type, still correlates via other signals
            },
        ),
    )

    incident_id = r1.json()["incident_id"]
    assert r2.json()["incident_id"] == incident_id
    assert r3.json()["incident_id"] == incident_id
    assert r1.json()["incident_created"] is True
    assert r2.json()["incident_created"] is False
    assert r3.json()["incident_created"] is False

    incident = client.get(f"/api/v1/incidents/{incident_id}").json()
    assert len(incident["alerts"]) == 3


# 3. Unrelated alerts produce separate incidents.
def test_scenario_3_unrelated_alerts_produce_separate_incidents(client):
    r1 = client.post(
        "/api/v1/alerts",
        json=_alert_payload(
            external_id=f"unrel-a-{uuid.uuid4()}",
            labels={
                "service": f"svc-a-{uuid.uuid4()}",
                "environment": "production",
                "alertname": "HighErrorRate",
            },
        ),
    )
    r2 = client.post(
        "/api/v1/alerts",
        json=_alert_payload(
            external_id=f"unrel-b-{uuid.uuid4()}",
            labels={
                "service": f"svc-b-{uuid.uuid4()}",
                "environment": "production",
                "alertname": "DiskFull",
            },
        ),
    )

    assert r1.json()["incident_id"] != r2.json()["incident_id"]
    assert r1.json()["incident_created"] is True
    assert r2.json()["incident_created"] is True


# 4. Duplicate delivery does not duplicate domain state.
def test_scenario_4_duplicate_delivery_does_not_duplicate_domain_state(client, session_factory):
    payload = _alert_payload(external_id=f"dup-{uuid.uuid4()}")

    first = client.post("/api/v1/alerts", json=payload)
    second = client.post("/api/v1/alerts", json=payload)  # identical request, replayed

    assert first.json() == second.json()

    with session_factory() as session:
        alerts = (
            session.execute(select(AlertRow).where(AlertRow.external_id == payload["external_id"]))
            .scalars()
            .all()
        )
    assert len(alerts) == 1


# 5. Relay crash/retry does not corrupt state.
def test_scenario_5_relay_crash_retry_does_not_corrupt_state(client, session_factory):
    """Simulates apps/worker/main.py's documented crash scenario: a relay
    pass that fails outright (e.g. Redis unreachable), followed by a pass
    that succeeds. No event is lost, none is silently marked published
    without actually reaching a publisher, and none is double-marked.
    """
    response = client.post("/api/v1/alerts", json=_alert_payload(external_id=f"s5-{uuid.uuid4()}"))
    incident_id = response.json()["incident_id"]

    with session_factory() as session:
        events_before = (
            session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.correlation_id == uuid.UUID(incident_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(events_before) == 2
    assert all(e.published_at is None for e in events_before)

    # Pass 1: total outage. Nothing gets marked published.
    crashed_pass = relay_once(session_factory, AlwaysFailingPublisher(), max_publish_attempts=1)
    assert crashed_pass == 0

    with session_factory() as session:
        events_mid = (
            session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.correlation_id == uuid.UUID(incident_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(events_mid) == 2  # nothing lost
    assert all(e.published_at is None for e in events_mid)  # nothing falsely marked published

    # Pass 2: recovered. Both events publish exactly once each.
    recovered_pass = relay_once(
        session_factory, FlakyPublisher(LoggingEventPublisher(), fail_times=0)
    )
    assert recovered_pass == 2

    with session_factory() as session:
        events_after = (
            session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.correlation_id == uuid.UUID(incident_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(events_after) == 2  # still exactly 2 rows -- no duplication
    assert all(e.published_at is not None for e in events_after)

    # A third pass finds nothing left to do -- idempotent at the relay level.
    assert relay_once(session_factory, LoggingEventPublisher()) == 0


# 6. Concurrent related alerts do not create duplicate incidents.
def test_scenario_6_concurrent_related_alerts_do_not_create_duplicate_incidents(
    client, session_factory
):
    service = f"concurrent-checkout-{uuid.uuid4()}"
    n = 6

    def send(i: int):
        return client.post(
            "/api/v1/alerts",
            json=_alert_payload(
                external_id=f"conc-{i}",
                labels={**_alert_payload()["labels"], "service": service},
            ),
        )

    with ThreadPoolExecutor(max_workers=n) as pool:
        responses = list(pool.map(send, range(n)))

    incident_ids = {r.json()["incident_id"] for r in responses}
    assert incident_ids == {list(incident_ids)[0]}  # all N alerts landed on the same one incident

    with session_factory() as session:
        incidents = (
            session.execute(select(IncidentRow).where(IncidentRow.service == service))
            .scalars()
            .all()
        )
    assert len(incidents) == 1
