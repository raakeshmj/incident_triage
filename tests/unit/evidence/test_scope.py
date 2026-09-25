from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.evidence import limits
from packages.evidence.errors import InvalidQueryError, ScopeViolationError
from packages.evidence.scope import IncidentScope, ServiceCatalog, bounded_limit

CATALOG = ServiceCatalog.load(
    Path(__file__).resolve().parents[3] / "infrastructure/evidence/service-catalog.json"
)
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _scope(service: str = "payment-service", opened: datetime = NOW) -> IncidentScope:
    return IncidentScope(
        incident_id=uuid.uuid4(),
        service=service,
        environment="production",
        status="TRIAGING",
        created_at=opened - timedelta(minutes=5),
        allowed_services=CATALOG.neighborhood(service),
    )


def test_neighborhood_is_the_service_plus_direct_dependencies_and_dependents():
    assert CATALOG.neighborhood("payment-service") == {
        "payment-service",
        "inventory-service",
        "checkout-service",
    }
    assert CATALOG.neighborhood("checkout-service") == {"checkout-service", "payment-service"}
    assert CATALOG.neighborhood("inventory-service") == {"inventory-service", "payment-service"}


def test_service_defaults_to_the_incident_and_rejects_anything_outside_scope():
    scope = _scope("checkout-service")
    assert scope.service_or_default(None) == "checkout-service"
    assert scope.service_or_default("payment-service") == "payment-service"
    with pytest.raises(ScopeViolationError):
        scope.service_or_default("inventory-service")  # two hops away
    with pytest.raises(InvalidQueryError):
        scope.service_or_default('checkout"} or up{job="x')  # selector injection attempt


def test_window_defaults_and_clamps_to_now():
    start, end = _scope().window(None, NOW + timedelta(hours=1), now=NOW)
    assert end == NOW
    assert end - start == limits.DEFAULT_WINDOW


@pytest.mark.parametrize(
    ("start", "end", "error"),
    [
        (NOW - timedelta(hours=4), NOW, InvalidQueryError),  # longer than MAX_WINDOW
        (NOW - timedelta(seconds=30), NOW, InvalidQueryError),  # shorter than MIN_WINDOW
        (NOW, NOW - timedelta(minutes=5), InvalidQueryError),  # inverted
        (NOW - timedelta(hours=26), NOW - timedelta(hours=25), ScopeViolationError),  # too early
    ],
)
def test_window_bounds_are_enforced(start, end, error):
    with pytest.raises(error):
        _scope().window(start, end, now=NOW)


def test_window_requires_timezone_aware_timestamps():
    with pytest.raises(InvalidQueryError):
        _scope().window(datetime(2026, 9, 25, 11, 0), None, now=NOW)


def test_change_windows_may_be_longer_but_not_reach_past_the_lookback():
    scope = _scope()
    start, end = scope.window(
        None, None, now=NOW, default=limits.MAX_CHANGE_WINDOW, max_length=limits.MAX_CHANGE_WINDOW
    )
    assert start == scope.created_at - limits.MAX_LOOKBACK_BEFORE_INCIDENT
    assert end == NOW


def test_bounded_limit_clamps_to_maximum_and_rejects_nonpositive():
    assert bounded_limit(None, default=5, maximum=10) == 5
    assert bounded_limit(500, default=5, maximum=10) == 10
    with pytest.raises(InvalidQueryError):
        bounded_limit(0, default=5, maximum=10)


def test_catalog_rejects_paths_that_escape_the_repository(tmp_path):
    bad = tmp_path / "catalog.json"
    bad.write_text('{"services": {"svc": {"repo_paths": ["../../etc/"]}}}')
    with pytest.raises(ValueError):
        ServiceCatalog.load(bad)
