"""`RemediationRunner`: drives authorized execution attempts through an executor.

    claim (incident-core re-checks kill switches, approval binding, catalog,
    attempts) -> executor.execute(request) under the attempt's deadline ->
    complete (EXECUTED / bounded retry / FAILED)

A claim of an attempt whose previous worker died returns a "reconcile"
ticket: the runner asks the executor what happened to that idempotency key
before anything else, and never assumes. Retries happen only where
incident-core allows them (retry-safe action, retryable failure, attempts
left). The runner never decides policy and never sees a model.
"""

from __future__ import annotations

import concurrent.futures
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from packages.domain.errors import LeaseLostError
from packages.domain.remediation import RemediationStatus, RemediationView
from packages.incident.remediations import ExecutionTicket, RemediationCoreService
from packages.remediation.executor import (
    ExecutionRequest,
    ExecutionTimeout,
    ExecutorResult,
    RemediationExecutor,
)
from packages.telemetry.logging import get_logger

log = get_logger(__name__)
_MAX_CLAIMS = 10  # far above any catalog max_attempts; a loop guard, not a policy


class RemediationRunner:
    def __init__(
        self,
        remediations: RemediationCoreService,
        executor: RemediationExecutor,
        *,
        owner: str,
        lease_seconds: int = 120,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        baseline: Callable[[ExecutionTicket, str], bool] | None = None,
    ) -> None:
        self._core = remediations
        self._executor = executor
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._baseline = baseline

    def run(self, remediation_id: uuid.UUID) -> RemediationView:
        for _ in range(_MAX_CLAIMS):
            ticket = self._core.claim_execution(
                remediation_id, owner=self._owner, lease_seconds=self._lease_seconds
            )
            if ticket is None:
                break
            try:
                view = (
                    self._reconcile(ticket) if ticket.mode == "reconcile" else self._execute(ticket)
                )
            except LeaseLostError:
                log.warning("remediation.lease_lost", remediation_id=str(remediation_id))
                break
            if view.status != RemediationStatus.EXECUTING:
                return view
        return self._core.get(remediation_id)

    def _reconcile(self, ticket: ExecutionTicket) -> RemediationView:
        known = self._executor.inspect(ticket.idempotency_key)
        if known is None:
            return self._complete(
                ticket,
                ExecutorResult(
                    False,
                    error="no executor record after the previous worker was lost; not applied",
                    retryable=True,
                ),
                reconciled=True,
            )
        return self._complete(ticket, known, reconciled=True)

    def _execute(self, ticket: ExecutionTicket) -> RemediationView:
        if self._baseline is not None and not self._baseline(ticket, self._owner):
            # the verification policy needs a "before" and we can't observe
            # one: don't act blind (nothing was executed; retry-safe actions
            # try again on their next attempt)
            return self._core.complete_execution(
                ticket.execution_id,
                owner=self._owner,
                outcome="failed",
                executor="none",
                error="pre-remediation baseline unavailable; action not executed",
                retryable=True,
            )
        request = ExecutionRequest(
            execution_id=str(ticket.execution_id),
            idempotency_key=ticket.idempotency_key,
            action_id=ticket.action_id,
            catalog_version=ticket.catalog_version,
            parameters=ticket.parameters,
            target_service=ticket.target_service,
            environment=ticket.environment,
            deadline=ticket.deadline,
        )
        remaining = (ticket.deadline - self._clock()).total_seconds()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(self._executor.execute, request)
            result = future.result(timeout=max(remaining, 0.0))
        except (concurrent.futures.TimeoutError, ExecutionTimeout) as exc:
            return self._core.complete_execution(
                ticket.execution_id,
                owner=self._owner,
                outcome="timed_out",
                executor=self._executor.name,
                error=f"no result before the deadline ({type(exc).__name__})",
                retryable=True,
            )
        except Exception as exc:  # an executor bug or transport error: outcome unknown
            return self._core.complete_execution(
                ticket.execution_id,
                owner=self._owner,
                outcome="failed",
                executor=self._executor.name,
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                retryable=True,
            )
        finally:
            pool.shutdown(wait=False)
        return self._complete(ticket, result)

    def _complete(
        self, ticket: ExecutionTicket, result: ExecutorResult, reconciled: bool = False
    ) -> RemediationView:
        return self._core.complete_execution(
            ticket.execution_id,
            owner=self._owner,
            outcome="succeeded" if result.succeeded else "failed",
            executor=self._executor.name,
            result={**result.detail, **({"reconciled": True} if reconciled else {})},
            error=result.error,
            retryable=result.retryable,
        )
