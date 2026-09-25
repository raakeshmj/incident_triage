"""Golden scenarios validate, keep their grading key out of model-visible
fields, and their worlds serve deterministic data through the real
adapters. No database, no network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from packages.evaluation.scenario import (
    EVAL_CATALOG,
    Scenario,
    leaked_expectations,
    load_scenarios,
)
from packages.evaluation.world import ScenarioWorld
from packages.evidence.adapters.loki import LokiAdapter
from packages.evidence.adapters.prometheus import PrometheusAdapter
from packages.evidence.errors import BackendUnavailableError
from packages.evidence.scope import ServiceCatalog

SCENARIOS = load_scenarios()
CATALOG = ServiceCatalog.load(EVAL_CATALOG)
ANCHOR = datetime(2026, 9, 1, 12, tzinfo=UTC)
REQUIRED_POSITIVE = {
    "bad-deployment",
    "database-latency",
    "dependency-failure",
    "memory-pressure",
    "cpu-saturation",
    "bad-configuration",
    "error-storm",
    "cascading-failure",
    "slow-downstream-dependency",
    "service-unavailable",
}


def test_the_golden_dataset_covers_every_required_case():
    positives = {s.id for s in SCENARIOS.values() if s.kind == "positive"}
    negatives = {s.id for s in SCENARIOS.values() if s.kind == "negative"}
    assert positives >= REQUIRED_POSITIVE
    assert len(negatives) >= 6
    assert all(SCENARIOS[n].expected.outcome == "ESCALATED" for n in negatives)


@pytest.mark.parametrize("scenario", SCENARIOS.values(), ids=list(SCENARIOS))
def test_each_scenario_is_complete_and_in_catalog(scenario: Scenario):
    assert scenario.initial_conditions and scenario.observable_symptoms
    assert scenario.service in CATALOG.services
    for service in scenario.world.metrics:
        assert service in CATALOG.services, service
    if scenario.kind == "positive":
        assert scenario.expected.root_cause is not None
        assert scenario.expected.required_evidence
        assert scenario.expected.competing_hypotheses
        assert scenario.expected.root_cause.component in CATALOG.services


@pytest.mark.parametrize("scenario", SCENARIOS.values(), ids=list(SCENARIOS))
def test_model_visible_alert_text_does_not_give_the_answer_away(scenario: Scenario):
    visible = [
        json.dumps([a.model_dump() for a in scenario.alerts]),
        scenario.service,
        scenario.environment,
    ]
    assert leaked_expectations(scenario, visible) == []
    if scenario.expected.root_cause is not None:
        text = json.dumps([a.model_dump() for a in scenario.alerts]).lower()
        for category in scenario.expected.root_cause.categories:
            assert category.replace("_", " ") not in text


def test_leak_detection_finds_a_leaked_grading_key():
    scenario = SCENARIOS["bad-deployment"]
    assert leaked_expectations(scenario, ["prefix " + scenario.description + " suffix"])


def test_scenarios_validate_strictly():
    data = json.loads((EVAL_CATALOG.parent / "scenarios" / "bad-deployment.json").read_text())
    with pytest.raises(ValidationError):
        Scenario.model_validate({**data, "unexpected": 1})
    with pytest.raises(ValidationError):  # positive scenario must expect RCA_READY
        Scenario.model_validate(
            {**data, "expected": {**data["expected"], "outcome": "ESCALATED", "root_cause": None}}
        )


def _prometheus(world: ScenarioWorld) -> PrometheusAdapter:
    import httpx

    return PrometheusAdapter(
        httpx.Client(base_url="http://p", transport=httpx.MockTransport(world._prometheus))
    )


def test_metrics_step_from_baseline_to_incident_through_the_real_adapter():
    world = ScenarioWorld(SCENARIOS["bad-deployment"], CATALOG, ANCHOR)
    obs = _prometheus(world).metric_window(
        metric="error_rate",
        service="checkout-service",
        environment="production",
        start=ANCHOR - timedelta(minutes=30),
        end=ANCHOR,
    )
    data = obs.normalized_payload
    assert data["comparison"]["direction"] == "up"
    assert data["baseline"]["avg"] == pytest.approx(0.002)
    assert data["current_value"] == pytest.approx(0.32)
    # unconfigured services read healthy defaults
    health = _prometheus(world).service_health(
        service="payment-service", environment="production", at=ANCHOR
    )
    assert health.normalized_payload["status"] == "healthy"


def test_worlds_are_deterministic():
    a = ScenarioWorld(SCENARIOS["cascading-failure"], CATALOG, ANCHOR)
    b = ScenarioWorld(SCENARIOS["cascading-failure"], CATALOG, ANCHOR)
    assert a.change_records() == b.change_records()
    assert a.log_lines("payment-service") == b.log_lines("payment-service")


def test_logs_are_filtered_by_service_severity_and_window():
    import httpx

    world = ScenarioWorld(SCENARIOS["error-storm"], CATALOG, ANCHOR)
    loki = LokiAdapter(
        httpx.Client(base_url="http://l", transport=httpx.MockTransport(world._loki))
    )
    obs = loki.query_logs(
        service="checkout-service",
        start=ANCHOR - timedelta(minutes=30),
        end=ANCHOR,
        severities=("ERROR",),
        trace_id=None,
        request_id=None,
        limit=10,
    )
    assert obs.normalized_payload["error_lines_total"] == 50
    messages = [g["message"] for g in obs.normalized_payload["representative"]]
    assert messages == ["connection pool exhausted (max=50)"]


def test_unavailable_sources_fail_like_real_outages():
    world = ScenarioWorld(SCENARIOS["evidence-source-unavailable"], CATALOG, ANCHOR)
    with pytest.raises(BackendUnavailableError):
        _prometheus(world).service_health(
            service="checkout-service", environment="production", at=ANCHOR
        )
    missing = ScenarioWorld(SCENARIOS["missing-deployment-data"], CATALOG, ANCHOR)
    registry = missing.change_registry()
    with pytest.raises(BackendUnavailableError):
        registry.deployments(
            service="checkout-service",
            environment="production",
            start=ANCHOR - timedelta(hours=1),
            end=ANCHOR,
            limit=5,
        )


def test_change_records_go_through_the_real_windowing():
    world = ScenarioWorld(SCENARIOS["deployment-without-causal-evidence"], CATALOG, ANCHOR)
    registry = world.change_registry()
    recent = registry.deployments(
        service="checkout-service",
        environment="production",
        start=ANCHOR - timedelta(minutes=30),
        end=ANCHOR,
        limit=5,
    )
    assert recent.normalized_payload["deployments_in_window_total"] == 0  # deployed at -45min
    assert recent.normalized_payload["current"]["version"] == "4.4.0"
