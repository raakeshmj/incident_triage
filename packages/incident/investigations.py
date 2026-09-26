"""incident-core's investigation commands and queries (Phase 5, ADR-0020).

incident-core stays the sole writer of investigation state. The
investigation worker holds no rule of its own about what may be persisted:
it proposes (a step, a hypothesis update, a conclusion) and this module
validates and writes -- including the deterministic stopping criteria and
evidence-citation checks, which are re-evaluated here no matter what the
worker already checked.

Concurrency:
- Starting an investigation is idempotent: unique `(incident_id,
  attempt_number)` plus a row lock on the incident.
- Every worker write is *fenced* by a lease: it must present the owner
  name that claimed the investigation, and the lease must not have been
  taken over. A worker that lost its lease gets `LeaseLostError` and stops.
- Incident transitions are optimistic (`version`), per
  docs/architecture/04-incident-state-machine.md.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from packages.domain.enums import IncidentStatus
from packages.domain.errors import (
    ConcurrentModificationError,
    IncidentNotFoundError,
    InvalidIncidentTransitionError,
    InvestigationNotFoundError,
    LeaseLostError,
)
from packages.domain.events import (
    AGGREGATE_TYPE_INCIDENT,
    AGGREGATE_TYPE_INVESTIGATION,
    EVENT_TYPE_INCIDENT_STATUS_CHANGED,
    EVENT_TYPE_INVESTIGATION_COMPLETED,
    EVENT_TYPE_INVESTIGATION_FAILED,
    EVENT_TYPE_INVESTIGATION_STARTED,
    PRODUCER_INCIDENT_CORE,
    IncidentStatusChangedPayload,
    InvestigationCompletedPayload,
    InvestigationFailedPayload,
    InvestigationStartedPayload,
)
from packages.domain.investigation import (
    TERMINAL_INVESTIGATION_STATUSES,
    ConclusionOutcome,
    FinalInvestigationResult,
    HypothesisSnapshot,
    HypothesisStatus,
    HypothesisUpdateBatch,
    HypothesisUpdateOutcome,
    InvestigationBudget,
    InvestigationState,
    InvestigationStatus,
    InvestigationView,
    StartedInvestigation,
    StepKind,
    StepView,
    StoppingCriteria,
    check_hypothesis_update,
    evaluate_conclusion,
    ungrounded_ids,
)
from packages.incident import repository
from packages.incident.db.models import (
    EvidenceRefRow,
    HypothesisEvidenceLinkRow,
    HypothesisRow,
    IncidentRow,
    InvestigationRow,
    InvestigationStepRow,
    OutboxEventRow,
    RcaReportRow,
)
from packages.telemetry.logging import get_logger
from packages.telemetry.metrics import get_metrics

log = get_logger(__name__)
metrics = get_metrics()

_OPEN_INVESTIGATION = (InvestigationStatus.CREATED.value, InvestigationStatus.INVESTIGATING.value)


class InvestigationCoreService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        criteria: StoppingCriteria | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._criteria = criteria or StoppingCriteria()
        self._clock = clock

    # --- starting ---------------------------------------------------------

    def request_investigation(
        self,
        incident_id: uuid.UUID,
        *,
        model_provider: str,
        model_name: str,
        model_settings: dict[str, Any],
        budget: InvestigationBudget,
    ) -> StartedInvestigation:
        """TRIAGING (or VERIFICATION_FAILED: re-investigation after a failed
        verification, Phase 8) -> INVESTIGATING and create the Investigation,
        atomically.

        Idempotent: if the incident is already INVESTIGATING with an open
        investigation, that one is returned (`created=False`). The state
        machine's guard -- at least one linked alert still firing -- is
        enforced here.
        """
        with self._session_factory() as session:
            incident = session.execute(
                select(IncidentRow).where(IncidentRow.id == incident_id).with_for_update()
            ).scalar_one_or_none()
            if incident is None:
                raise IncidentNotFoundError(str(incident_id))

            existing = session.execute(
                select(InvestigationRow).where(
                    InvestigationRow.incident_id == incident_id,
                    InvestigationRow.status.in_(_OPEN_INVESTIGATION),
                )
            ).scalar_one_or_none()
            if existing is not None:
                return StartedInvestigation(
                    investigation_id=existing.id, incident_id=incident_id, created=False
                )
            if incident.status not in (
                IncidentStatus.TRIAGING.value,
                IncidentStatus.VERIFICATION_FAILED.value,
            ):
                raise InvalidIncidentTransitionError(
                    f"incident {incident_id} is {incident.status}, not TRIAGING or "
                    "VERIFICATION_FAILED"
                )
            from_status = incident.status
            if repository.count_firing_alerts(session, incident_id) == 0:
                raise InvalidIncidentTransitionError(
                    f"incident {incident_id} has no firing alert; nothing to investigate"
                )

            now = self._clock()
            attempt = incident.attempt_count + 1
            incident.attempt_count = attempt
            session.flush()
            if not repository.transition_incident_status(
                session,
                incident=incident,
                to_status=IncidentStatus.INVESTIGATING,
                now=now,
                closes=False,
            ):
                raise ConcurrentModificationError(f"incident {incident_id} changed concurrently")
            investigation = InvestigationRow(
                id=uuid.uuid4(),
                incident_id=incident_id,
                attempt_number=attempt,
                status=InvestigationStatus.CREATED.value,
                model_provider=model_provider,
                model_name=model_name,
                model_config=model_settings,
                budget=budget.model_dump(mode="json"),
                iteration_count=0,
                tool_call_count=0,
                evidence_count=0,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                created_at=now,
                updated_at=now,
            )
            session.add(investigation)
            session.flush()
            self._status_event(
                session,
                incident,
                from_status,
                "investigation_started"
                if from_status == IncidentStatus.TRIAGING.value
                else "reinvestigation_after_failed_verification",
            )
            repository.insert_outbox_event(
                session,
                event_type=EVENT_TYPE_INVESTIGATION_STARTED,
                aggregate_type=AGGREGATE_TYPE_INVESTIGATION,
                aggregate_id=investigation.id,
                correlation_id=incident_id,
                producer=PRODUCER_INCIDENT_CORE,
                payload=InvestigationStartedPayload(
                    investigation_id=investigation.id,
                    incident_id=incident_id,
                    attempt_number=attempt,
                    model_provider=model_provider,
                    model_name=model_name,
                ).model_dump(mode="json"),
            )
            session.commit()
            metrics.increment("investigation.started", model=model_name)
            log.info(
                "investigation.started",
                investigation_id=str(investigation.id),
                incident_id=str(incident_id),
                attempt=attempt,
                model=model_name,
            )
            return StartedInvestigation(
                investigation_id=investigation.id, incident_id=incident_id, created=True
            )

    def due_for_investigation(self, *, debounce_seconds: int, limit: int = 10) -> list[uuid.UUID]:
        """TRIAGING incidents whose debounce window has elapsed, and
        VERIFICATION_FAILED incidents the verification left for
        re-investigation -- each with a firing alert (04-incident-state-machine.md)."""
        cutoff = self._clock() - timedelta(seconds=debounce_seconds)
        with self._session_factory() as session:
            result = session.execute(
                text(
                    """
                    SELECT i.id FROM incident_core.incidents i
                    WHERE ((i.status = 'TRIAGING' AND i.created_at <= :cutoff)
                           OR i.status = 'VERIFICATION_FAILED')
                      AND EXISTS (SELECT 1 FROM incident_core.alerts a
                                  WHERE a.incident_id = i.id AND a.status = 'firing')
                    ORDER BY i.created_at LIMIT :limit
                    """
                ),
                {"cutoff": cutoff, "limit": limit},
            )
            incident_ids: list[uuid.UUID] = list(result.scalars())
            return incident_ids

    # --- claiming / leases --------------------------------------------------

    def claim(
        self, investigation_id: uuid.UUID, *, owner: str, lease_seconds: int
    ) -> InvestigationState | None:
        """Take (or resume) ownership. None if the investigation is finished
        or another live worker holds it -- duplicate deliveries land here."""
        now = self._clock()
        with self._session_factory() as session:
            row = self._lock(session, investigation_id)
            if InvestigationStatus(row.status) in TERMINAL_INVESTIGATION_STATUSES:
                return None
            live = row.lease_expires_at is not None and row.lease_expires_at > now
            if live and row.lease_owner != owner:
                return None
            resumed = row.status == InvestigationStatus.INVESTIGATING.value
            row.status = InvestigationStatus.INVESTIGATING.value
            row.lease_owner = owner
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.started_at = row.started_at or now
            row.updated_at = now
            session.commit()
            metrics.increment("investigation.claimed", resumed=resumed)
            log.info(
                "investigation.claimed",
                investigation_id=str(investigation_id),
                owner=owner,
                resumed=resumed,
            )
        return self.load_state(investigation_id)

    def resumable(self, *, limit: int = 10) -> list[uuid.UUID]:
        """Open investigations nobody holds a live lease on (a crashed or
        never-started worker). The liveness guarantee behind event delivery."""
        now = self._clock()
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(InvestigationRow.id)
                    .where(
                        InvestigationRow.status.in_(_OPEN_INVESTIGATION),
                        (InvestigationRow.lease_expires_at.is_(None))
                        | (InvestigationRow.lease_expires_at <= now),
                    )
                    .order_by(InvestigationRow.created_at)
                    .limit(limit)
                ).scalars()
            )

    def release(self, investigation_id: uuid.UUID, *, owner: str) -> None:
        with self._session_factory() as session:
            row = self._lock(session, investigation_id)
            if row.lease_owner == owner:
                row.lease_expires_at = self._clock()
                session.commit()

    # --- the trace ---------------------------------------------------------

    def record_step(
        self,
        investigation_id: uuid.UUID,
        *,
        owner: str,
        kind: StepKind,
        iteration: int,
        payload: dict[str, Any],
        call_id: str | None = None,
        latency_ms: int | None = None,
        usage: dict[str, int] | None = None,
        tool_call: bool = False,
        new_evidence: int = 0,
        last_action: str | None = None,
        lease_seconds: int = 120,
    ) -> int:
        with self._session_factory() as session:
            row = self._fenced(session, investigation_id, owner)
            sequence = self._append_step(
                session, row, kind, iteration, payload, call_id, latency_ms
            )
            if kind == StepKind.MODEL_TURN:
                row.iteration_count = max(row.iteration_count, iteration)
            if usage:
                row.input_tokens += usage.get("input_tokens", 0)
                row.output_tokens += usage.get("output_tokens", 0)
                row.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                row.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
            if tool_call:
                row.tool_call_count += 1
            row.evidence_count += new_evidence
            if last_action:
                row.last_action = last_action[:200]
            now = self._clock()
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            session.commit()
            return sequence

    # --- hypotheses ----------------------------------------------------------

    def apply_hypothesis_updates(
        self,
        investigation_id: uuid.UUID,
        *,
        owner: str,
        iteration: int,
        call_id: str,
        batch: HypothesisUpdateBatch,
    ) -> HypothesisUpdateOutcome:
        """Validate each update against what this investigation has actually
        been shown, apply the valid ones, quarantine the rest, and record the
        whole outcome as one trace step -- in a single transaction."""
        with self._session_factory() as session:
            row = self._fenced(session, investigation_id, owner)
            accessible = self._accessible_evidence(session, row)
            hypotheses = {h.key: h for h in self._hypothesis_rows(session, investigation_id)}
            snapshots = self._snapshots(session, list(hypotheses.values()))
            now = self._clock()
            applied: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []

            for update in batch.updates:
                problems = check_hypothesis_update(update, snapshots.get(update.key), accessible)
                if problems:
                    rejected.append({"key": update.key, "problems": problems})
                    metrics.increment("investigation.hypothesis_update_rejected")
                    continue
                existing = hypotheses.get(update.key)
                if existing is None:
                    existing = HypothesisRow(
                        id=uuid.uuid4(),
                        investigation_id=investigation_id,
                        key=update.key,
                        description=update.description or "",
                        status=update.status or HypothesisStatus.ACTIVE.value,
                        confidence=_decimal(update.confidence),
                        missing_evidence=update.missing_evidence or [],
                        cause_category=update.cause_category,
                        component=update.component,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(existing)
                    session.flush()
                    hypotheses[update.key] = existing
                    from_status = None
                else:
                    from_status = existing.status
                    if update.description:
                        existing.description = update.description
                    if update.status:
                        existing.status = update.status
                    if update.confidence is not None:
                        existing.confidence = _decimal(update.confidence)
                    if update.missing_evidence is not None:
                        existing.missing_evidence = update.missing_evidence
                    existing.updated_at = now
                for relation, ids in (
                    ("supports", update.supporting_evidence_ids),
                    ("contradicts", update.contradicting_evidence_ids),
                ):
                    for evidence_id in ids:
                        session.execute(
                            pg_insert(HypothesisEvidenceLinkRow)
                            .values(
                                hypothesis_id=existing.id,
                                evidence_id=evidence_id,
                                relation=relation,
                            )
                            .on_conflict_do_nothing(index_elements=["hypothesis_id", "evidence_id"])
                        )
                session.flush()
                snapshots[update.key] = self._snapshots(session, [existing])[update.key]
                applied.append(
                    {
                        "key": update.key,
                        "cause_category": existing.cause_category,
                        "component": existing.component,
                        "from_status": from_status,
                        "to_status": existing.status,
                        "confidence": update.confidence,
                        "added_supporting": [str(e) for e in update.supporting_evidence_ids],
                        "added_contradicting": [str(e) for e in update.contradicting_evidence_ids],
                        "rationale": update.rationale,
                    }
                )
                metrics.increment(
                    "investigation.hypothesis_transition",
                    from_status=str(from_status),
                    to_status=existing.status,
                )

            outcome = HypothesisUpdateOutcome(applied=applied, rejected=rejected)
            self._append_step(
                session,
                row,
                StepKind.HYPOTHESIS_UPDATE,
                iteration,
                {
                    "applied": applied,
                    "rejected": rejected,
                    "hypotheses": [s.to_json() for s in snapshots.values()],
                },
                call_id,
                None,
            )
            row.last_action = f"update_hypotheses: {len(applied)} applied, {len(rejected)} rejected"
            row.updated_at = now
            session.commit()
            return outcome

    # --- outcomes ------------------------------------------------------------

    def complete(
        self,
        investigation_id: uuid.UUID,
        *,
        owner: str,
        iteration: int,
        call_id: str,
        result: FinalInvestigationResult,
    ) -> ConclusionOutcome:
        """Accept the conclusion only if every cited evidence id is
        accessible and the stopping criteria hold; otherwise record why and
        leave the investigation running."""
        with self._session_factory() as session:
            row = self._fenced(session, investigation_id, owner)
            accessible = self._accessible_evidence(session, row)
            hypothesis_rows = self._hypothesis_rows(session, investigation_id)
            snapshots = self._snapshots(session, hypothesis_rows)
            types = self._evidence_types(session, row.incident_id)

            unmet: list[str] = []
            bad = ungrounded_ids(result.rca.evidence_ids(), accessible)
            if bad:
                unmet.append(
                    "rca cites evidence ids this investigation was never shown: " + ", ".join(bad)
                )
            unmet += evaluate_conclusion(result, snapshots, types, self._criteria)
            if unmet:
                self._append_step(
                    session,
                    row,
                    StepKind.CONCLUSION_REJECTED,
                    iteration,
                    {
                        "unmet_criteria": unmet,
                        "proposed": result.model_dump(mode="json"),
                    },
                    call_id,
                    None,
                )
                row.last_action = "conclusion rejected"
                session.commit()
                metrics.increment("investigation.conclusion_rejected")
                return ConclusionOutcome(accepted=False, unmet_criteria=unmet)

            now = self._clock()
            selected = next(h for h in hypothesis_rows if h.key == result.selected_hypothesis_key)
            selected.status = HypothesisStatus.SELECTED.value
            selected.confidence = _decimal(result.confidence)
            selected.updated_at = now
            selected_snapshot = snapshots[selected.key]

            steps = self._steps(session, investigation_id)
            report = {
                **result.rca.model_dump(mode="json"),
                "confidence": result.confidence,
                "root_cause_hypothesis": {
                    "key": selected.key,
                    "description": selected.description,
                    "cause_category": selected.cause_category,
                    "component": selected.component,
                },
                # Derived by the application, not asserted by the model:
                "supporting_evidence": [str(e) for e in selected_snapshot.supporting],
                "investigation_actions": _actions(steps),
                "competing_hypotheses": [
                    s.to_json() for k, s in snapshots.items() if k != selected.key
                ],
            }
            rca = RcaReportRow(
                id=uuid.uuid4(),
                incident_id=row.incident_id,
                investigation_id=investigation_id,
                root_cause_hypothesis_id=selected.id,
                report=report,
                summary=render_rca_summary(report),
                generated_at=now,
            )
            session.add(rca)
            row.status = InvestigationStatus.COMPLETED.value
            row.selected_hypothesis_id = selected.id
            row.final_result = {
                "outcome": "root_cause_identified",
                "rca_report_id": str(rca.id),
                "selected_hypothesis_key": selected.key,
                "confidence": result.confidence,
            }
            row.completed_at = now
            row.updated_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_action = "completed"
            session.flush()
            self._append_step(
                session,
                row,
                StepKind.OUTCOME,
                iteration,
                {"status": "COMPLETED", "rca_report_id": str(rca.id)},
                call_id,
                None,
            )
            incident = session.get(IncidentRow, row.incident_id)
            assert incident is not None
            self._move_incident(session, incident, IncidentStatus.RCA_READY, "root_cause_selected")
            repository.insert_outbox_event(
                session,
                event_type=EVENT_TYPE_INVESTIGATION_COMPLETED,
                aggregate_type=AGGREGATE_TYPE_INVESTIGATION,
                aggregate_id=investigation_id,
                correlation_id=row.incident_id,
                producer=PRODUCER_INCIDENT_CORE,
                payload=InvestigationCompletedPayload(
                    investigation_id=investigation_id,
                    incident_id=row.incident_id,
                    selected_hypothesis_id=selected.id,
                    rca_report_id=rca.id,
                ).model_dump(mode="json"),
            )
            session.commit()
            metrics.increment("investigation.outcome", outcome="COMPLETED", model=row.model_name)
            log.info(
                "investigation.completed",
                investigation_id=str(investigation_id),
                incident_id=str(row.incident_id),
                selected=selected.key,
                rca_report_id=str(rca.id),
            )
            return ConclusionOutcome(accepted=True, rca_report_id=rca.id)

    def escalate(
        self,
        investigation_id: uuid.UUID,
        *,
        owner: str,
        iteration: int,
        outcome: InvestigationStatus,
        reason_code: str,
        detail: str,
        inconclusive_reason: str | None = None,
        evidence_gaps: list[str] | None = None,
        call_id: str | None = None,
    ) -> None:
        """End without a root cause: ESCALATED (inconclusive / budget) or
        FAILED (technical). Persists a structured incomplete result."""
        assert outcome in (InvestigationStatus.ESCALATED, InvestigationStatus.FAILED)
        with self._session_factory() as session:
            row = self._fenced(session, investigation_id, owner)
            snapshots = self._snapshots(session, self._hypothesis_rows(session, investigation_id))
            accessible = self._accessible_evidence(session, row)
            now = self._clock()
            leading = _leading(snapshots)
            row.status = outcome.value
            row.final_result = {
                "outcome": outcome.value,
                "reason_code": reason_code,
                "detail": detail[:2000],
                "evidence_gaps": evidence_gaps or [],
                "leading_hypothesis": leading.to_json() if leading else None,
                "hypotheses": [s.to_json() for s in snapshots.values()],
                "evidence_ids": sorted(str(e) for e in accessible),
                "budget_usage": {
                    "iterations": row.iteration_count,
                    "tool_calls": row.tool_call_count,
                    "evidence_items": row.evidence_count,
                    "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                },
            }
            if outcome == InvestigationStatus.FAILED:
                row.failure_reason = f"{reason_code}: {detail}"[:2000]
            else:
                row.escalation_reason = f"{reason_code}: {detail}"[:2000]
            row.inconclusive_reason = inconclusive_reason
            row.completed_at = now
            row.updated_at = now
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_action = f"{outcome.value.lower()}: {reason_code}"
            self._append_step(
                session,
                row,
                StepKind.OUTCOME,
                iteration,
                {"status": outcome.value, "reason_code": reason_code, "detail": detail[:2000]},
                call_id,
                None,
            )
            incident = session.get(IncidentRow, row.incident_id)
            assert incident is not None
            self._move_incident(
                session, incident, IncidentStatus.ESCALATED, f"investigation_{reason_code}"
            )
            repository.insert_outbox_event(
                session,
                event_type=EVENT_TYPE_INVESTIGATION_FAILED,
                aggregate_type=AGGREGATE_TYPE_INVESTIGATION,
                aggregate_id=investigation_id,
                correlation_id=row.incident_id,
                producer=PRODUCER_INCIDENT_CORE,
                payload=InvestigationFailedPayload(
                    investigation_id=investigation_id,
                    incident_id=row.incident_id,
                    outcome=outcome.value,
                    reason_code=reason_code,
                ).model_dump(mode="json"),
            )
            session.commit()
            metrics.increment(
                "investigation.outcome",
                outcome=outcome.value,
                reason=reason_code,
                model=row.model_name,
            )
            log.info(
                "investigation.ended_without_root_cause",
                investigation_id=str(investigation_id),
                outcome=outcome.value,
                reason_code=reason_code,
            )

    # --- queries ---------------------------------------------------------------

    def load_state(self, investigation_id: uuid.UUID) -> InvestigationState:
        with self._session_factory() as session:
            row = session.get(InvestigationRow, investigation_id)
            if row is None:
                raise InvestigationNotFoundError(str(investigation_id))
            hypotheses = self._hypothesis_rows(session, investigation_id)
            return InvestigationState(
                investigation=_view(row),
                steps=self._steps(session, investigation_id),
                hypotheses=self._snapshots(session, hypotheses),
                accessible_evidence=self._accessible_evidence(session, row),
                evidence_types=self._evidence_types(session, row.incident_id),
            )

    def get_trace(self, investigation_id: uuid.UUID) -> dict[str, Any]:
        """The complete, replayable investigation trace as one document."""
        state = self.load_state(investigation_id)
        with self._session_factory() as session:
            rca = session.execute(
                select(RcaReportRow).where(RcaReportRow.investigation_id == investigation_id)
            ).scalar_one_or_none()
        return {
            "investigation": state.investigation.model_dump(mode="json"),
            "hypotheses": [h.to_json() for h in state.hypotheses.values()],
            "steps": [s.model_dump(mode="json") for s in state.steps],
            "rca_report": None
            if rca is None
            else {"id": str(rca.id), "summary": rca.summary, "report": rca.report},
        }

    def incident_transitions(self, incident_id: uuid.UUID) -> list[dict[str, Any]]:
        """The incident's status changes, in order, from its outbox events --
        part of a recorded investigation (state transitions it caused)."""
        with self._session_factory() as session:
            rows = session.execute(
                select(OutboxEventRow)
                .where(
                    OutboxEventRow.aggregate_id == incident_id,
                    OutboxEventRow.event_type == EVENT_TYPE_INCIDENT_STATUS_CHANGED,
                )
                .order_by(OutboxEventRow.sequence)
            ).scalars()
            return [
                {
                    "at": r.occurred_at.isoformat(),
                    "from": r.payload.get("from_status"),
                    "to": r.payload.get("to_status"),
                    "reason": r.payload.get("reason"),
                }
                for r in rows
            ]

    def latest_for_incident(self, incident_id: uuid.UUID) -> InvestigationView | None:
        with self._session_factory() as session:
            row = session.execute(
                select(InvestigationRow)
                .where(InvestigationRow.incident_id == incident_id)
                .order_by(InvestigationRow.attempt_number.desc())
                .limit(1)
            ).scalar_one_or_none()
            return _view(row) if row else None

    # --- internals -------------------------------------------------------------

    def _lock(self, session: Session, investigation_id: uuid.UUID) -> InvestigationRow:
        row = session.execute(
            select(InvestigationRow)
            .where(InvestigationRow.id == investigation_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise InvestigationNotFoundError(str(investigation_id))
        return row

    def _fenced(
        self, session: Session, investigation_id: uuid.UUID, owner: str
    ) -> InvestigationRow:
        row = self._lock(session, investigation_id)
        if row.status != InvestigationStatus.INVESTIGATING.value or row.lease_owner != owner:
            raise LeaseLostError(
                f"{owner} does not hold investigation {investigation_id} "
                f"(status {row.status}, owner {row.lease_owner})"
            )
        return row

    def _append_step(
        self,
        session: Session,
        row: InvestigationRow,
        kind: StepKind,
        iteration: int,
        payload: dict[str, Any],
        call_id: str | None,
        latency_ms: int | None,
    ) -> int:
        current = session.execute(
            select(func.coalesce(func.max(InvestigationStepRow.sequence), 0)).where(
                InvestigationStepRow.investigation_id == row.id
            )
        ).scalar_one()
        sequence = int(current) + 1
        session.add(
            InvestigationStepRow(
                id=uuid.uuid4(),
                investigation_id=row.id,
                sequence=sequence,
                iteration=iteration,
                kind=kind.value,
                call_id=call_id,
                payload=payload,
                latency_ms=latency_ms,
                created_at=self._clock(),
            )
        )
        session.flush()
        return sequence

    def _steps(self, session: Session, investigation_id: uuid.UUID) -> list[StepView]:
        rows = session.execute(
            select(InvestigationStepRow)
            .where(InvestigationStepRow.investigation_id == investigation_id)
            .order_by(InvestigationStepRow.sequence)
        ).scalars()
        return [
            StepView(
                sequence=r.sequence,
                iteration=r.iteration,
                kind=StepKind(r.kind),
                call_id=r.call_id,
                payload=r.payload,
                latency_ms=r.latency_ms,
                created_at=r.created_at,
            )
            for r in rows
        ]

    def _accessible_evidence(self, session: Session, row: InvestigationRow) -> set[uuid.UUID]:
        """Evidence ids this investigation has actually been shown (context +
        tool results), intersected with what is registered for the incident.
        Computed from incident-core's own trace -- never from worker input."""
        shown: set[uuid.UUID] = set()
        payload: dict[str, Any]
        for payload in session.execute(
            select(InvestigationStepRow.payload).where(
                InvestigationStepRow.investigation_id == row.id,
                InvestigationStepRow.kind.in_([StepKind.CONTEXT.value, StepKind.TOOL_CALL.value]),
            )
        ).scalars():
            ids: list[str] = payload.get("evidence_ids", [])
            shown.update(uuid.UUID(e) for e in ids)
        if not shown:
            return shown
        registered = set(
            session.execute(
                select(EvidenceRefRow.id).where(
                    EvidenceRefRow.incident_id == row.incident_id, EvidenceRefRow.id.in_(shown)
                )
            ).scalars()
        )
        return shown & registered

    @staticmethod
    def _evidence_types(session: Session, incident_id: uuid.UUID) -> dict[uuid.UUID, str]:
        return {
            r.id: r.evidence_type
            for r in session.execute(
                select(EvidenceRefRow).where(EvidenceRefRow.incident_id == incident_id)
            ).scalars()
        }

    @staticmethod
    def _hypothesis_rows(session: Session, investigation_id: uuid.UUID) -> list[HypothesisRow]:
        return list(
            session.execute(
                select(HypothesisRow)
                .where(HypothesisRow.investigation_id == investigation_id)
                .order_by(HypothesisRow.created_at, HypothesisRow.key)
            ).scalars()
        )

    @staticmethod
    def _snapshots(session: Session, rows: list[HypothesisRow]) -> dict[str, HypothesisSnapshot]:
        if not rows:
            return {}
        links = session.execute(
            select(HypothesisEvidenceLinkRow)
            .where(HypothesisEvidenceLinkRow.hypothesis_id.in_([r.id for r in rows]))
            .order_by(HypothesisEvidenceLinkRow.linked_at, HypothesisEvidenceLinkRow.evidence_id)
        ).scalars()
        by_hypothesis: dict[uuid.UUID, list[HypothesisEvidenceLinkRow]] = {}
        for link in links:
            by_hypothesis.setdefault(link.hypothesis_id, []).append(link)
        return {
            r.key: HypothesisSnapshot(
                key=r.key,
                description=r.description,
                status=HypothesisStatus(r.status),
                confidence=float(r.confidence) if r.confidence is not None else None,
                supporting=[
                    lk.evidence_id
                    for lk in by_hypothesis.get(r.id, [])
                    if lk.relation == "supports"
                ],
                contradicting=[
                    lk.evidence_id
                    for lk in by_hypothesis.get(r.id, [])
                    if lk.relation == "contradicts"
                ],
                missing_evidence=list(r.missing_evidence or []),
                cause_category=r.cause_category,
                component=r.component,
            )
            for r in rows
        }

    def _move_incident(
        self, session: Session, incident: IncidentRow, to_status: IncidentStatus, reason: str
    ) -> None:
        """INVESTIGATING -> RCA_READY / ESCALATED. If a human already moved
        the incident elsewhere (ESCALATED is reachable from anywhere), the
        investigation outcome is still recorded; the incident is left alone."""
        if incident.status != IncidentStatus.INVESTIGATING.value:
            log.info(
                "investigation.incident_not_investigating",
                incident_id=str(incident.id),
                status=incident.status,
            )
            return
        if not repository.transition_incident_status(
            session,
            incident=incident,
            to_status=to_status,
            now=self._clock(),
            closes=False,
        ):
            raise ConcurrentModificationError(f"incident {incident.id} changed concurrently")
        self._status_event(session, incident, "INVESTIGATING", reason)

    @staticmethod
    def _status_event(
        session: Session, incident: IncidentRow, from_status: str, reason: str
    ) -> None:
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


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 2)))


def _leading(snapshots: dict[str, HypothesisSnapshot]) -> HypothesisSnapshot | None:
    candidates = [
        s
        for s in snapshots.values()
        if s.status in (HypothesisStatus.SUPPORTED, HypothesisStatus.ACTIVE)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: (s.confidence or 0.0, len(s.supporting), s.key))


def _actions(steps: list[StepView]) -> list[dict[str, Any]]:
    """What the investigation actually did, from the trace (not the model)."""
    return [
        {
            "iteration": s.iteration,
            "tool": s.payload.get("tool"),
            "arguments": s.payload.get("arguments"),
            "ok": s.payload.get("ok"),
            "evidence_ids": s.payload.get("evidence_ids", []),
            "summary": s.payload.get("summary"),
        }
        for s in steps
        if s.kind == StepKind.TOOL_CALL
    ]


def render_rca_summary(report: dict[str, Any]) -> str:
    """Deterministic rendering (08-evidence-model.md, "RCA report rendering"):
    every claim is followed by the evidence ids that back it."""

    def cite(ids: list[str]) -> str:
        return "[" + ", ".join(i[:8] for i in ids) + "]"

    lines = [
        f"Root cause (confidence {report['confidence']:.2f}): "
        f"{report['root_cause']['text']} {cite(report['root_cause']['evidence_ids'])}",
        f"Summary: {report['incident_summary']['text']} "
        f"{cite(report['incident_summary']['evidence_ids'])}",
        f"Impact: {report['impact']['text']} {cite(report['impact']['evidence_ids'])}",
        "Affected services: "
        + "; ".join(
            f"{s['service']} {cite(s['evidence_ids'])}" for s in report["affected_services"]
        ),
        "Timeline:",
        *[f"  {t['at']} {t['event']} {cite(t['evidence_ids'])}" for t in report["timeline"]],
    ]
    if report.get("contributing_factors"):
        lines.append("Contributing factors:")
        lines += [
            f"  {c['text']} {cite(c['evidence_ids'])}" for c in report["contributing_factors"]
        ]
    if report.get("contradicting_evidence"):
        lines.append("Contradicting evidence considered:")
        lines += [
            f"  {c['evidence_id'][:8]}: {c['explanation']}"
            for c in report["contradicting_evidence"]
        ]
    if report.get("unresolved_questions"):
        lines.append("Unresolved questions (not evidence-backed):")
        lines += [f"  - {q}" for q in report["unresolved_questions"]]
    return "\n".join(lines)


def _view(row: InvestigationRow) -> InvestigationView:
    return InvestigationView(
        id=row.id,
        incident_id=row.incident_id,
        attempt_number=row.attempt_number,
        status=InvestigationStatus(row.status),
        model_provider=row.model_provider,
        model_name=row.model_name,
        model_settings=row.model_config,
        budget=InvestigationBudget.model_validate(row.budget),
        iteration_count=row.iteration_count,
        tool_call_count=row.tool_call_count,
        evidence_count=row.evidence_count,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_creation_tokens=row.cache_creation_tokens,
        last_action=row.last_action,
        failure_reason=row.failure_reason,
        escalation_reason=row.escalation_reason,
        inconclusive_reason=row.inconclusive_reason,
        selected_hypothesis_id=row.selected_hypothesis_id,
        final_result=row.final_result,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )
