"""Verification domain (Phase 8): pure, deterministic, no I/O.

A verification decides, from observed evidence only, whether an executed
remediation actually fixed the incident. It never reads the executor's own
success report and never asks a model.

    catalog VerificationPolicy (per action)  +  proposal parameters  +  baseline
        -> build_spec()  -> VerificationSpec (persisted, immutable)
    each poll: evidence -> Sample -> evaluate_sample(spec, sample) -> ObservationResult
    after each poll: decide(...) -> continue | PASSED | FAILED | TIMED_OUT

Success needs `required_consecutive` consecutive *conclusive, passing*
observations after the grace period -- a transient recovery can't pass,
and a gap in evidence resets the streak. A failed state check (the
expected version / config / flag / replica count isn't there) fails at
once; everything else keeps being observed until the window closes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

VERIFICATION_POLICY_VERSION = "verification-2026.09-1"

CheckKind = Literal[
    "state.deployment_version",
    "state.config_value",
    "state.flag",
    "state.replicas",
    "health.status",
    "metric.max",
    "alerts.no_new_firing",
]
STATE_CHECKS = frozenset(
    {"state.deployment_version", "state.config_value", "state.flag", "state.replicas"}
)


class VerificationStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_VERIFICATION_STATUSES = frozenset(
    {VerificationStatus.PASSED, VerificationStatus.FAILED, VerificationStatus.TIMED_OUT}
)


@dataclass(frozen=True)
class CheckTemplate:
    kind: CheckKind
    metric: str | None = None
    max_value: float | None = None


@dataclass(frozen=True)
class VerificationPolicy:
    """Declared per action in the action catalog."""

    grace_seconds: int
    window_seconds: int
    poll_interval_seconds: int
    required_consecutive: int
    baseline_required: bool
    checks: tuple[CheckTemplate, ...] = field(default_factory=tuple)

    @property
    def timeout_seconds(self) -> int:
        return self.grace_seconds + self.window_seconds + 2 * self.poll_interval_seconds

    def describe(self) -> dict[str, Any]:
        return {
            "grace_seconds": self.grace_seconds,
            "window_seconds": self.window_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "required_consecutive": self.required_consecutive,
            "timeout_seconds": self.timeout_seconds,
            "baseline_required": self.baseline_required,
            "checks": [
                {"kind": c.kind, "metric": c.metric, "max_value": c.max_value} for c in self.checks
            ],
        }


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckSpec(_Frozen):
    kind: CheckKind
    metric: str | None = None
    max_value: float | None = None
    key: str | None = None  # config key / flag name
    expected: Any = None
    definitive: bool = False


class VerificationSpec(_Frozen):
    policy_version: str
    action_id: str
    target_service: str
    grace_seconds: float
    window_seconds: float
    poll_interval_seconds: float
    timeout_seconds: float
    required_consecutive: int
    checks: list[CheckSpec]

    def needs(self) -> set[str]:
        """Which evidence sources an observation must read."""
        wanted = {"health"}
        for c in self.checks:
            if c.kind == "state.deployment_version":
                wanted.add("deployment")
            elif c.kind == "state.config_value":
                wanted.add("config")
            elif c.kind in ("state.flag", "state.replicas"):
                wanted.add("runtime")
        return wanted


def build_spec(
    policy: VerificationPolicy,
    *,
    action_id: str,
    parameters: dict[str, Any],
    baseline: dict[str, Any] | None,
    time_scale: float = 1.0,
) -> VerificationSpec:
    """Resolve the catalog policy against this remediation's parameters and
    baseline: every expected value comes from the approved proposal (or the
    baseline), never from the executor's result."""
    checks: list[CheckSpec] = []
    for t in policy.checks:
        if t.kind == "state.deployment_version":
            checks.append(
                CheckSpec(kind=t.kind, expected=parameters.get("to_version"), definitive=True)
            )
        elif t.kind == "state.config_value":
            checks.append(
                CheckSpec(
                    kind=t.kind,
                    key=parameters.get("key"),
                    expected=parameters.get("to_value"),
                    definitive=True,
                )
            )
        elif t.kind == "state.flag":
            checks.append(
                CheckSpec(
                    kind=t.kind, key=parameters.get("flag"), expected="false", definitive=True
                )
            )
        elif t.kind == "state.replicas":
            before = ((baseline or {}).get("runtime") or {}).get("replicas")
            expected = (
                int(before) + int(parameters.get("increase_by", 0)) if before is not None else None
            )
            checks.append(CheckSpec(kind=t.kind, expected=expected, definitive=True))
        elif t.kind == "health.status":
            checks.append(CheckSpec(kind=t.kind, expected="healthy"))
        elif t.kind == "metric.max":
            checks.append(CheckSpec(kind=t.kind, metric=t.metric, max_value=t.max_value))
        else:
            checks.append(CheckSpec(kind=t.kind, expected=0))
    scale = max(time_scale, 0.0)
    return VerificationSpec(
        policy_version=VERIFICATION_POLICY_VERSION,
        action_id=action_id,
        target_service=str(parameters.get("service", "")),
        grace_seconds=policy.grace_seconds * scale,
        window_seconds=policy.window_seconds * scale,
        poll_interval_seconds=policy.poll_interval_seconds * scale,
        timeout_seconds=policy.timeout_seconds * scale,
        required_consecutive=policy.required_consecutive,
        checks=checks,
    )


class Sample(_Frozen):
    """What one poll observed, reduced to the values the checks read.
    A missing source (None) plus an entry in `errors` makes the sample
    inconclusive."""

    health: dict[str, Any] | None = None  # {"status": ..., "signals": {metric: value}}
    deployment: dict[str, Any] | None = None  # {"version": ...}
    config: dict[str, Any] | None = None  # {"effective": {key: value}}
    runtime: dict[str, Any] | None = None  # {"replicas": int, "flags": {name: value}}
    new_firing_alerts: int | None = None
    errors: list[str] = []


class CheckResult(_Frozen):
    kind: str
    ok: bool | None  # None: could not be evaluated (source missing)
    observed: Any = None
    expected: Any = None
    detail: str = ""


class ObservationResult(_Frozen):
    passed: bool
    conclusive: bool
    definitive_failure: str | None
    checks: list[CheckResult]


def _metric(sample: Sample, name: str | None) -> float | None:
    signals = (sample.health or {}).get("signals") or {}
    value = signals.get(name) if name else None
    if isinstance(value, dict):
        value = value.get("value")
    return float(value) if isinstance(value, (int, float)) else None


def evaluate_sample(spec: VerificationSpec, sample: Sample) -> ObservationResult:
    results: list[CheckResult] = []
    for c in spec.checks:
        observed: Any
        if c.kind == "state.deployment_version":
            observed = (sample.deployment or {}).get("version") if sample.deployment else None
            ok = None if sample.deployment is None else observed == c.expected
            detail = f"running {observed!r}, expected {c.expected!r}"
        elif c.kind == "state.config_value":
            effective = (sample.config or {}).get("effective") or {}
            observed = effective.get(c.key or "")
            ok = None if sample.config is None else str(observed) == str(c.expected)
            detail = f"{c.key} is {observed!r}, expected {c.expected!r}"
        elif c.kind == "state.flag":
            observed = ((sample.runtime or {}).get("flags") or {}).get(c.key or "")
            ok = None if sample.runtime is None else str(observed).lower() == "false"
            detail = f"flag {c.key} is {observed!r}, expected off"
        elif c.kind == "state.replicas":
            observed = (sample.runtime or {}).get("replicas")
            ok = None if sample.runtime is None or c.expected is None else observed == c.expected
            detail = f"{observed} replicas, expected {c.expected}"
        elif c.kind == "health.status":
            observed = (sample.health or {}).get("status")
            ok = None if sample.health is None else observed == "healthy"
            detail = f"service is {observed}"
        elif c.kind == "metric.max":
            observed = _metric(sample, c.metric)
            ok = None if observed is None else observed <= float(c.max_value or 0)
            detail = f"{c.metric} {observed} vs max {c.max_value}"
        else:
            observed = sample.new_firing_alerts
            ok = None if observed is None else observed == 0
            detail = f"{observed} alert(s) fired since verification started"
        results.append(
            CheckResult(
                kind=c.kind,
                ok=ok,
                observed=observed,
                expected=c.expected if c.kind != "metric.max" else c.max_value,
                detail=detail,
            )
        )
    conclusive = not sample.errors and all(r.ok is not None for r in results)
    definitive = next(
        (
            r.detail
            for c, r in zip(spec.checks, results, strict=True)
            if c.definitive and r.ok is False
        ),
        None,
    )
    return ObservationResult(
        passed=conclusive and all(r.ok for r in results),
        conclusive=conclusive,
        definitive_failure=definitive,
        checks=results,
    )


class Decision(_Frozen):
    status: VerificationStatus  # RUNNING = keep observing
    reason: str | None = None


def decide(
    spec: VerificationSpec,
    *,
    latest: ObservationResult | None,
    consecutive_successes: int,
    conclusive_observations: int,
    now_seconds: float,
    deadline_seconds: float,
) -> Decision:
    """After a poll (or when the deadline arrives without one)."""
    if latest is not None and latest.definitive_failure:
        return Decision(status=VerificationStatus.FAILED, reason=latest.definitive_failure)
    if consecutive_successes >= spec.required_consecutive:
        return Decision(status=VerificationStatus.PASSED)
    if now_seconds >= deadline_seconds:
        if conclusive_observations == 0:
            return Decision(
                status=VerificationStatus.TIMED_OUT,
                reason="no conclusive observation before the deadline (evidence unavailable)",
            )
        failing = (
            "; ".join(c.detail for c in latest.checks if c.ok is False)
            if latest is not None
            else ""
        )
        return Decision(
            status=VerificationStatus.FAILED,
            reason=f"{consecutive_successes}/{spec.required_consecutive} consecutive successful "
            f"observations when the window closed" + (f" ({failing})" if failing else ""),
        )
    return Decision(status=VerificationStatus.RUNNING)


class VerificationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: Any
    incident_id: Any
    remediation_id: Any
    verification_type: str
    policy_version: str
    spec: dict[str, Any]
    baseline: dict[str, Any] | None
    status: VerificationStatus
    consecutive_successes: int
    observation_count: int
    result: str | None
    failure_reason: str | None
    next_action: str | None
    correlation_id: Any
    grace_until: Any | None
    deadline_at: Any | None
    next_poll_at: Any | None
    started_at: Any | None
    completed_at: Any | None
    created_at: Any
    updated_at: Any
