"""Deployment/config registries (real Redis, isolated DB), the Git adapter
(this repository), and historical-incident search (real Postgres)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.evidence.adapters.changes import ChangeRegistryAdapter
from packages.evidence.adapters.git import GitAdapter
from packages.evidence.adapters.history import IncidentHistoryAdapter
from packages.evidence.errors import InvalidQueryError, ScopeViolationError
from simulator.changes.registry import ChangeRegistry
from simulator.chaos.cli import _record_change_on_start, _record_change_on_stop
from tests.factories import make_alert_command

ROOT = Path(__file__).resolve().parents[2]
CHECKOUT_PATHS = ("simulator/services/checkout-service/", "simulator/services/common/")


def _window() -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now - timedelta(hours=1), now + timedelta(seconds=1)


def test_seed_is_idempotent_and_records_real_commit_shas(test_redis):
    registry = ChangeRegistry(test_redis)
    assert registry.seed()
    assert registry.seed() == []
    current = registry.current_deployment("checkout-service")
    assert current["version"] == "1.0.0" and current["deployed_by"] == "ci-pipeline"
    assert current["commit_sha"] is None or len(current["commit_sha"]) == 40


def test_bad_deployment_scenario_records_a_deploy_then_a_rollback(test_redis):
    _record_change_on_start(
        test_redis, "bad-deployment", "checkout-service", {"version": "1.1.0-bad"}
    )
    start, end = _window()
    during = ChangeRegistryAdapter(test_redis).deployments(
        service="checkout-service", environment="production", start=start, end=end, limit=10
    )
    assert during.normalized_payload["current"]["version"] == "1.1.0-bad"
    assert during.normalized_payload["current"]["previous_version"] == "1.0.0"
    # The record says what changed, never why.
    assert "chaos" not in str(during.raw_response)

    _record_change_on_stop(test_redis, "bad-deployment", "checkout-service")
    after = ChangeRegistryAdapter(test_redis).deployments(
        service="checkout-service",
        environment="production",
        start=start,
        end=_window()[1],
        limit=10,
    )
    current = after.normalized_payload["current"]
    assert (current["version"], current["change_type"]) == ("1.0.0", "rollback")
    assert [d["version"] for d in after.normalized_payload["deployments_in_window"]] == [
        "1.0.0",
        "1.1.0-bad",
        "1.0.0",
    ]


def test_bad_configuration_scenario_records_a_push_then_a_revert(test_redis):
    _record_change_on_start(test_redis, "bad-configuration", "payment-service", {})
    start, end = _window()
    pushed = ChangeRegistryAdapter(test_redis).config_changes(
        service="payment-service", environment="production", start=start, end=end, limit=10
    )
    assert pushed.normalized_payload["effective_config"] == {
        "request_pipeline_config_version": "v2"
    }

    _record_change_on_stop(test_redis, "bad-configuration", "payment-service")
    reverted = ChangeRegistryAdapter(test_redis).config_changes(
        service="payment-service", environment="production", start=start, end=_window()[1], limit=10
    )
    assert reverted.normalized_payload["effective_config"] == {
        "request_pipeline_config_version": "v1"
    }


def test_registry_reads_are_windowed_environment_scoped_and_never_invent(test_redis):
    adapter = ChangeRegistryAdapter(test_redis)
    start, end = _window()
    empty = adapter.deployments(
        service="checkout-service", environment="production", start=start, end=end, limit=5
    )
    assert empty.normalized_payload["current"] is None and empty.result_count == 0

    ChangeRegistry(test_redis).record_deployment(
        service="checkout-service", version="2.0.0", environment="staging"
    )
    still_empty = adapter.deployments(
        service="checkout-service", environment="production", start=start, end=_window()[1], limit=5
    )
    assert still_empty.normalized_payload["current"] is None


def test_git_recent_commits_and_code_changes_are_scoped_to_service_paths():
    git = GitAdapter(ROOT)
    now = datetime.now(UTC)
    recent = git.recent_commits(
        service="checkout-service", paths=CHECKOUT_PATHS, until=now, reference_time=now, limit=5
    )
    assert recent.result_count >= 1
    sha = recent.normalized_payload["commits"][0]["sha"]
    assert "email" not in str(recent.raw_response)

    changes = git.code_changes(
        service="checkout-service", paths=CHECKOUT_PATHS, start=now, end=now, sha=sha, limit=5
    )
    files = changes.normalized_payload["commits"][0]["files"]
    assert files and all(f["path"].startswith(CHECKOUT_PATHS) for f in files)
    assert changes.normalized_payload["commits"][0]["diff_summary"]["files_changed"] == len(files)


def test_git_rejects_malformed_or_unknown_shas_and_unscoped_services():
    git = GitAdapter(ROOT)
    now = datetime.now(UTC)
    kwargs = {"service": "checkout-service", "start": now, "end": now, "limit": 5}
    with pytest.raises(InvalidQueryError):
        git.code_changes(paths=CHECKOUT_PATHS, sha="HEAD~1", **kwargs)
    with pytest.raises(InvalidQueryError):
        git.code_changes(paths=CHECKOUT_PATHS, sha="0" * 40, **kwargs)
    with pytest.raises(ScopeViolationError):
        git.code_changes(paths=(), sha=None, **kwargs)


def test_similar_incident_search_is_deterministic_and_excludes_later_incidents(core):
    def open_incident(service: str, alertname: str, environment: str = "production"):
        return core.handle_alert_received(
            make_alert_command(
                service=service,
                environment=environment,
                extra_labels={"alertname": alertname, "region": "us-east-1"},
            )
        ).incident_id

    same_service = open_incident("checkout-service", "HighErrorRate")
    other_service = open_incident("inventory-service", "MemoryPressure")
    current = open_incident("checkout-service-v2", "HighErrorRate")
    # A separate, *later* checkout incident (another environment, so it
    # doesn't correlate into the first): similar, but it can't be history.
    later = open_incident("checkout-service", "HighErrorRate", environment="staging")
    current_view = core.get_incident_view(current)

    def search():
        return IncidentHistoryAdapter(core.list_incident_summaries).search_similar(
            incident_id=current,
            service="checkout-service",
            environment="production",
            regions=("us-east-1",),
            alert_types=("HighErrorRate",),
            reference_time=current_view.created_at,
            limit=5,
        )

    result = search()
    ids = [m["incident_id"] for m in result.normalized_payload["matches"]]
    assert ids[0] == str(same_service)
    assert str(current) not in ids and str(later) not in ids
    assert str(other_service) in ids  # weaker (region/environment/recency) match, ranked below
    assert result.normalized_payload["matches"][0]["root_cause_category"] is None
    assert search().normalized_payload == result.normalized_payload
