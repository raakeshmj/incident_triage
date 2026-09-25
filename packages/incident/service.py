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

from packages.domain.commands import (
    COMMAND_TYPE_ALERT_RECEIVED,
    AlertReceivedCommand,
    RegisterEvidenceRefCommand,
)
from packages.domain.correlation import compute_fingerprint
from packages.domain.correlation_engine import (
    DEFAULT_CANDIDATE_LOOKBACK_SECONDS,
    CorrelationEngine,
    NewAlertContext,
    default_correlation_engine,
)
from packages.domain.enums import AlertStatus, IncidentStatus
from packages.domain.errors import (
    ConcurrentModificationError,
    EvidenceRefConflictError,
    IncidentNotFoundError,
)
from packages.domain.events import (
    AGGREGATE_TYPE_ALERT,
    AGGREGATE_TYPE_INCIDENT,
    EVENT_TYPE_ALERT_CORRELATED,
    EVENT_TYPE_ALERT_RECEIVED,
    EVENT_TYPE_ALERT_RESOLVED,
    EVENT_TYPE_EVIDENCE_REF_REGISTERED,
    EVENT_TYPE_INCIDENT_CREATED,
    EVENT_TYPE_INCIDENT_STATUS_CHANGED,
    PRODUCER_INCIDENT_CORE,
    AlertCorrelatedPayload,
    AlertReceivedPayload,
    AlertResolvedPayload,
    EvidenceRefRegisteredPayload,
    IncidentCreatedPayload,
    IncidentStatusChangedPayload,
)
from packages.domain.results import AlertReceivedResult, EvidenceRefRegisteredResult
from packages.domain.views import AlertView, EvidenceRefView, IncidentSummary, IncidentView
from packages.incident import repository
from packages.incident.db.models import AlertRow, IncidentRow
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

        if command.status is AlertStatus.RESOLVED:
            # Same lock as the firing path, deliberately: a resolution and a
            # concurrently-correlating firing alert for the same
            # service+environment are serialized, so the "no linked alert
            # still firing" guard below can never be evaluated against a
            # half-linked incident.
            return self._process_alert_resolved(session, command=command, fingerprint=fingerprint)

        alert_row, alert_already_existed = repository.insert_alert_or_get_existing(
            session, command=command, fingerprint=fingerprint
        )

        if alert_already_existed:
            # Defense-in-depth dedup: this exact (source, external_id) alert
            # was already recorded by an earlier, already-completed command.
            # No new incident, no new events -- just record this command's
            # idempotency entry so a retry of *this* key is also a no-op.
            # The existing row may be an already-*resolved* episode (a late,
            # out-of-order firing notification, possibly one whose firing
            # notification never arrived at all, so it's linked to no
            # incident): an ended episode never reopens or creates anything.
            if alert_row.status == AlertStatus.RESOLVED.value:
                log.info(
                    "alert_received.firing_after_resolution_ignored",
                    alert_id=str(alert_row.id),
                    idempotency_key=command.idempotency_key,
                )
            result = AlertReceivedResult(
                alert_id=alert_row.id,
                incident_id=alert_row.incident_id,
                incident_created=False,
                incident_status=self._incident_status(session, alert_row.incident_id),
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
            incident_status=incident_row.status,
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

    def _process_alert_resolved(
        self,
        session: Session,
        *,
        command: AlertReceivedCommand,
        fingerprint: str,
    ) -> AlertReceivedResult:
        """Deterministic resolution semantics
        (docs/architecture/04-incident-state-machine.md, "Alert resolution"):

        - the referenced alert's status flips firing -> resolved, once;
        - the incident transitions only via the state machine's own
          `TRIAGING -> CANCELLED` edge, and only when *no* linked alert is
          still firing -- one resolved alert never ends an incident another
          alert is still firing for;
        - a second resolution of an already-resolved alert changes nothing;
        - a resolution for an episode never seen firing is recorded,
          unlinked, and opens nothing.
        """
        now = self._clock()
        alert = repository.find_alert_to_resolve(session, command=command, fingerprint=fingerprint)

        if alert is None:
            alert = repository.insert_unlinked_resolved_alert(
                session, command=command, fingerprint=fingerprint, now=now
            )
            metrics.increment("alert_resolution.unmatched", service=command.service)
            log.info(
                "alert_resolved.unmatched",
                alert_id=str(alert.id),
                idempotency_key=command.idempotency_key,
            )
            return self._finish_resolution(session, command, alert, transitioned=False)

        if alert.status == AlertStatus.RESOLVED.value:
            metrics.increment("alert_resolution.duplicate", service=command.service)
            log.info("alert_resolved.already_resolved", alert_id=str(alert.id))
            return self._finish_resolution(session, command, alert, transitioned=False)

        repository.mark_alert_resolved(session, alert, now)
        session.flush()  # autoflush is off: the count below must see this row resolved

        assert alert.incident_id is not None, "a firing alert is always linked to an incident"
        incident = session.get(IncidentRow, alert.incident_id)
        assert incident is not None
        remaining = repository.count_firing_alerts(session, incident.id)

        repository.insert_outbox_event(
            session,
            event_type=EVENT_TYPE_ALERT_RESOLVED,
            aggregate_type=AGGREGATE_TYPE_ALERT,
            aggregate_id=alert.id,
            correlation_id=incident.id,
            producer=PRODUCER_INCIDENT_CORE,
            payload=AlertResolvedPayload(
                alert_id=alert.id, incident_id=incident.id, firing_alerts_remaining=remaining
            ).model_dump(mode="json"),
        )

        transitioned = False
        if remaining == 0 and incident.status == IncidentStatus.TRIAGING.value:
            from_status = incident.status
            if not repository.transition_incident_status(
                session,
                incident=incident,
                to_status=IncidentStatus.CANCELLED,
                now=now,
                closes=True,
            ):
                raise ConcurrentModificationError(
                    f"incident {incident.id} changed concurrently; retry the command"
                )
            repository.insert_outbox_event(
                session,
                event_type=EVENT_TYPE_INCIDENT_STATUS_CHANGED,
                aggregate_type=AGGREGATE_TYPE_INCIDENT,
                aggregate_id=incident.id,
                correlation_id=incident.id,
                producer=PRODUCER_INCIDENT_CORE,
                payload=IncidentStatusChangedPayload(
                    incident_id=incident.id,
                    from_status=from_status,
                    to_status=incident.status,
                    reason="all_linked_alerts_resolved",
                    version=incident.version,
                ).model_dump(mode="json"),
            )
            transitioned = True
        elif remaining == 0:
            # Every alert has stopped firing, but the incident is past
            # TRIAGING. The state machine has no alert-driven edge out of
            # those states -- verification, or a human, ends them. Recorded
            # and logged, deliberately not acted on.
            log.info(
                "alert_resolved.no_transition_from_state",
                incident_id=str(incident.id),
                status=incident.status,
            )

        metrics.increment(
            "alert_resolution.resolved",
            service=command.service,
            incident_transitioned=transitioned,
        )
        log.info(
            "alert_resolved.processed",
            alert_id=str(alert.id),
            incident_id=str(incident.id),
            firing_alerts_remaining=remaining,
            incident_status=incident.status,
            idempotency_key=command.idempotency_key,
        )
        return self._finish_resolution(session, command, alert, transitioned=transitioned)

    def _finish_resolution(
        self,
        session: Session,
        command: AlertReceivedCommand,
        alert: AlertRow,
        *,
        transitioned: bool,
    ) -> AlertReceivedResult:
        del transitioned  # carried in the outbox event, not the command result
        result = AlertReceivedResult(
            alert_id=alert.id,
            incident_id=alert.incident_id,
            incident_created=False,
            incident_status=self._incident_status(session, alert.incident_id),
        )
        self._finalize(session, command, result)
        return result

    @staticmethod
    def _incident_status(session: Session, incident_id: uuid.UUID | None) -> str | None:
        if incident_id is None:
            return None
        incident = session.get(IncidentRow, incident_id)
        return incident.status if incident is not None else None

    def register_evidence_ref(
        self, command: RegisterEvidenceRefCommand
    ) -> EvidenceRefRegisteredResult:
        """Record incident-core's reference to an evidence record
        evidence-service has already persisted. Insert-only and idempotent
        by `evidence_id` (the natural key, so no separate ledger row): the
        same registration twice is a no-op; the same id with different
        content or a different incident is refused -- an evidence id names
        exactly one immutable observation."""
        with self._session_factory() as session:
            if session.get(IncidentRow, command.incident_id) is None:
                raise IncidentNotFoundError(str(command.incident_id))

            inserted = repository.insert_evidence_ref(
                session,
                evidence_id=command.evidence_id,
                incident_id=command.incident_id,
                investigation_id=command.investigation_id,
                evidence_type=command.evidence_type,
                content_hash=command.content_hash,
                source_system=command.source_system,
                collected_at=command.collected_at,
            )
            if not inserted:
                existing = repository.get_evidence_ref(session, command.evidence_id)
                assert existing is not None
                if (
                    existing.content_hash != command.content_hash
                    or existing.incident_id != command.incident_id
                ):
                    raise EvidenceRefConflictError(str(command.evidence_id))
                return EvidenceRefRegisteredResult(
                    evidence_id=command.evidence_id,
                    incident_id=command.incident_id,
                    newly_registered=False,
                )

            repository.insert_outbox_event(
                session,
                event_type=EVENT_TYPE_EVIDENCE_REF_REGISTERED,
                aggregate_type=AGGREGATE_TYPE_INCIDENT,
                aggregate_id=command.incident_id,
                correlation_id=command.incident_id,
                producer=PRODUCER_INCIDENT_CORE,
                payload=EvidenceRefRegisteredPayload(
                    evidence_id=command.evidence_id,
                    incident_id=command.incident_id,
                    evidence_type=command.evidence_type,
                    source_system=command.source_system,
                    content_hash=command.content_hash,
                ).model_dump(mode="json"),
            )
            session.commit()
            return EvidenceRefRegisteredResult(
                evidence_id=command.evidence_id,
                incident_id=command.incident_id,
                newly_registered=True,
            )

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
                version=incident_row.version,
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
                        resolved_at=a.resolved_at,
                    )
                    for a in sorted(alert_rows, key=lambda a: (a.received_at, a.id))
                ],
            )

    def list_incident_summaries(
        self,
        *,
        environment: str | None = None,
        exclude_incident_id: uuid.UUID | None = None,
        created_before: datetime | None = None,
        limit: int = 200,
    ) -> list[IncidentSummary]:
        with self._session_factory() as session:
            rows = repository.list_incident_summaries(
                session,
                environment=environment,
                exclude_incident_id=exclude_incident_id,
                created_before=created_before,
                limit=limit,
            )
            return [
                IncidentSummary(
                    id=incident.id,
                    status=incident.status,
                    severity=incident.severity,
                    service=incident.service,
                    environment=incident.environment,
                    regions=tuple(
                        sorted({a.labels["region"] for a in alerts if "region" in a.labels})
                    ),
                    alert_types=tuple(
                        sorted({a.labels["alertname"] for a in alerts if "alertname" in a.labels})
                    ),
                    created_at=incident.created_at,
                    closed_at=incident.closed_at,
                )
                for incident, alerts in rows
            ]

    def list_evidence_refs(self, incident_id: uuid.UUID) -> list[EvidenceRefView]:
        with self._session_factory() as session:
            return [
                EvidenceRefView(
                    id=r.id,
                    incident_id=r.incident_id,
                    investigation_id=r.investigation_id,
                    evidence_type=r.evidence_type,
                    content_hash=r.content_hash,
                    source_system=r.source_system,
                    collected_at=r.collected_at,
                    registered_at=r.registered_at,
                )
                for r in repository.list_evidence_refs(session, incident_id)
            ]


def _label_values(alerts: list[AlertRow], key: str) -> tuple[str, ...]:
    return tuple(sorted({a.labels[key] for a in alerts if key in a.labels}))
