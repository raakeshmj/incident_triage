"""Action catalog completeness and strictness; the deterministic planner.
No database, no network."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from packages.remediation.catalog import CATALOG, CATALOG_VERSION, catalog_digest, get_entry
from packages.remediation.planner import RemediationPlanner

REQUIRED_ACTIONS = {
    "restart_service",
    "scale_service",
    "rollback_deployment",
    "disable_feature_flag",
    "revert_configuration",
}


def test_the_catalog_defines_exactly_the_required_actions_completely():
    assert set(CATALOG) == REQUIRED_ACTIONS
    for entry in CATALOG.values():
        d = entry.describe()
        assert d["parameters_schema"]["required"]  # required parameters are explicit
        assert d["parameters_schema"].get("additionalProperties") is False
        assert entry.allowed_environments and entry.timeout_seconds > 0
        assert 1 <= entry.base_blast_radius <= entry.max_blast_radius <= 2
        assert entry.approval_mandatory and not entry.auto_execution_allowed
        assert entry.max_attempts >= 1 and entry.verification
    assert catalog_digest() == catalog_digest() and CATALOG_VERSION


def test_retry_safety_is_declared_where_a_repeat_would_double_act():
    assert get_entry("scale_service").retry_safe is False  # type: ignore[union-attr]
    assert get_entry("scale_service").max_attempts == 1  # type: ignore[union-attr]
    assert get_entry("rollback_deployment").retry_safe is True  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "params",
    [
        {"service": "checkout-service", "from_version": "a", "to_version": "b", "extra": 1},
        {"service": "Checkout Service", "from_version": "a", "to_version": "b"},
        {"service": "checkout-service", "from_version": "a;rm -rf /", "to_version": "b"},
        {"service": "checkout-service"},
    ],
)
def test_parameters_are_strict(params):
    _, problems = get_entry("rollback_deployment").validate(params)  # type: ignore[union-attr]
    assert problems


@dataclass
class _Record:
    evidence_id: uuid.UUID
    subject_service: str
    evidence_type: SimpleNamespace
    normalized_payload: dict


class _Reader:
    def __init__(self, records):
        self.records = records

    def get_incident_evidence(self, incident_id):
        return self.records


def _rca(category, component, ids):
    return {
        "root_cause_hypothesis": {"cause_category": category, "component": component},
        "supporting_evidence": [str(i) for i in ids],
        "root_cause": {"text": "t"},
    }


def _record(kind, service, payload):
    return _Record(uuid.uuid4(), service, SimpleNamespace(value=kind), payload)


INC, INV = uuid.uuid4(), uuid.uuid4()


def test_a_deployment_cause_plans_a_rollback_from_the_cited_record():
    deploy = _record(
        "deployment",
        "checkout-service",
        {"current": {"version": "1.1.0-bad", "previous_version": "1.0.0"}},
    )
    proposal, _ = RemediationPlanner(_Reader([deploy])).plan(
        INC, INV, _rca("deployment", "checkout-service", [deploy.evidence_id])
    )
    assert proposal is not None and proposal.action_id == "rollback_deployment"
    assert proposal.parameters == {
        "service": "checkout-service",
        "from_version": "1.1.0-bad",
        "to_version": "1.0.0",
    }
    assert proposal.source == "planner" and proposal.investigation_id == INV


def test_uncited_evidence_is_never_used():
    deploy = _record(
        "deployment", "checkout-service", {"current": {"version": "2", "previous_version": "1"}}
    )
    proposal, why = RemediationPlanner(_Reader([deploy])).plan(
        INC, INV, _rca("deployment", "checkout-service", [])
    )
    assert proposal is None and "deployment record" in why


def test_config_causes_plan_a_revert_or_a_flag_disable():
    change = {"key": "request_pipeline_config_version", "old_value": "v1", "new_value": "v2"}
    config = _record("configuration", "payment-service", {"changes_in_window": [change]})
    proposal, _ = RemediationPlanner(_Reader([config])).plan(
        INC, INV, _rca("configuration", "payment-service", [config.evidence_id])
    )
    assert proposal.action_id == "revert_configuration"  # type: ignore[union-attr]
    assert proposal.parameters["from_value"] == "v2" and proposal.parameters["to_value"] == "v1"  # type: ignore[union-attr]
    flag = _record(
        "configuration",
        "payment-service",
        {"changes_in_window": [{**change, "key": "feature.new_cart"}]},
    )
    proposal, _ = RemediationPlanner(_Reader([flag])).plan(
        INC, INV, _rca("configuration", "payment-service", [flag.evidence_id])
    )
    assert proposal.action_id == "disable_feature_flag"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("category", "action"),
    [
        ("resource_memory", "restart_service"),
        ("resource_cpu", "scale_service"),
        ("traffic", "scale_service"),
    ],
)
def test_resource_causes(category, action):
    proposal, _ = RemediationPlanner(_Reader([])).plan(
        INC, INV, _rca(category, "checkout-service", [])
    )
    assert proposal.action_id == action  # type: ignore[union-attr]


@pytest.mark.parametrize("category", ["dependency", "database", "infrastructure", "other"])
def test_causes_outside_the_service_get_no_proposal(category):
    proposal, why = RemediationPlanner(_Reader([])).plan(
        INC, INV, _rca(category, "payment-service", [])
    )
    assert proposal is None and "no catalog action" in why
