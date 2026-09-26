"""`VerificationEngine`: drives verifications one poll at a time.

    tick(id): claim_due (fenced lease, claim_attempt) -> observe through
              evidence-service -> record_observation (incident-core decides)

Each tick does at most one observation and holds a lease only for that
poll, so a crashed worker's verification is picked up by the next tick
after its lease lapses, from its persisted streak -- never restarted from
zero, never double-counted (a superseded worker's write is rejected).
`BaselineCollector` captures the pre-remediation observation the runner
takes just before an action executes.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from packages.domain.errors import LeaseLostError
from packages.domain.verification import (
    TERMINAL_VERIFICATION_STATUSES,
    VerificationView,
    build_spec,
)
from packages.incident.remediations import ExecutionTicket, RemediationCoreService
from packages.incident.verifications import VerificationCoreService
from packages.remediation.catalog import get_entry
from packages.telemetry.logging import get_logger
from packages.verification.observer import EvidenceObserver

log = get_logger(__name__)


class VerificationEngine:
    def __init__(
        self,
        verifications: VerificationCoreService,
        observer: EvidenceObserver,
        *,
        owner: str,
        lease_seconds: float = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._core = verifications
        self._observer = observer
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._clock = clock

    def start(self, verification_id: uuid.UUID) -> VerificationView:
        return self._core.start(verification_id, actor=f"worker:{self._owner}")

    def tick(self, verification_id: uuid.UUID) -> VerificationView | None:
        ticket = self._core.claim_due(
            verification_id, owner=self._owner, lease_seconds=self._lease_seconds
        )
        if ticket is None:
            return None
        sample, evidence_ids = self._observer.observe(
            ticket.incident_id,
            service=ticket.spec.target_service,
            needs=ticket.spec.needs(),
            requested_by=f"verification:{ticket.verification_id}",
        )
        try:
            return self._core.record_observation(
                ticket, sample=sample, evidence_ids=evidence_ids, observed_at=self._clock()
            )
        except LeaseLostError:
            log.warning("verification.lease_lost", verification_id=str(verification_id))
            return None

    def run_until_done(
        self,
        verification_id: uuid.UUID,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 0.05,
        max_ticks: int = 10_000,
    ) -> VerificationView:
        """Tick until a verdict (tests, scripts; workers use due() + tick())."""
        self.start(verification_id)
        for _ in range(max_ticks):
            self.tick(verification_id)
            view = self._core.get(verification_id)
            if view.status in TERMINAL_VERIFICATION_STATUSES:
                return view
            sleep(poll_seconds)
        return self._core.get(verification_id)


class BaselineCollector:
    """Captures the pre-remediation observation for actions whose
    verification policy requires a baseline. Returns False when it can't
    (evidence unavailable): the runner then does not act blind."""

    def __init__(self, remediations: RemediationCoreService, observer: EvidenceObserver) -> None:
        self._remediations = remediations
        self._observer = observer

    def __call__(self, ticket: ExecutionTicket, owner: str) -> bool:
        entry = get_entry(ticket.action_id)
        if entry is None or not entry.verification.baseline_required:
            return True
        if self._remediations.baseline(ticket.remediation.id) is not None:
            return True  # captured before an earlier attempt: the true "before"
        spec = build_spec(
            entry.verification,
            action_id=ticket.action_id,
            parameters=ticket.parameters,
            baseline=None,
        )
        sample, evidence_ids = self._observer.observe(
            ticket.remediation.incident_id,
            service=ticket.target_service,
            needs=spec.needs() | {"runtime"}
            if ticket.action_id == "scale_service"
            else spec.needs(),
            requested_by=f"baseline:{ticket.remediation.id}",
        )
        if sample.errors:
            log.warning(
                "remediation.baseline_unavailable",
                remediation_id=str(ticket.remediation.id),
                errors=sample.errors,
            )
            return False
        self._remediations.record_baseline(
            ticket.remediation.id,
            owner=owner,
            values=sample.model_dump(mode="json", exclude={"errors", "new_firing_alerts"}),
            evidence_ids=evidence_ids,
        )
        return True
