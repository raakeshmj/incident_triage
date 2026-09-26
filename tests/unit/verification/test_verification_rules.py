"""Verification rules: pure, deterministic, evidence-only. No I/O."""

from __future__ import annotations

import pytest

from packages.domain.verification import (
    Sample,
    VerificationStatus,
    build_spec,
    decide,
    evaluate_sample,
)
from packages.remediation.catalog import CATALOG, get_entry

ROLLBACK = {"service": "checkout-service", "from_version": "1.1.0-bad", "to_version": "1.0.0"}
HEALTHY = {"status": "healthy", "signals": {"error_rate": 0.002, "latency_p95": 0.2}}
SICK = {"status": "degraded", "signals": {"error_rate": 0.31, "latency_p95": 1.8}}


def _spec(action="rollback_deployment", params=ROLLBACK, baseline=None):
    return build_spec(
        get_entry(action).verification, action_id=action, parameters=params, baseline=baseline
    )  # type: ignore[union-attr]


def test_every_catalog_action_declares_windows_and_checks():
    for entry in CATALOG.values():
        v = entry.verification
        assert v.grace_seconds > 0 and v.window_seconds > 0 and v.poll_interval_seconds > 0
        assert v.required_consecutive >= 2  # never a single instantaneous reading
        assert v.timeout_seconds > v.grace_seconds + v.window_seconds
        assert any(c.kind == "health.status" for c in v.checks)
        assert any(c.kind == "alerts.no_new_firing" for c in v.checks)


def test_expected_values_come_from_the_proposal_and_baseline_never_the_executor():
    spec = _spec()
    version = next(c for c in spec.checks if c.kind == "state.deployment_version")
    assert version.expected == "1.0.0" and version.definitive
    scale = _spec(
        "scale_service",
        {"service": "checkout-service", "increase_by": 2},
        baseline={"runtime": {"replicas": 3}},
    )
    assert next(c for c in scale.checks if c.kind == "state.replicas").expected == 5
    no_baseline = _spec("scale_service", {"service": "checkout-service", "increase_by": 2})
    assert next(c for c in no_baseline.checks if c.kind == "state.replicas").expected is None


def test_time_scale_shortens_every_window_proportionally():
    full, fast = (
        _spec(),
        build_spec(
            get_entry("rollback_deployment").verification,  # type: ignore[union-attr]
            action_id="rollback_deployment",
            parameters=ROLLBACK,
            baseline=None,
            time_scale=0.01,
        ),
    )
    assert fast.grace_seconds == pytest.approx(full.grace_seconds * 0.01)
    assert fast.timeout_seconds == pytest.approx(full.timeout_seconds * 0.01)
    assert fast.required_consecutive == full.required_consecutive


def test_a_recovered_sample_passes_every_check():
    result = evaluate_sample(
        _spec(), Sample(health=HEALTHY, deployment={"version": "1.0.0"}, new_firing_alerts=0)
    )
    assert result.passed and result.conclusive and result.definitive_failure is None


def test_unhealthy_metrics_fail_but_not_definitively():
    result = evaluate_sample(
        _spec(), Sample(health=SICK, deployment={"version": "1.0.0"}, new_firing_alerts=0)
    )
    assert not result.passed and result.conclusive and result.definitive_failure is None
    assert {c.kind for c in result.checks if c.ok is False} == {"health.status", "metric.max"}


def test_the_wrong_version_is_a_definitive_failure():
    result = evaluate_sample(
        _spec(), Sample(health=HEALTHY, deployment={"version": "1.2.0"}, new_firing_alerts=0)
    )
    assert result.definitive_failure and "1.2.0" in result.definitive_failure


def test_missing_evidence_is_inconclusive_never_a_pass():
    result = evaluate_sample(
        _spec(),
        Sample(
            health=None,
            deployment={"version": "1.0.0"},
            errors=["health: backend_unavailable"],
            new_firing_alerts=0,
        ),
    )
    assert not result.passed and not result.conclusive


def test_a_new_alert_during_verification_fails_the_observation():
    result = evaluate_sample(
        _spec(), Sample(health=HEALTHY, deployment={"version": "1.0.0"}, new_firing_alerts=1)
    )
    assert not result.passed and result.conclusive


def _decide(spec, *, latest=None, streak=0, conclusive=0, now=0.0, deadline=100.0):
    return decide(
        spec,
        latest=latest,
        consecutive_successes=streak,
        conclusive_observations=conclusive,
        now_seconds=now,
        deadline_seconds=deadline,
    )


def test_success_needs_the_full_consecutive_streak():
    spec = _spec()
    ok = evaluate_sample(
        spec, Sample(health=HEALTHY, deployment={"version": "1.0.0"}, new_firing_alerts=0)
    )
    assert (
        _decide(spec, latest=ok, streak=spec.required_consecutive - 1, conclusive=5).status
        == VerificationStatus.RUNNING
    )
    assert (
        _decide(spec, latest=ok, streak=spec.required_consecutive, conclusive=5).status
        == VerificationStatus.PASSED
    )


def test_deadline_verdicts_distinguish_failure_from_no_evidence():
    spec = _spec()
    sick = evaluate_sample(
        spec, Sample(health=SICK, deployment={"version": "1.0.0"}, new_firing_alerts=0)
    )
    failed = _decide(spec, latest=sick, streak=0, conclusive=4, now=100, deadline=100)
    assert failed.status == VerificationStatus.FAILED and "error_rate" in (failed.reason or "")
    timed_out = _decide(spec, latest=None, streak=0, conclusive=0, now=100, deadline=100)
    assert timed_out.status == VerificationStatus.TIMED_OUT


def test_a_definitive_failure_ends_verification_at_once():
    spec = _spec()
    wrong = evaluate_sample(
        spec, Sample(health=HEALTHY, deployment={"version": "9"}, new_firing_alerts=0)
    )
    assert _decide(spec, latest=wrong, streak=2, conclusive=3).status == VerificationStatus.FAILED
