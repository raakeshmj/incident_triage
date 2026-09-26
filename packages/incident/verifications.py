"""incident-core's verification commands (Phase 8): the only writer of
verifications, their observations and evidence links -- and of the
incident transitions that close the loop.

    (RemediationCoreService.complete_execution, same transaction)
        remediation EXECUTED + incident VERIFYING + verification PENDING
    start          PENDING -> RUNNING (grace, deadline, first poll time)
    claim_due      a fenced, leased ticket for one poll (claim_attempt++)
    record_observation   one poll's sample -> checks -> streak -> decide
    finalize       PASSED    -> incident RESOLVED          (IncidentResolved)
                   FAILED    -> incident VERIFICATION_FAILED, then
                                  attempts left + alert firing -> stays for re-investigation
                                  otherwise                    -> ESCALATED
                   TIMED_OUT -> incident ESCALATED (evidence never became conclusive)

Stale results can't close anything: every write is fenced by lease owner
and claim attempt, and a verdict only moves the incident if it is still
VERIFYING for *this* remediation (the remediation's current verification,
and no newer remediation exists). Otherwise the verdict is recorded and the
incident left alone.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.domain.enums import IncidentStatus
from packages.domain.errors import (
    ConcurrentModificationError,
    LeaseLostError,
    RemediationNotFoundError,
)
from packages.domain.events import (
    AGGREGATE_TYPE_INCIDENT,
    AGGREGATE_TYPE_VERIFICATION,
    EVENT_TYPE_INCIDENT_ESCALATED,
    EVENT_TYPE_INCIDENT_RESOLVED,
    EVENT_TYPE_INCIDENT_STATUS_CHANGED,
    EVENT_TYPE_VERIFICATION_COMPLETED,
    EVENT_TYPE_VERIFICATION_STARTED,
    PRODUCER_INCIDENT_CORE,
    IncidentClosureEventPayload,
    IncidentStatusChangedPayload,
    VerificationEventPayload,
)
from packages.domain.remediation import RemediationStatus
from packages.domain.verification import (
    TERMINAL_VERIFICATION_STATUSES,
    CheckResult,
    Decision,
    ObservationResult,
    Sample,
    VerificationSpec,
    VerificationStatus,
    VerificationView,
    decide,
    evaluate_sample,
)
from packages.incident import repository
from packages.incident.db.models import (
    AlertRow,
    IncidentRow,
    RemediationRow,
    VerificationEvidenceRow,
    VerificationObservationRow,
    VerificationRow,
)
from packages.telemetry.logging import get_logger
from packages.telemetry.metrics import get_metrics

log = get_logger(__name__)
metrics = get_metrics()


@dataclass(frozen=True)
class VerificationTicket:
    verification_id: uuid.UUID
    incident_id: uuid.UUID
    remediation_id: uuid.UUID
    spec: VerificationSpec
    owner: str
    claim_attempt: int


class VerificationCoreService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        max_investigation_attempts: int = 2,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_investigation_attempts
        self._clock = clock

    # --- lifecycle --------------------------------------------------------------

    def start(self, verification_id: uuid.UUID, *, actor: str) -> VerificationView:
        """PENDING -> RUNNING. Idempotent: a duplicate VerificationRequested
        (or a second worker) finds it already running or finished."""
        with self._session_factory() as session:
            row = self._lock(session, verification_id)
            if row.status == VerificationStatus.PENDING.value:
                self._start(session, row, actor)
                session.commit()
            return self._view(row)

    def _start(self, session: Session, row: VerificationRow, actor: str) -> None:
        spec = VerificationSpec.model_validate(row.spec)
        now = self._clock()
        row.status = VerificationStatus.RUNNING.value
        row.started_at = now
        row.known_alert_ids = [
            str(a)
            for a in session.execute(
                select(AlertRow.id).where(AlertRow.incident_id == row.incident_id)
            ).scalars()
        ]
        row.grace_until = now + timedelta(seconds=spec.grace_seconds)
        row.deadline_at = now + timedelta(seconds=spec.timeout_seconds)
        row.next_poll_at = row.grace_until
        row.updated_at = now
        self._event(session, row, EVENT_TYPE_VERIFICATION_STARTED, actor=actor)
        metrics.increment("verification.started", action=row.verification_type)
        log.info(
            "verification.started",
            verification_id=str(row.id),
            incident_id=str(row.incident_id),
            action=row.verification_type,
            grace_seconds=spec.grace_seconds,
            timeout_seconds=spec.timeout_seconds,
        )

    def claim_due(
        self, verification_id: uuid.UUID, *, owner: str, lease_seconds: float = 60
    ) -> VerificationTicket | None:
        """A ticket for one poll, if one is due and nobody else holds it.
        Past the deadline, the verdict is reached here without polling."""
        with self._session_factory() as session:
            row = self._lock(session, verification_id)
            if row.status == VerificationStatus.PENDING.value:
                self._start(session, row, f"worker:{owner}")
            if row.status != VerificationStatus.RUNNING.value:
                session.commit()
                return None
            now = self._clock()
            if (
                row.lease_owner is not None
                and row.lease_expires_at is not None
                and row.lease_expires_at > now
            ):
                session.commit()
                return None
            spec = VerificationSpec.model_validate(row.spec)
            if row.deadline_at is not None and now >= row.deadline_at:
                self._finalize(
                    session,
                    row,
                    decide(
                        spec,
                        latest=self._latest_result(session, row),
                        consecutive_successes=row.consecutive_successes,
                        conclusive_observations=row.conclusive_count,
                        now_seconds=now.timestamp(),
                        deadline_seconds=row.deadline_at.timestamp(),
                    ),
                    actor=f"worker:{owner}",
                )
                session.commit()
                return None
            if row.next_poll_at is not None and now < row.next_poll_at:
                session.commit()
                return None
            row.claim_attempt += 1
            row.lease_owner = owner
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            session.commit()
            return VerificationTicket(
                verification_id=row.id,
                incident_id=row.incident_id,
                remediation_id=row.remediation_id,
                spec=spec,
                owner=owner,
                claim_attempt=row.claim_attempt,
            )

    def record_observation(
        self,
        ticket: VerificationTicket,
        *,
        sample: Sample,
        evidence_ids: list[uuid.UUID],
        observed_at: datetime,
    ) -> VerificationView:
        """Evaluate one poll and decide. Fenced: a worker that lost its lease
        (or was superseded by a later claim) can't record anything."""
        with self._session_factory() as session:
            row = self._lock(session, ticket.verification_id)
            if (
                row.status != VerificationStatus.RUNNING.value
                or row.lease_owner != ticket.owner
                or row.claim_attempt != ticket.claim_attempt
            ):
                raise LeaseLostError(
                    f"{ticket.owner} (claim {ticket.claim_attempt}) no longer holds "
                    f"verification {row.id}"
                )
            new_alerts = self._new_firing_alerts(session, row)
            sample = sample.model_copy(update={"new_firing_alerts": new_alerts})
            result = evaluate_sample(ticket.spec, sample)
            sequence = row.observation_count + 1
            row.observation_count = sequence
            row.conclusive_count += 1 if result.conclusive else 0
            row.consecutive_successes = row.consecutive_successes + 1 if result.passed else 0
            session.add(
                VerificationObservationRow(
                    verification_id=row.id,
                    sequence=sequence,
                    claim_attempt=ticket.claim_attempt,
                    observed_at=observed_at,
                    passed=result.passed,
                    conclusive=result.conclusive,
                    checks=[c.model_dump(mode="json") for c in result.checks],
                    errors=list(sample.errors),
                    evidence_ids=[str(e) for e in evidence_ids],
                )
            )
            for evidence_id in evidence_ids:
                session.add(
                    VerificationEvidenceRow(
                        verification_id=row.id,
                        evidence_id=evidence_id,
                        poll_sequence=sequence,
                        role="observation",
                        collected_at=observed_at,
                    )
                )
            now = self._clock()
            row.next_poll_at = now + timedelta(seconds=ticket.spec.poll_interval_seconds)
            row.lease_owner = row.lease_expires_at = None
            row.updated_at = now
            session.flush()
            decision = decide(
                ticket.spec,
                latest=result,
                consecutive_successes=row.consecutive_successes,
                conclusive_observations=row.conclusive_count,
                now_seconds=now.timestamp(),
                deadline_seconds=(row.deadline_at or now).timestamp(),
            )
            if decision.status != VerificationStatus.RUNNING:
                self._finalize(session, row, decision, actor=f"worker:{ticket.owner}")
            session.commit()
            metrics.increment(
                "verification.observation",
                action=row.verification_type,
                passed=result.passed,
                conclusive=result.conclusive,
            )
            return self._view(row)

    def _finalize(
        self, session: Session, row: VerificationRow, decision: Decision, *, actor: str
    ) -> None:
        now = self._clock()
        row.status = decision.status.value
        row.result = decision.status.value
        row.failure_reason = (decision.reason or "")[:2000] or None
        row.completed_at = now
        row.lease_owner = row.lease_expires_at = None
        row.next_poll_at = None
        row.updated_at = now

        incident = session.get(IncidentRow, row.incident_id, with_for_update=True)
        remediation = session.get(RemediationRow, row.remediation_id)
        assert incident is not None and remediation is not None
        stale = self._staleness(session, row, incident, remediation)
        if stale:
            row.next_action = "none"
            row.failure_reason = ((row.failure_reason or "") + f" [not applied: {stale}]").strip()
        elif decision.status == VerificationStatus.PASSED:
            row.next_action = "resolve"
            self._move(session, incident, IncidentStatus.RESOLVED, "verification_passed")
            self._closure(
                session, incident, EVENT_TYPE_INCIDENT_RESOLVED, "verification passed", row
            )
        elif decision.status == VerificationStatus.TIMED_OUT:
            row.next_action = "escalate"
            self._move(session, incident, IncidentStatus.ESCALATED, "verification_timed_out")
            self._closure(
                session, incident, EVENT_TYPE_INCIDENT_ESCALATED, "verification timed out", row
            )
        else:
            self._move(session, incident, IncidentStatus.VERIFICATION_FAILED, "verification_failed")
            firing = repository.count_firing_alerts(session, incident.id)
            if incident.attempt_count < self._max_attempts and firing > 0:
                row.next_action = "reinvestigate"  # the investigation scheduler picks it up
            else:
                row.next_action = "escalate"
                why = (
                    f"investigation attempts exhausted "
                    f"({incident.attempt_count}/{self._max_attempts})"
                    if firing
                    else "no alert still firing to re-investigate"
                )
                self._move(session, incident, IncidentStatus.ESCALATED, "verification_failed")
                self._closure(
                    session,
                    incident,
                    EVENT_TYPE_INCIDENT_ESCALATED,
                    f"verification failed; {why}",
                    row,
                )
        self._event(session, row, EVENT_TYPE_VERIFICATION_COMPLETED, actor=actor)
        metrics.increment("verification.completed", result=row.result, action=row.verification_type)
        log.info(
            "verification.completed",
            verification_id=str(row.id),
            incident_id=str(row.incident_id),
            result=row.result,
            next_action=row.next_action,
            reason=row.failure_reason,
        )

    def _staleness(
        self,
        session: Session,
        row: VerificationRow,
        incident: IncidentRow,
        remediation: RemediationRow,
    ) -> str | None:
        if incident.status != IncidentStatus.VERIFYING.value:
            return f"incident is {incident.status}, not VERIFYING"
        if remediation.status != RemediationStatus.EXECUTED.value:
            return f"remediation is {remediation.status}"
        if remediation.verification_ref != row.id:
            return "a newer verification owns this remediation"
        newer = session.execute(
            select(func.count())
            .select_from(RemediationRow)
            .where(
                RemediationRow.incident_id == incident.id,
                RemediationRow.created_at > remediation.created_at,
            )
        ).scalar_one()
        if newer:
            return "a newer remediation exists for the incident"
        return None

    # --- scheduling -------------------------------------------------------------------

    def due(self, limit: int = 50) -> list[uuid.UUID]:
        """PENDING (a lost VerificationRequested), or RUNNING with a poll or
        the deadline due and no live lease -- crash recovery included."""
        now = self._clock()
        with self._session_factory() as session:
            rows = session.execute(
                select(VerificationRow.id)
                .where(
                    (VerificationRow.status == VerificationStatus.PENDING.value)
                    | (
                        (VerificationRow.status == VerificationStatus.RUNNING.value)
                        & (
                            (VerificationRow.next_poll_at <= now)
                            | (VerificationRow.deadline_at <= now)
                        )
                        & (
                            VerificationRow.lease_expires_at.is_(None)
                            | (VerificationRow.lease_expires_at < now)
                        )
                    )
                )
                .order_by(VerificationRow.next_poll_at.nulls_first())
                .limit(limit)
            ).scalars()
            return list(rows)

    # --- reads -------------------------------------------------------------------

    def get(self, verification_id: uuid.UUID) -> VerificationView:
        with self._session_factory() as session:
            row = session.get(VerificationRow, verification_id)
            if row is None:
                raise RemediationNotFoundError(f"verification {verification_id}")
            return self._view(row)

    def for_incident(self, incident_id: uuid.UUID) -> list[VerificationView]:
        with self._session_factory() as session:
            rows = session.execute(
                select(VerificationRow)
                .where(VerificationRow.incident_id == incident_id)
                .order_by(VerificationRow.created_at)
            ).scalars()
            return [self._view(r) for r in rows]

    def observations(self, verification_id: uuid.UUID) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.execute(
                select(VerificationObservationRow)
                .where(VerificationObservationRow.verification_id == verification_id)
                .order_by(VerificationObservationRow.sequence)
            ).scalars()
            return [
                {
                    "sequence": r.sequence,
                    "claim_attempt": r.claim_attempt,
                    "observed_at": r.observed_at.isoformat(),
                    "passed": r.passed,
                    "conclusive": r.conclusive,
                    "checks": r.checks,
                    "errors": r.errors,
                    "evidence_ids": r.evidence_ids,
                }
                for r in rows
            ]

    def evidence_links(self, verification_id: uuid.UUID) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.execute(
                select(VerificationEvidenceRow)
                .where(VerificationEvidenceRow.verification_id == verification_id)
                .order_by(
                    VerificationEvidenceRow.poll_sequence, VerificationEvidenceRow.collected_at
                )
            ).scalars()
            return [
                {
                    "evidence_id": str(r.evidence_id),
                    "poll_sequence": r.poll_sequence,
                    "role": r.role,
                    "collected_at": r.collected_at.isoformat(),
                }
                for r in rows
            ]

    # --- internals -------------------------------------------------------------------

    @staticmethod
    def _latest_result(session: Session, row: VerificationRow) -> ObservationResult | None:
        """The last persisted observation, for a verdict reached at the
        deadline (its failing checks explain the failure). Never definitive:
        a definitive failure would already have ended the verification."""
        last = session.execute(
            select(VerificationObservationRow)
            .where(VerificationObservationRow.verification_id == row.id)
            .order_by(VerificationObservationRow.sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last is None:
            return None
        return ObservationResult(
            passed=last.passed,
            conclusive=last.conclusive,
            definitive_failure=None,
            checks=[CheckResult.model_validate(c) for c in last.checks],
        )

    @staticmethod
    def _new_firing_alerts(session: Session, row: VerificationRow) -> int:
        """Alerts linked to the incident since this verification began (not
        among those known at start) and still firing: a new problem, whatever
        the metrics say about the old one. Clock-free on purpose."""
        known = {uuid.UUID(a) for a in row.known_alert_ids or []}
        firing = session.execute(
            select(AlertRow.id).where(
                AlertRow.incident_id == row.incident_id, AlertRow.status == "firing"
            )
        ).scalars()
        return sum(1 for alert_id in firing if alert_id not in known)

    def _move(
        self, session: Session, incident: IncidentRow, to_status: IncidentStatus, reason: str
    ) -> None:
        from_status = incident.status
        if not repository.transition_incident_status(
            session, incident=incident, to_status=to_status, now=self._clock(), closes=False
        ):
            raise ConcurrentModificationError(f"incident {incident.id} changed concurrently")
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
                reason=reason,
                version=incident.version,
            ).model_dump(mode="json"),
        )

    @staticmethod
    def _closure(
        session: Session,
        incident: IncidentRow,
        event_type: str,
        reason: str,
        row: VerificationRow,
    ) -> None:
        repository.insert_outbox_event(
            session,
            event_type=event_type,
            aggregate_type=AGGREGATE_TYPE_INCIDENT,
            aggregate_id=incident.id,
            correlation_id=incident.id,
            producer=PRODUCER_INCIDENT_CORE,
            payload=IncidentClosureEventPayload(
                incident_id=incident.id,
                status=incident.status,
                reason=reason,
                verification_id=row.id,
                remediation_id=row.remediation_id,
            ).model_dump(mode="json"),
        )

    @staticmethod
    def _event(session: Session, row: VerificationRow, event_type: str, *, actor: str) -> None:
        repository.insert_outbox_event(
            session,
            event_type=event_type,
            aggregate_type=AGGREGATE_TYPE_VERIFICATION,
            aggregate_id=row.id,
            correlation_id=row.correlation_id,
            producer=PRODUCER_INCIDENT_CORE,
            payload=VerificationEventPayload(
                verification_id=row.id,
                incident_id=row.incident_id,
                remediation_id=row.remediation_id,
                status=row.status,
                policy_version=row.policy_version,
                claim_attempt=row.claim_attempt,
                result=row.result,
                reason=row.failure_reason,
                next_action=row.next_action,
            ).model_dump(mode="json"),
        )
        del actor

    @staticmethod
    def _lock(session: Session, verification_id: uuid.UUID) -> VerificationRow:
        row = session.get(VerificationRow, verification_id, with_for_update=True)
        if row is None:
            raise RemediationNotFoundError(f"verification {verification_id}")
        return row

    @staticmethod
    def _view(row: VerificationRow) -> VerificationView:
        return VerificationView(
            id=row.id,
            incident_id=row.incident_id,
            remediation_id=row.remediation_id,
            verification_type=row.verification_type,
            policy_version=row.policy_version,
            spec=row.spec,
            baseline=row.baseline,
            status=VerificationStatus(row.status),
            consecutive_successes=row.consecutive_successes,
            observation_count=row.observation_count,
            result=row.result,
            failure_reason=row.failure_reason,
            next_action=row.next_action,
            correlation_id=row.correlation_id,
            grace_until=row.grace_until,
            deadline_at=row.deadline_at,
            next_poll_at=row.next_poll_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


__all__ = ["TERMINAL_VERIFICATION_STATUSES", "VerificationCoreService", "VerificationTicket"]
