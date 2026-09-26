"""The executor boundary: `RemediationExecutor` and the simulator implementation.

An executor performs exactly one action-catalog action per request, and
nothing else. It is only ever called by `RemediationRunner` for an
execution attempt incident-core has already authorized (approved proposal,
re-checked kill switches and parameters, attempt row with an idempotency
key). The investigation agent has no path to it: no tool, no import (see
tests/unit/test_boundaries.py).

Every simulator action is:
- catalog-bound: parameters are re-validated against the catalog entry here
  (defense in depth -- the row could have been altered outside the normal path);
- idempotent: results are stored under the attempt's idempotency key, so a
  redelivered request returns the stored result instead of acting again;
  state-changing actions are compare-and-set (rollback checks `from_version`,
  revert checks `from_value`), so even a retry after a lost result can't
  double-act;
- bounded: a request past its deadline is refused before acting;
- observable: every action appends to the simulator's operations log.

`SimulatorRemediationExecutor` acts on the simulated production environment
through the same control surfaces the simulator itself uses: a service's
fault state (`chaos:{service}`, which the running services poll) and the
simulated deployment / config registries. It has no shell, no Docker or
Kubernetes access, and touches no database.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import redis

from packages.evidence.adapters.changes import (
    CONFIG_KEY,
    DEPLOYMENTS_KEY,
    FLAGS_KEY,
    REPLICAS_KEY,
)
from packages.remediation.catalog import CATALOG_VERSION, get_entry

APPLIED_KEY = "sim:remediation:applied:{key}"
OPS_LOG_KEY = "sim:ops:{service}"
MAX_REPLICAS = 10
# Faults a process restart clears (they live in the service process).
RESTART_CLEARS = frozenset({"memory-leak", "high-cpu"})
# The simulated config system's one managed key (simulator/changes/registry.py).
CONFIG_KEY_NAME = "request_pipeline_config_version"
ACTOR = "remediation-executor"


class SimulatedChangeSystem:
    """Client for the simulated CI/CD and config systems' append-only
    registries (record contract: simulator/changes/registry.py). Writes
    only what those systems would record for a rollback / config revert."""

    def __init__(self, client: redis.Redis) -> None:
        self._redis = client

    def _latest(self, key: str) -> dict[str, Any] | None:
        raw = self._redis.lindex(key, -1)
        return json.loads(raw) if raw else None  # type: ignore[arg-type]

    def current_deployment(self, service: str) -> dict[str, Any] | None:
        return self._latest(DEPLOYMENTS_KEY.format(service=service))

    def find_deployment(self, service: str, version: str) -> dict[str, Any] | None:
        key = DEPLOYMENTS_KEY.format(service=service)
        for raw in reversed(self._redis.lrange(key, 0, -1)):  # type: ignore[arg-type]
            record = json.loads(raw)
            if record.get("version") == version:
                return record
        return None

    def current_config(self, service: str) -> dict[str, Any] | None:
        return self._latest(CONFIG_KEY.format(service=service))

    def record_rollback(
        self, service: str, environment: str, to_version: str, commit_sha: str | None
    ) -> dict[str, Any]:
        current = self.current_deployment(service) or {}
        record = {
            "deployment_id": str(uuid.uuid4()),
            "service": service,
            "environment": environment,
            "version": to_version,
            "previous_version": current.get("version"),
            "commit_sha": commit_sha,
            "deployed_at": datetime.now(UTC).isoformat(),
            "deployed_by": ACTOR,
            "change_type": "rollback",
        }
        self._redis.rpush(DEPLOYMENTS_KEY.format(service=service), json.dumps(record))
        return record

    def record_config(self, service: str, environment: str, new_value: str) -> dict[str, Any]:
        current = self.current_config(service) or {}
        record = {
            "change_id": str(uuid.uuid4()),
            "service": service,
            "environment": environment,
            "key": CONFIG_KEY_NAME,
            "old_value": current.get("new_value"),
            "new_value": new_value,
            "changed_at": datetime.now(UTC).isoformat(),
            "changed_by": ACTOR,
        }
        self._redis.rpush(CONFIG_KEY.format(service=service), json.dumps(record))
        return record


class ExecutionTimeout(Exception):
    """The executor could not finish before the deadline; the action may or
    may not have taken effect."""


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    idempotency_key: str
    action_id: str
    catalog_version: str
    parameters: dict[str, Any]
    target_service: str
    environment: str
    deadline: datetime


@dataclass(frozen=True)
class ExecutorResult:
    succeeded: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    retryable: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "detail": self.detail,
            "error": self.error,
            "retryable": self.retryable,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ExecutorResult:
        return cls(
            succeeded=bool(data["succeeded"]),
            detail=data.get("detail") or {},
            error=data.get("error"),
            retryable=bool(data.get("retryable")),
        )


class RemediationExecutor(Protocol):
    name: str

    def execute(self, request: ExecutionRequest) -> ExecutorResult: ...

    def inspect(self, idempotency_key: str) -> ExecutorResult | None:
        """What happened to an earlier request, if the executor knows."""
        ...


class SimulatorRemediationExecutor:
    name = "simulator"

    def __init__(self, client: redis.Redis, clock: Any = lambda: datetime.now(UTC)) -> None:
        self._redis = client
        self._registry = SimulatedChangeSystem(client)
        self._clock = clock

    def inspect(self, idempotency_key: str) -> ExecutorResult | None:
        raw = self._redis.get(APPLIED_KEY.format(key=idempotency_key))
        return ExecutorResult.from_json(json.loads(raw)) if raw else None  # type: ignore[arg-type]

    def execute(self, request: ExecutionRequest) -> ExecutorResult:
        previous = self.inspect(request.idempotency_key)
        if previous is not None:
            return previous  # duplicate delivery: never act twice
        entry = get_entry(request.action_id)
        if entry is None or request.catalog_version != CATALOG_VERSION:
            return ExecutorResult(
                False, error=f"{request.action_id} is not a current catalog action"
            )
        params, problems = entry.validate(request.parameters)
        if params is None:
            return ExecutorResult(False, error="invalid parameters: " + "; ".join(problems))
        if params.service != request.target_service:
            return ExecutorResult(False, error="parameters target a different service")
        if request.environment not in entry.allowed_environments:
            return ExecutorResult(
                False, error=f"{request.action_id} not allowed in {request.environment}"
            )
        if self._clock() >= request.deadline:
            raise ExecutionTimeout("deadline passed before the action started")

        handler = getattr(self, f"_{request.action_id}")
        result: ExecutorResult = handler(request, params)
        self._redis.set(
            APPLIED_KEY.format(key=request.idempotency_key), json.dumps(result.to_json())
        )
        self._redis.rpush(
            OPS_LOG_KEY.format(service=request.target_service),
            json.dumps(
                {
                    "at": time.time(),
                    "action": request.action_id,
                    "parameters": request.parameters,
                    "idempotency_key": request.idempotency_key,
                    "succeeded": result.succeeded,
                    "error": result.error,
                }
            ),
        )
        return result

    # --- actions ---------------------------------------------------------------------

    def _fault(self, service: str) -> dict[str, Any] | None:
        raw = self._redis.get(f"chaos:{service}")
        return json.loads(raw) if raw else None  # type: ignore[arg-type]

    def _clear_fault(self, service: str, scenarios: set[str] | frozenset[str]) -> str | None:
        fault = self._fault(service)
        if fault and fault.get("scenario") in scenarios:
            self._redis.delete(f"chaos:{service}")
            return str(fault["scenario"])
        return None

    def _restart_service(self, request: ExecutionRequest, params: Any) -> ExecutorResult:
        cleared = self._clear_fault(params.service, RESTART_CLEARS)
        return ExecutorResult(True, {"restarted": params.service, "cleared_fault": cleared})

    def _scale_service(self, request: ExecutionRequest, params: Any) -> ExecutorResult:
        key = REPLICAS_KEY.format(service=params.service)
        before = int(self._redis.get(key) or 1)  # type: ignore[arg-type]
        after = before + params.increase_by
        if after > MAX_REPLICAS:
            return ExecutorResult(
                False, {"replicas": before}, error=f"would exceed {MAX_REPLICAS} replicas"
            )
        self._redis.set(key, after)
        return ExecutorResult(True, {"replicas_before": before, "replicas_after": after})

    def _rollback_deployment(self, request: ExecutionRequest, params: Any) -> ExecutorResult:
        current = self._registry.current_deployment(params.service) or {}
        running = current.get("version")
        if running == params.to_version:
            cleared = self._clear_fault(params.service, {"bad-deployment"})
            return ExecutorResult(
                True, {"already_at": params.to_version, "cleared_fault": cleared, "no_op": True}
            )
        if running != params.from_version:
            return ExecutorResult(
                False,
                {"running": running},
                error=f"target changed: {params.service} runs {running}, not {params.from_version}",
            )
        target = self._registry.find_deployment(params.service, params.to_version) or {}
        record = self._registry.record_rollback(
            params.service, request.environment, params.to_version, target.get("commit_sha")
        )
        cleared = self._clear_fault(params.service, {"bad-deployment"})
        return ExecutorResult(
            True,
            {
                "deployment_id": record["deployment_id"],
                "from_version": params.from_version,
                "to_version": params.to_version,
                "cleared_fault": cleared,
            },
        )

    def _disable_feature_flag(self, request: ExecutionRequest, params: Any) -> ExecutorResult:
        key = FLAGS_KEY.format(service=params.service)
        previous = self._redis.hget(key, params.flag)
        self._redis.hset(key, params.flag, "false")
        return ExecutorResult(True, {"flag": params.flag, "previous": previous, "now": "false"})

    def _revert_configuration(self, request: ExecutionRequest, params: Any) -> ExecutorResult:
        if params.key != CONFIG_KEY_NAME:
            return ExecutorResult(False, error=f"unknown configuration key {params.key!r}")
        current = (self._registry.current_config(params.service) or {}).get("new_value")
        if current == params.to_value:
            cleared = self._clear_fault(params.service, {"bad-configuration"})
            return ExecutorResult(
                True, {"already": current, "cleared_fault": cleared, "no_op": True}
            )
        if current != params.from_value:
            return ExecutorResult(
                False,
                {"current": current},
                error=f"target changed: {params.key} is {current!r}, not {params.from_value!r}",
            )
        record = self._registry.record_config(
            params.service, request.environment, str(params.to_value)
        )
        cleared = self._clear_fault(params.service, {"bad-configuration"})
        return ExecutorResult(
            True,
            {
                "change_id": record["change_id"],
                "key": params.key,
                "now": params.to_value,
                "cleared_fault": cleared,
            },
        )
