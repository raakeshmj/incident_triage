from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from packages.domain.correlation_engine import (
    CorrelationCandidate,
    CorrelationEngine,
    DeploymentProximityRule,
    NewAlertContext,
    RelatedAlertTypeRule,
    SameEnvironmentRule,
    SameServiceRule,
    TemporalProximityRule,
    candidate_lookback_cutoff,
    default_correlation_engine,
    same_dependency_rule,
    same_region_rule,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _alert(**overrides) -> NewAlertContext:
    defaults = dict(
        source="prometheus",
        fingerprint="fp-new",
        labels={"service": "checkout", "environment": "production", "alertname": "HighErrorRate"},
        service="checkout",
        environment="production",
        received_at=NOW,
    )
    defaults.update(overrides)
    return NewAlertContext(**defaults)


def _candidate(**overrides) -> CorrelationCandidate:
    defaults = dict(
        incident_id=uuid.uuid4(),
        correlation_key="existing-key",
        status="TRIAGING",
        service="checkout",
        environment="production",
        most_recent_alert_received_at=NOW,
        most_recent_alert_labels={
            "service": "checkout",
            "environment": "production",
            "alertname": "HighErrorRate",
        },
        most_recent_alert_fingerprint="fp-old",
    )
    defaults.update(overrides)
    return CorrelationCandidate(**defaults)


# --- individual rules --------------------------------------------------


def test_same_service_rule_matches():
    rule = SameServiceRule(weight=0.3)
    result = rule.evaluate(_alert(service="checkout"), _candidate(service="checkout"))
    assert result.contribution == 0.3
    assert result.signal == "same service"


def test_same_service_rule_does_not_match():
    rule = SameServiceRule(weight=0.3)
    result = rule.evaluate(_alert(service="checkout"), _candidate(service="payments"))
    assert result.contribution == 0.0
    assert result.signal is None


def test_same_environment_rule():
    rule = SameEnvironmentRule(weight=0.2)
    matching = rule.evaluate(_alert(environment="prod"), _candidate(environment="prod"))
    differing = rule.evaluate(_alert(environment="prod"), _candidate(environment="staging"))
    assert matching.contribution == 0.2
    assert differing.contribution == 0.0


def test_label_value_rule_neutral_when_label_missing():
    rule = same_region_rule(weight=0.1)
    alert = _alert(labels={"service": "checkout", "environment": "production"})  # no region
    candidate = _candidate(most_recent_alert_labels={"region": "us-east"})
    result = rule.evaluate(alert, candidate)
    assert result.contribution == 0.0
    assert result.signal is None  # absence is not evidence against correlation


def test_label_value_rule_matches_when_both_present_and_equal():
    rule = same_dependency_rule(weight=0.1)
    alert = _alert(labels={"dependency": "postgres"})
    candidate = _candidate(most_recent_alert_labels={"dependency": "postgres"})
    result = rule.evaluate(alert, candidate)
    assert result.contribution == 0.1
    assert result.signal == "same dependency"


def test_label_value_rule_no_match_when_different():
    rule = same_region_rule(weight=0.1)
    alert = _alert(labels={"region": "us-east"})
    candidate = _candidate(most_recent_alert_labels={"region": "us-west"})
    assert rule.evaluate(alert, candidate).contribution == 0.0


@pytest.mark.parametrize(
    "delta_seconds,expected",
    [(0, 0.2), (89, 0.2), (90, 0.2), (91, 0.0), (3600, 0.0)],
)
def test_temporal_proximity_rule(delta_seconds, expected):
    rule = TemporalProximityRule(weight=0.2, window_seconds=90)
    alert = _alert(received_at=NOW)
    candidate = _candidate(most_recent_alert_received_at=NOW - timedelta(seconds=delta_seconds))
    assert rule.evaluate(alert, candidate).contribution == expected


def test_temporal_proximity_rule_symmetric_for_later_candidate():
    """An alert that arrives *before* the candidate's most recent alert
    (clock skew / reordering) is still within-window if close enough --
    the rule uses absolute distance, not a one-sided window.
    """
    rule = TemporalProximityRule(weight=0.2, window_seconds=90)
    alert = _alert(received_at=NOW)
    candidate = _candidate(most_recent_alert_received_at=NOW + timedelta(seconds=30))
    assert rule.evaluate(alert, candidate).contribution == 0.2


def test_related_alert_type_rule_exact_match():
    rule = RelatedAlertTypeRule(weight=0.2)
    alert = _alert(labels={"alertname": "HighErrorRate"})
    candidate = _candidate(most_recent_alert_labels={"alertname": "HighErrorRate"})
    result = rule.evaluate(alert, candidate)
    assert result.contribution == 0.2
    assert result.signal == "same alert type"


def test_related_alert_type_rule_configured_relation():
    rule = RelatedAlertTypeRule(
        weight=0.2,
        related_weight_fraction=0.85,
        related_alert_types={"HighErrorRate": frozenset({"HighLatency"})},
    )
    alert = _alert(labels={"alertname": "HighErrorRate"})
    candidate = _candidate(most_recent_alert_labels={"alertname": "HighLatency"})
    result = rule.evaluate(alert, candidate)
    assert result.contribution == pytest.approx(0.17)
    assert result.signal == "related alert types"


def test_related_alert_type_rule_unrelated():
    rule = RelatedAlertTypeRule(weight=0.2)
    alert = _alert(labels={"alertname": "HighErrorRate"})
    candidate = _candidate(most_recent_alert_labels={"alertname": "DiskFull"})
    result = rule.evaluate(alert, candidate)
    assert result.contribution == 0.0
    assert result.signal is None


def test_deployment_proximity_rule_always_neutral_stub():
    rule = DeploymentProximityRule()
    result = rule.evaluate(_alert(), _candidate())
    assert result == (0.0, None) or (result.contribution == 0.0 and result.signal is None)


# --- engine: matches the worked example from the Phase 2 brief ----------


def test_engine_matches_documented_example():
    """matched: same service, same environment, within 90 seconds, related
    alert types; score: 0.87; decision: CORRELATE.
    """
    engine = default_correlation_engine(
        related_alert_types={"HighErrorRate": frozenset({"HighLatency"})}
    )
    alert = _alert(
        service="checkout",
        environment="production",
        received_at=NOW,
        labels={"service": "checkout", "environment": "production", "alertname": "HighErrorRate"},
    )
    candidate = _candidate(
        service="checkout",
        environment="production",
        most_recent_alert_received_at=NOW - timedelta(seconds=45),
        most_recent_alert_labels={
            "service": "checkout",
            "environment": "production",
            "alertname": "HighLatency",
        },
    )
    decision = engine.decide(alert, [candidate])
    assert decision.decision == "CORRELATE"
    assert decision.score == pytest.approx(0.87)
    assert set(decision.matched_signals) == {
        "same service",
        "same environment",
        "within 90 seconds",
        "related alert types",
    }
    assert decision.matched_incident_id == candidate.incident_id
    assert decision.correlation_key == candidate.correlation_key


def test_engine_new_incident_when_no_candidates():
    engine = default_correlation_engine()
    decision = engine.decide(_alert(fingerprint="fp-1"), [])
    assert decision.decision == "NEW_INCIDENT"
    assert decision.score == 0.0
    assert decision.matched_signals == ()
    assert decision.matched_incident_id is None
    assert decision.correlation_key == "fp-1"


def test_engine_new_incident_when_below_threshold():
    engine = CorrelationEngine([SameServiceRule(weight=0.3)], threshold=0.6)
    alert = _alert(service="checkout", fingerprint="fp-2")
    candidate = _candidate(service="checkout")  # only 0.3 < 0.6 threshold
    decision = engine.decide(alert, [candidate])
    assert decision.decision == "NEW_INCIDENT"
    assert decision.score == 0.3
    assert decision.matched_signals == ("same service",)
    assert decision.correlation_key == "fp-2"  # falls back to the alert's own fingerprint


def test_engine_picks_highest_scoring_candidate():
    engine = default_correlation_engine()
    alert = _alert(service="checkout", environment="production")
    weak = _candidate(
        service="checkout",
        environment="staging",  # different environment -> lower score
        most_recent_alert_received_at=NOW - timedelta(seconds=3000),
    )
    strong = _candidate(
        service="checkout",
        environment="production",
        most_recent_alert_received_at=NOW - timedelta(seconds=10),
        most_recent_alert_labels={
            "service": "checkout",
            "environment": "production",
            "alertname": "HighErrorRate",
        },
    )
    decision = engine.decide(alert, [weak, strong])
    assert decision.decision == "CORRELATE"
    assert decision.matched_incident_id == strong.incident_id


def test_engine_score_is_clamped_to_one():
    # Deliberately overlapping weights that would sum above 1.0.
    engine = CorrelationEngine(
        [SameServiceRule(weight=0.7), SameEnvironmentRule(weight=0.7)], threshold=0.6
    )
    alert = _alert(service="checkout", environment="production")
    candidate = _candidate(service="checkout", environment="production")
    decision = engine.decide(alert, [candidate])
    assert decision.score == 1.0


def test_candidate_lookback_cutoff():
    cutoff = candidate_lookback_cutoff(NOW, lookback_seconds=900)
    assert cutoff == NOW - timedelta(seconds=900)
