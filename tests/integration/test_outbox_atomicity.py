from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.domain.events import EVENT_TYPE_ALERT_RECEIVED, EVENT_TYPE_INCIDENT_CREATED
from packages.incident import repository
from packages.incident.db.models import AlertRow, IncidentRow, OutboxEventRow
from tests.factories import make_alert_command


def test_alert_incident_and_outbox_written_in_one_transaction(core, session_factory):
    result = core.handle_alert_received(make_alert_command())

    with session_factory() as session:
        events = (
            session.execute(
                select(OutboxEventRow).where(OutboxEventRow.correlation_id == result.incident_id)
            )
            .scalars()
            .all()
        )

    event_types = {e.event_type for e in events}
    assert event_types == {EVENT_TYPE_ALERT_RECEIVED, EVENT_TYPE_INCIDENT_CREATED}
    assert all(e.published_at is None for e in events)

    alert_event = next(e for e in events if e.event_type == EVENT_TYPE_ALERT_RECEIVED)
    assert alert_event.payload["alert_id"] == str(result.alert_id)
    assert alert_event.payload["incident_id"] == str(result.incident_id)

    incident_event = next(e for e in events if e.event_type == EVENT_TYPE_INCIDENT_CREATED)
    assert incident_event.payload["incident_id"] == str(result.incident_id)


def test_no_partial_success_when_outbox_write_fails(core, session_factory, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure after alert+incident insert")

    monkeypatch.setattr(repository, "insert_outbox_event", boom)

    command = make_alert_command()
    with pytest.raises(RuntimeError, match="simulated failure"):
        core.handle_alert_received(command)

    with session_factory() as session:
        alerts = session.execute(select(AlertRow)).scalars().all()
        incidents = session.execute(select(IncidentRow)).scalars().all()

    assert alerts == []
    assert incidents == []
