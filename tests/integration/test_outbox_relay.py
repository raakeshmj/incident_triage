from __future__ import annotations

from sqlalchemy import select

from apps.worker.main import relay_once
from packages.events.envelope import OutboxEventEnvelope
from packages.incident.db.models import OutboxEventRow
from tests.factories import make_alert_command


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[OutboxEventEnvelope] = []

    def publish(self, event: OutboxEventEnvelope) -> None:
        self.published.append(event)


def test_relay_once_publishes_and_marks_unpublished_events(core, session_factory):
    core.handle_alert_received(make_alert_command())
    publisher = _RecordingPublisher()

    count = relay_once(session_factory, publisher)

    assert count == 2
    assert {e.event_type for e in publisher.published} == {"AlertReceived", "IncidentCreated"}

    with session_factory() as session:
        events = session.execute(select(OutboxEventRow)).scalars().all()
    assert all(e.published_at is not None for e in events)

    # Nothing left unpublished on a second pass.
    assert relay_once(session_factory, publisher) == 0
