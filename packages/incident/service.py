"""incident-core's command handler and query API.

This is the ONLY public entry point other components should call --
apps/api's alert-ingestion router depends on `IncidentCoreService` as an
abstract collaborator (see apps/api/dependencies.py) rather than importing
anything from packages/incident/db directly, which is what keeps
alert-ingestion free of database credentials per Phase 1 requirement 6.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from packages.domain.commands import COMMAND_TYPE_ALERT_RECEIVED, AlertReceivedCommand
from packages.domain.correlation import compute_fingerprint
from packages.domain.correlation_engine import (
    DEFAULT_CANDIDATE_LOOKBACK_SECONDS,
    CorrelationEngine,
    NewAlertContext,
    default_correlation_engine,
)
from packages.domain.events import (
    AGGREGATE_TYPE_ALERT,
    AGGREGATE_TYPE_INCIDENT,
    EVENT_TYPE_ALERT_CORRELATED,
    EVENT_TYPE_ALERT_RECEIVED,
    EVENT_TYPE_INCIDENT_CREATED,
    PRODUCER_INCIDENT_CORE,
    AlertCorrelatedPayload,
    AlertReceivedPayload,
    IncidentCreatedPayload,
)
from packages.domain.results import AlertReceivedResult
from packages.domain.views import AlertView, IncidentView
from packages.incident import repository
from packages.incident.db.models import IncidentRow
from packages.telemetry.logging import get_logger
from packages.telemetry.metrics import get_metrics

log = get_logger(__name__)
metrics = get_metrics()

_MAX_IDEMPOTENCY_RACE_RETRIES = 3


class _LostIdempotencyRace(Exception):
    """Internal control-flow signal: a concurrent identical command won."""


class IncidentCoreService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        correlation_engine: CorrelationEngine | None = None,
        candidate_lookback_seconds: int = DEFAULT_CANDIDATE_LOOKBACK_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._correlation_engine = correlation_engine or default_correlation_engine()
        self._candidate_lookback_seconds = candidate_lookback_seconds
        # Deliberately decoupled from `alerts.received_at` (DB-assigned at
        # insert time): correlation timing is a decision made by
        # incident-core, not a fact read back from storage, and tests need
        # to control it precisely to exercise Case F (late-arriving alerts)
        # without waiting in real time. In production this is just "now."
        self._clock = clock

    # --- commands ------------------------------------------------------

    def handle_alert_received(self, command: AlertReceivedCommand) -> AlertReceivedResult:
        with self._session_factory() as session:
            existing = repository.get_processed_command(
                session, COMMAND_TYPE_ALERT_RECEIVED, command.idempotency_key
            )
            if existing is not None:
                log.info(
                    "alert_received.idempotent_replay",
                    idempotency_key=command.idempotency_key,
                )
                return AlertReceivedResult.model_validate(existing.result)

            fingerprint = compute_fingerprint(command.source, command.labels)

            for _attempt in range(_MAX_IDEMPOTENCY_RACE_RETRIES):
                try:
                    result = self._process_alert_received(
                        session,
                        command=command,
                        fingerprint=fingerprint,
                    )
                    session.commit()
                    return result
                except _LostIdempotencyRace:
                    session.rollback()
                    winner = repository.get_processed_command(
                        session, COMMAND_TYPE_ALERT_RECEIVED, command.idempotency_key
                    )
                    if winner is not None:
                        log.info(
                            "alert_received.idempotency_race_lost",
                            idempotency_key=command.idempotency_key,
                        )
                        return AlertReceivedResult.model_validate(winner.result)
                    continue  # exceedingly unlikely: retry

            raise RuntimeError(
                f"failed to process AlertReceivedCommand {command.idempotency_key!r} "
                f"after {_MAX_IDEMPOTENCY_RACE_RETRIES} retries"
            )

    def _process_alert_received(
        self,
        session: Session,
        *,
        command: AlertReceivedCommand,
        fingerprint: str,
    ) -> AlertReceivedResult:
        # Serializes "read candidate incidents, decide, write" for this
        # (service, environment) pair -- see repository.acquire_correlation_lock
        # and ADR-0015. Must happen before we look at candidates, and before
        # the alert insert below, so two concurrent alerts for the same
        # service+environment are never mid-decision at the same time.
        repository.acquire_correlation_lock(
            session, service=command.service, environment=command.environment
        )

        alert_row, alert_already_existed = repository.insert_alert_or_get_existing(
            session, command=command, fingerprint=fingerprint
        )

        if alert_already_existed:
            # Defense-in-depth dedup: this exact (source, external_id) alert
            # was already recorded by an earlier, already-completed command.
            # No new incident, no new events -- just record this command's
            # idempotency entry so a retry of *this* key is also a no-op.
            assert alert_row.incident_id is not None, (
                "a previously-recorded alert must already be linked to an incident"
            )
            result = AlertReceivedResult(
                alert_id=alert_row.id,
                incident_id=alert_row.incident_id,
                incident_created=False,
            )
            self._finalize(session, command, result)
            return result

        now = self._clock()
        candidates = repository.find_open_incident_candidates(
            session,
            service=command.service,
            environment=command.environment,
            now=now,
            lookback_seconds=self._candidate_lookback_seconds,
        )
        decision = self._correlation_engine.decide(
            NewAlertContext(
                source=command.source.value,
                fingerprint=fingerprint,
                labels=command.labels,
                service=command.service,
                environment=command.environment,
                received_at=now,
            ),
            candidates,
        )
        metrics.increment(
            "correlation.decision", decision=decision.decision, service=command.service
        )
        metrics.observe(
            "correlation.score", decision.score, decision=decision.decision, service=command.service
        )

        if decision.decision == "CORRELATE":
            assert decision.matched_incident_id is not None
            incident_row = session.get(IncidentRow, decision.matched_incident_id)
            assert incident_row is not None
            incident_created = False
            repository.touch_incident_updated_at(session, incident_row)
        else:
            # Disambiguate the new incident's key from any *stale* open
            # incident that happens to share the same fingerprint but fell
            # outside the candidate lookback window (that's precisely why
            # the engine said NEW_INCIDENT instead of CORRELATE -- see
            # Case F in docs/architecture/04-incident-state-machine.md's
            # Phase 2 addendum). Without this, the partial unique index on
            # `correlation_key` would make this INSERT collide with the old
            # incident and we'd silently attach to it via the "lost the
            # race" fallback path -- which is correct behavior for a true
            # concurrent race (same bucket) and wrong for a stale match
            # (different bucket). Bucketing by the same window the
            # candidate query itself uses makes both cases fall out
            # correctly: two genuinely concurrent decisions for the same
            # signature share a bucket (and so still collide, as intended,
            # serialized by ON CONFLICT); a stale match does not.
            bucket = int(now.timestamp() // self._candidate_lookback_seconds)
            new_incident_correlation_key = f"{decision.correlation_key}:{bucket}"
            incident_row, incident_created = repository.get_or_create_open_incident(
                session,
                correlation_key=new_incident_correlation_key,
                severity=command.severity,
                service=command.service,
                environment=command.environment,
            )

        repository.link_alert_to_incident(session, alert_row.id, incident_row.id)

        repository.insert_outbox_event(
            session,
            event_type=EVENT_TYPE_ALERT_RECEIVED,
            aggregate_type=AGGREGATE_TYPE_ALERT,
            aggregate_id=alert_row.id,
            correlation_id=incident_row.id,
            producer=PRODUCER_INCIDENT_CORE,
            payload=AlertReceivedPayload(
                alert_id=alert_row.id,
                fingerprint=fingerprint,
                source=command.source,
                incident_id=incident_row.id,
            ).model_dump(mode="json"),
        )

        if incident_created:
            repository.insert_outbox_event(
                session,
                event_type=EVENT_TYPE_INCIDENT_CREATED,
                aggregate_type=AGGREGATE_TYPE_INCIDENT,
                aggregate_id=incident_row.id,
                correlation_id=incident_row.id,
                producer=PRODUCER_INCIDENT_CORE,
                payload=IncidentCreatedPayload(
                    incident_id=incident_row.id,
                    correlation_key=incident_row.correlation_key,
                    initial_severity=command.severity,
                    service=command.service,
                    environment=command.environment,
                    best_candidate_score=decision.score,
                    matched_signals=decision.matched_signals,
                ).model_dump(mode="json"),
            )
        else:
            repository.insert_outbox_event(
                session,
                event_type=EVENT_TYPE_ALERT_CORRELATED,
                aggregate_type=AGGREGATE_TYPE_INCIDENT,
                aggregate_id=incident_row.id,
                correlation_id=incident_row.id,
                producer=PRODUCER_INCIDENT_CORE,
                payload=AlertCorrelatedPayload(
                    alert_id=alert_row.id,
                    incident_id=incident_row.id,
                    score=decision.score,
                    matched_signals=decision.matched_signals,
                ).model_dump(mode="json"),
            )

        result = AlertReceivedResult(
            alert_id=alert_row.id,
            incident_id=incident_row.id,
            incident_created=incident_created,
        )
        self._finalize(session, command, result)

        log.info(
            "alert_received.processed",
            alert_id=str(alert_row.id),
            incident_id=str(incident_row.id),
            incident_created=incident_created,
            correlation_decision=decision.decision,
            correlation_score=decision.score,
            matched_signals=list(decision.matched_signals),
            idempotency_key=command.idempotency_key,
        )
        return result

    def _finalize(
        self, session: Session, command: AlertReceivedCommand, result: AlertReceivedResult
    ) -> None:
        inserted = repository.insert_processed_command(
            session,
            COMMAND_TYPE_ALERT_RECEIVED,
            command.idempotency_key,
            result.model_dump(mode="json"),
        )
        if not inserted:
            raise _LostIdempotencyRace()

    # --- queries ---------------------------------------------------------

    def get_incident_view(self, incident_id: uuid.UUID) -> IncidentView | None:
        with self._session_factory() as session:
            found = repository.get_incident_with_alerts(session, incident_id)
            if found is None:
                return None
            incident_row, alert_rows = found
            return IncidentView(
                id=incident_row.id,
                status=incident_row.status,
                severity=incident_row.severity,
                service=incident_row.service,
                environment=incident_row.environment,
                correlation_key=incident_row.correlation_key,
                attempt_count=incident_row.attempt_count,
                created_at=incident_row.created_at,
                updated_at=incident_row.updated_at,
                closed_at=incident_row.closed_at,
                alerts=[
                    AlertView(
                        id=a.id,
                        external_id=a.external_id,
                        source=a.source,
                        fingerprint=a.fingerprint,
                        labels=a.labels,
                        annotations=a.annotations,
                        severity=a.severity,
                        status=a.status,
                        received_at=a.received_at,
                    )
                    for a in alert_rows
                ],
            )
