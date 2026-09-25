"""Alert resolution semantics at the incident-core service boundary (Phase 4).

docs/architecture/04-incident-state-machine.md, "Alert resolution": the only
alert-driven exit from an open incident is `TRIAGING -> CANCELLED`, guarded
by "no linked alert still firing".
"""

from __future__ import annotations

import threading
import uuid

from sqlalchemy import select, text

from packages.domain.enums import AlertStatus
from packages.incident.db.models import AlertRow, IncidentRow, OutboxEventRow
from tests.factories import make_alert_command


def _events(session, incident_id, event_type):
    return (
        session.execute(
            select(OutboxEventRow).where(
                OutboxEventRow.correlation_id == incident_id,
                OutboxEventRow.event_type == event_type,
            )
        )
        .scalars()
        .all()
    )


def test_resolution_cancels_bumps_version_and_emits_events(core, session_factory):
    fired = core.handle_alert_received(make_alert_command(external_id="ep-1"))
    resolved = core.handle_alert_received(
        make_alert_command(external_id="ep-1", status=AlertStatus.RESOLVED)
    )

    assert resolved.incident_status == "CANCELLED"
    with session_factory() as session:
        incident = session.get(IncidentRow, fired.incident_id)
        assert incident.status == "CANCELLED"
        assert incident.version == 1
        assert incident.closed_at is not None

        [alert_resolved] = _events(session, fired.incident_id, "AlertResolved")
        assert alert_resolved.payload["firing_alerts_remaining"] == 0
        [status_changed] = _events(session, fired.incident_id, "IncidentStatusChanged")
        assert status_changed.payload == {
            "incident_id": str(fired.incident_id),
            "from_status": "TRIAGING",
            "to_status": "CANCELLED",
            "reason": "all_linked_alerts_resolved",
            "version": 1,
        }


def test_replayed_resolution_command_is_a_ledger_hit(core, session_factory):
    core.handle_alert_received(make_alert_command(external_id="ep-2"))
    command = make_alert_command(
        idempotency_key="resolve-ep-2", external_id="ep-2", status=AlertStatus.RESOLVED
    )
    first = core.handle_alert_received(command)
    second = core.handle_alert_received(command)
    assert first == second
    with session_factory() as session:
        assert len(_events(session, first.incident_id, "AlertResolved")) == 1


def test_resolution_without_external_id_matches_by_label_fingerprint(core, session_factory):
    fired = core.handle_alert_received(make_alert_command())
    resolved = core.handle_alert_received(make_alert_command(status=AlertStatus.RESOLVED))
    assert resolved.alert_id == fired.alert_id
    assert resolved.incident_status == "CANCELLED"


def test_resolution_past_triaging_records_but_does_not_transition(core, session_factory, engine):
    fired = core.handle_alert_received(make_alert_command(external_id="ep-3"))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE incident_core.incidents SET status = 'INVESTIGATING' WHERE id = :id"),
            {"id": fired.incident_id},
        )

    resolved = core.handle_alert_received(
        make_alert_command(external_id="ep-3", status=AlertStatus.RESOLVED)
    )

    assert resolved.incident_status == "INVESTIGATING"
    with session_factory() as session:
        assert session.get(AlertRow, fired.alert_id).status == "resolved"
        assert _events(session, fired.incident_id, "IncidentStatusChanged") == []


def test_concurrent_firing_and_resolution_never_cancel_an_incident_with_a_firing_alert(
    core, session_factory
):
    """Alert A resolves while a related alert B is correlating into the same
    incident. The advisory lock serializes them, so exactly one of two
    outcomes is possible -- and neither leaves B firing on a CANCELLED
    incident."""
    for _ in range(10):
        episode = uuid.uuid4().hex
        fired_a = core.handle_alert_received(make_alert_command(external_id=f"a-{episode}"))
        results: dict = {}

        def resolve_a(ep: str = episode, results: dict = results) -> None:
            results["a"] = core.handle_alert_received(
                make_alert_command(external_id=f"a-{ep}", status=AlertStatus.RESOLVED)
            )

        def fire_b(ep: str = episode, results: dict = results) -> None:
            results["b"] = core.handle_alert_received(
                make_alert_command(
                    external_id=f"b-{ep}", extra_labels={"alertname": "HighP95Latency"}
                )
            )

        threads = [threading.Thread(target=resolve_a), threading.Thread(target=fire_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with session_factory() as session:
            incident_a = session.get(IncidentRow, fired_a.incident_id)
            alert_b = session.get(AlertRow, results["b"].alert_id)
            incident_b = session.get(IncidentRow, alert_b.incident_id)
        if alert_b.incident_id == fired_a.incident_id:
            # B joined first: A's resolution saw B firing, no cancellation.
            assert incident_a.status == "TRIAGING"
        else:
            # A cancelled first: B saw no open candidate and got a new incident.
            assert incident_a.status == "CANCELLED"
            assert incident_b.status == "TRIAGING"
