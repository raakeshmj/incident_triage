from __future__ import annotations

from sqlalchemy import select

from packages.incident.db.models import AlertRow, IncidentRow, OutboxEventRow, ProcessedCommandRow
from tests.factories import make_alert_command


def test_duplicate_command_returns_same_result_no_duplicate_incident(core, session_factory):
    command = make_alert_command(idempotency_key="same-key-123")

    first = core.handle_alert_received(command)
    second = core.handle_alert_received(command)

    assert first == second

    with session_factory() as session:
        alerts = session.execute(select(AlertRow)).scalars().all()
        incidents = session.execute(select(IncidentRow)).scalars().all()
        outbox_events = session.execute(select(OutboxEventRow)).scalars().all()
        ledger_rows = (
            session.execute(
                select(ProcessedCommandRow).where(
                    ProcessedCommandRow.idempotency_key == "same-key-123"
                )
            )
            .scalars()
            .all()
        )

    assert len(alerts) == 1
    assert len(incidents) == 1
    assert len(outbox_events) == 2  # AlertReceived + IncidentCreated, not duplicated
    assert len(ledger_rows) == 1


def test_different_idempotency_key_same_content_still_dedupes_via_correlation(core):
    """Two distinct commands (different idempotency keys) describing the
    same underlying alert condition should still land on one incident --
    idempotency and correlation are two different mechanisms and this
    test exercises them together.
    """
    first = core.handle_alert_received(make_alert_command(idempotency_key="key-a"))
    second = core.handle_alert_received(make_alert_command(idempotency_key="key-b"))

    assert first.incident_id == second.incident_id
    assert first.alert_id != second.alert_id
    assert second.incident_created is False
