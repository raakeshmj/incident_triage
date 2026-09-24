from __future__ import annotations

from sqlalchemy import select

from packages.incident.db.models import AlertRow, IncidentRow
from tests.factories import make_alert_command


def test_new_alert_creates_incident(core, session_factory):
    command = make_alert_command()

    result = core.handle_alert_received(command)

    assert result.incident_created is True

    with session_factory() as session:
        alert = session.get(AlertRow, result.alert_id)
        incident = session.get(IncidentRow, result.incident_id)

    assert alert is not None
    assert alert.incident_id == result.incident_id
    assert incident is not None
    assert incident.status == "TRIAGING"
    assert incident.service == "checkout"
    assert incident.environment == "production"
    assert incident.severity == "critical"


def test_second_alert_with_same_fingerprint_links_to_existing_incident(core, session_factory):
    first = core.handle_alert_received(make_alert_command())
    # same labels+source as `first` -> same fingerprint -> links, doesn't create
    second = core.handle_alert_received(make_alert_command())

    assert first.incident_created is True
    assert second.incident_created is False
    assert second.incident_id == first.incident_id
    assert second.alert_id != first.alert_id

    with session_factory() as session:
        incident_count = session.execute(select(IncidentRow)).scalars().all()
        alert_count = session.execute(select(AlertRow)).scalars().all()

    assert len(incident_count) == 1
    assert len(alert_count) == 2
    assert {a.incident_id for a in alert_count} == {first.incident_id}


def test_different_fingerprint_creates_separate_incident(core):
    first = core.handle_alert_received(make_alert_command(service="checkout"))
    second = core.handle_alert_received(make_alert_command(service="payments"))

    assert first.incident_id != second.incident_id
    assert second.incident_created is True
