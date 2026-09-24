from __future__ import annotations

from sqlalchemy import select

from packages.domain.enums import AlertSource
from packages.incident.db.models import AlertRow, IncidentRow, OutboxEventRow
from tests.factories import make_alert_command


def test_duplicate_external_alert_deduplicated_at_database_level(core, session_factory):
    """Two commands with *different* idempotency keys but the same
    (source, external_id) -- simulating a retried webhook delivery that,
    for whatever reason, got a differently-derived idempotency key --
    must not create a second Alert row (06-database-design.md, "Alert
    deduplication and retries", layer 2).
    """
    first = core.handle_alert_received(
        make_alert_command(
            idempotency_key="delivery-1", source=AlertSource.PAGERDUTY, external_id="pd-999"
        )
    )
    second = core.handle_alert_received(
        make_alert_command(
            idempotency_key="delivery-2", source=AlertSource.PAGERDUTY, external_id="pd-999"
        )
    )

    assert second.alert_id == first.alert_id
    assert second.incident_id == first.incident_id
    assert second.incident_created is False

    with session_factory() as session:
        alerts = (
            session.execute(select(AlertRow).where(AlertRow.external_id == "pd-999"))
            .scalars()
            .all()
        )
        incidents = session.execute(select(IncidentRow)).scalars().all()
        outbox_events = session.execute(select(OutboxEventRow)).scalars().all()

    assert len(alerts) == 1
    assert len(incidents) == 1
    # Only the first (genuinely new) command produced events.
    assert len(outbox_events) == 2


def test_distinct_external_ids_are_not_deduplicated(core):
    first = core.handle_alert_received(
        make_alert_command(source=AlertSource.PAGERDUTY, external_id="pd-1")
    )
    second = core.handle_alert_received(
        make_alert_command(source=AlertSource.PAGERDUTY, external_id="pd-2")
    )

    assert first.alert_id != second.alert_id
    # Same labels -> same fingerprint -> same incident, but two distinct alerts.
    assert first.incident_id == second.incident_id
