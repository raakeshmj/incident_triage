"""Real Postgres: Case F -- a late-arriving alert must not correlate to a
long-idle incident just because the service/environment match, per the
documented candidate lookback window
(packages.domain.correlation_engine.DEFAULT_CANDIDATE_LOOKBACK_SECONDS and
repository.find_open_incident_candidates).

Correlation timing is driven by `IncidentCoreService`'s injectable clock
(decoupled from `alerts.received_at`, which is DB-assigned at insert time)
specifically so tests can simulate "time has passed" without an actual
multi-minute sleep -- see the clock parameter's docstring in
packages/incident/service.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.domain.correlation_engine import DEFAULT_CANDIDATE_LOOKBACK_SECONDS
from packages.incident.service import IncidentCoreService
from tests.factories import make_alert_command


def test_alert_outside_lookback_window_creates_new_incident_despite_matching_service(
    session_factory,
):
    # Anchored to real "now": `alerts.received_at` is DB-assigned at insert
    # time (real wall-clock), so the injected clock must stay in the same
    # neighborhood for the candidate query's comparison to mean anything --
    # an arbitrary fixed-in-the-past date would make every real DB
    # timestamp look "in the future" relative to it.
    t0 = datetime.now(UTC)
    core_at_t0 = IncidentCoreService(session_factory, clock=lambda: t0)

    first = core_at_t0.handle_alert_received(
        make_alert_command(external_id="late-1", service="stale-checkout", environment="production")
    )
    assert first.incident_created is True

    # A second, otherwise-identical alert arrives well outside the
    # candidate lookback window.
    t1 = t0 + timedelta(seconds=DEFAULT_CANDIDATE_LOOKBACK_SECONDS * 3)
    core_at_t1 = IncidentCoreService(session_factory, clock=lambda: t1)
    second = core_at_t1.handle_alert_received(
        make_alert_command(external_id="late-2", service="stale-checkout", environment="production")
    )

    assert second.incident_created is True
    assert second.incident_id != first.incident_id


def test_alert_inside_lookback_window_still_correlates(session_factory):
    t0 = datetime.now(UTC)
    core_at_t0 = IncidentCoreService(session_factory, clock=lambda: t0)

    first = core_at_t0.handle_alert_received(
        make_alert_command(
            external_id="fresh-1", service="fresh-checkout", environment="production"
        )
    )

    t1 = t0 + timedelta(seconds=30)
    core_at_t1 = IncidentCoreService(session_factory, clock=lambda: t1)
    second = core_at_t1.handle_alert_received(
        make_alert_command(
            external_id="fresh-2", service="fresh-checkout", environment="production"
        )
    )

    assert second.incident_created is False
    assert second.incident_id == first.incident_id
