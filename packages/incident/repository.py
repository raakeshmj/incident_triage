"""Persistence functions for incident-core.

Every function here takes an open `Session` and does not commit -- the
caller (service.py) controls the transaction boundary, per Phase 1
requirement 5 ("no partial success"). Race handling uses Postgres
`ON CONFLICT DO NOTHING` against the exact unique indexes defined in the
Alembic migration, matching the mechanisms documented in
docs/architecture/06-database-design.md's "Concurrency mechanisms" table.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from packages.domain.commands import AlertReceivedCommand
from packages.domain.correlation_engine import CorrelationCandidate
from packages.domain.enums import CLOSED_INCIDENT_STATUSES, AlertSeverity, IncidentStatus
from packages.incident.db.models import (
    AlertRow,
    ConsumedEventRow,
    IncidentRow,
    OutboxEventRow,
    ProcessedCommandRow,
)

_CLOSED_STATUS_VALUES = tuple(status.value for status in CLOSED_INCIDENT_STATUSES)


# --- processed_commands -----------------------------------------------------


def get_processed_command(
    session: Session, command_type: str, idempotency_key: str
) -> ProcessedCommandRow | None:
    stmt = select(ProcessedCommandRow).where(
        ProcessedCommandRow.command_type == command_type,
        ProcessedCommandRow.idempotency_key == idempotency_key,
    )
    return session.execute(stmt).scalar_one_or_none()


def insert_processed_command(
    session: Session, command_type: str, idempotency_key: str, result: dict
) -> bool:
    """Returns True if this call actually inserted the row, False if a
    concurrent call already had (the idempotency-race case in service.py).

    Uses `RETURNING` rather than `CursorResult.rowcount` to detect the
    conflict -- `rowcount` is reported as -1 (unsupported) for this
    statement shape under the psycopg3 dialect, which would otherwise
    make every insert look like a lost race.
    """
    stmt = (
        pg_insert(ProcessedCommandRow)
        .values(command_type=command_type, idempotency_key=idempotency_key, result=result)
        .on_conflict_do_nothing(index_elements=["command_type", "idempotency_key"])
        .returning(ProcessedCommandRow.idempotency_key)
    )
    inserted = session.execute(stmt).scalar_one_or_none()
    return inserted is not None


# --- alerts ------------------------------------------------------------------


def insert_alert_or_get_existing(
    session: Session, *, command: AlertReceivedCommand, fingerprint: str
) -> tuple[AlertRow, bool]:
    """Insert a new Alert, or return the pre-existing one on a
    (source, external_id) dedup hit (defense in depth behind command-level
    idempotency -- see 06-database-design.md, "Alert deduplication and
    retries"). Returns (row, already_existed).
    """
    values = dict(
        external_id=command.external_id,
        source=command.source.value,
        fingerprint=fingerprint,
        labels=command.labels,
        annotations=command.annotations,
        severity=command.severity.value,
        status=command.status.value,
        raw_payload=command.raw_payload,
    )

    if command.external_id is not None:
        stmt = (
            pg_insert(AlertRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["source", "external_id"],
                index_where=text("external_id IS NOT NULL"),
            )
            .returning(AlertRow)
        )
        inserted = session.execute(stmt).scalars().one_or_none()
        if inserted is not None:
            return inserted, False

        existing = session.execute(
            select(AlertRow).where(
                AlertRow.source == command.source.value,
                AlertRow.external_id == command.external_id,
            )
        ).scalar_one()
        return existing, True

    stmt = pg_insert(AlertRow).values(**values).returning(AlertRow)
    inserted = session.execute(stmt).scalars().one()
    return inserted, False


def link_alert_to_incident(session: Session, alert_id: uuid.UUID, incident_id: uuid.UUID) -> None:
    alert = session.get(AlertRow, alert_id)
    assert alert is not None
    alert.incident_id = incident_id


# --- incidents -----------------------------------------------------------


def get_or_create_open_incident(
    session: Session,
    *,
    correlation_key: str,
    severity: AlertSeverity,
    service: str,
    environment: str,
) -> tuple[IncidentRow, bool]:
    """Find the open incident for this correlation_key, or create one.

    Returns (row, created). Race-safe against the partial unique index
    `incidents_open_correlation_key`: if two commands concurrently decide
    to create a new incident for the same correlation_key, the loser's
    INSERT is a no-op and it adopts the winner's row instead -- see
    docs/review/critical-review.md, "Race conditions".
    """
    existing = _select_open_incident(session, correlation_key)
    if existing is not None:
        return existing, False

    stmt = (
        pg_insert(IncidentRow)
        .values(
            status=IncidentStatus.TRIAGING.value,
            severity=severity.value,
            service=service,
            environment=environment,
            correlation_key=correlation_key,
            attempt_count=0,
            version=0,
        )
        .on_conflict_do_nothing(
            index_elements=["correlation_key"],
            index_where=text("status NOT IN ('CLOSED', 'CANCELLED', 'SUPPRESSED')"),
        )
        .returning(IncidentRow)
    )
    inserted = session.execute(stmt).scalars().one_or_none()
    if inserted is not None:
        return inserted, True

    # Lost the race: another concurrent command created the open incident
    # first. Adopt it rather than erroring.
    winner = _select_open_incident(session, correlation_key)
    assert winner is not None
    return winner, False


def _select_open_incident(session: Session, correlation_key: str) -> IncidentRow | None:
    stmt = select(IncidentRow).where(
        IncidentRow.correlation_key == correlation_key,
        IncidentRow.status.notin_(_CLOSED_STATUS_VALUES),
    )
    return session.execute(stmt).scalar_one_or_none()


def _advisory_lock_key(service: str, environment: str) -> int:
    digest = hashlib.sha256(f"{service}:{environment}".encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def acquire_correlation_lock(session: Session, *, service: str, environment: str) -> None:
    """Serializes "read candidate incidents, decide, write" for a given
    (service, environment) pair within the current transaction --
    released automatically at commit/rollback. Needed because multi-signal
    correlation scoring reads several rows before deciding what to write,
    which a unique index alone cannot make race-safe (two concurrent,
    differently-fingerprinted alerts that *should* merge could otherwise
    both see zero candidates and each create their own incident). See
    ADR-0015 and docs/review/critical-review.md, "Race conditions".
    """
    key = _advisory_lock_key(service, environment)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def find_open_incident_candidates(
    session: Session,
    *,
    service: str,
    environment: str,
    now: datetime,
    lookback_seconds: int,
) -> list[CorrelationCandidate]:
    """Open incidents for this (service, environment), each paired with its
    most recently received alert, bounded to incidents whose most recent
    alert arrived within `lookback_seconds` of `now`. That bound is a
    query-level cap, not just a scoring input -- an incident with no
    activity in, say, the last 15 minutes is not a plausible correlation
    target regardless of what the rules would otherwise say, and excluding
    it keeps the query cheap. See docs/architecture/04-incident-state-machine.md's
    Phase 2 addendum ("Case F: late-arriving alerts").
    """
    cutoff = now - timedelta(seconds=lookback_seconds)
    latest_alert = (
        select(
            AlertRow.incident_id.label("incident_id"),
            func.max(AlertRow.received_at).label("max_received_at"),
        )
        .group_by(AlertRow.incident_id)
        .subquery()
    )
    stmt = (
        select(IncidentRow, AlertRow)
        .join(latest_alert, IncidentRow.id == latest_alert.c.incident_id)
        .join(
            AlertRow,
            and_(
                AlertRow.incident_id == latest_alert.c.incident_id,
                AlertRow.received_at == latest_alert.c.max_received_at,
            ),
        )
        .where(
            IncidentRow.service == service,
            IncidentRow.environment == environment,
            IncidentRow.status.notin_(_CLOSED_STATUS_VALUES),
            latest_alert.c.max_received_at >= cutoff,
        )
    )
    return [
        CorrelationCandidate(
            incident_id=incident.id,
            correlation_key=incident.correlation_key,
            status=incident.status,
            service=incident.service,
            environment=incident.environment,
            most_recent_alert_received_at=alert.received_at,
            most_recent_alert_labels=alert.labels,
            most_recent_alert_fingerprint=alert.fingerprint,
        )
        for incident, alert in session.execute(stmt).all()
    ]


def touch_incident_updated_at(session: Session, incident: IncidentRow) -> None:
    incident.updated_at = datetime.now(UTC)


def get_incident_with_alerts(
    session: Session, incident_id: uuid.UUID
) -> tuple[IncidentRow, list[AlertRow]] | None:
    incident = session.get(IncidentRow, incident_id)
    if incident is None:
        return None
    alerts = (
        session.execute(select(AlertRow).where(AlertRow.incident_id == incident_id)).scalars().all()
    )
    return incident, list(alerts)


# --- outbox ----------------------------------------------------------------


def insert_outbox_event(
    session: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict,
    producer: str = "incident-core",
    correlation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
) -> OutboxEventRow:
    row = OutboxEventRow(
        event_id=uuid.uuid4(),
        event_type=event_type,
        schema_version=1,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        producer=producer,
        payload=payload,
        occurred_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def get_unpublished_outbox_events(session: Session, limit: int = 100) -> list[OutboxEventRow]:
    stmt = (
        select(OutboxEventRow)
        .where(OutboxEventRow.published_at.is_(None))
        .order_by(OutboxEventRow.sequence)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def mark_outbox_event_published(session: Session, event: OutboxEventRow) -> None:
    event.published_at = datetime.now(UTC)
    event.publish_attempts += 1
    event.last_publish_error = None


def mark_outbox_event_publish_failed(session: Session, event: OutboxEventRow, error: str) -> None:
    """Records a failed publish attempt without touching `published_at` --
    the event stays eligible for `get_unpublished_outbox_events` and will
    be retried on the next relay pass. Purely diagnostic bookkeeping; see
    OutboxEventRow's docstring.
    """
    event.publish_attempts += 1
    event.last_publish_error = error[:2000]


# --- consumed_events (consumer-side idempotency) ----------------------------


def has_consumed_event(session: Session, *, consumer_name: str, event_id: uuid.UUID) -> bool:
    stmt = select(ConsumedEventRow).where(
        ConsumedEventRow.consumer_name == consumer_name,
        ConsumedEventRow.event_id == event_id,
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def mark_event_consumed(session: Session, *, consumer_name: str, event_id: uuid.UUID) -> bool:
    """Returns True if this call recorded the (consumer, event) pair for
    the first time, False if it was already recorded (a duplicate
    delivery this consumer has already processed).
    """
    stmt = (
        pg_insert(ConsumedEventRow)
        .values(consumer_name=consumer_name, event_id=event_id)
        .on_conflict_do_nothing(index_elements=["consumer_name", "event_id"])
        .returning(ConsumedEventRow.event_id)
    )
    inserted = session.execute(stmt).scalar_one_or_none()
    return inserted is not None
