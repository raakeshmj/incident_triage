from __future__ import annotations

from packages.domain.enums import CLOSED_INCIDENT_STATUSES, IncidentStatus


def test_closed_incident_statuses_matches_database_partial_index():
    """Pins the list to what the `incidents_open_correlation_key` partial
    unique index excludes (docs/architecture/06-database-design.md) --
    the migration and this list must never drift apart.
    """
    assert set(CLOSED_INCIDENT_STATUSES) == {
        IncidentStatus.CLOSED,
        IncidentStatus.CANCELLED,
        IncidentStatus.SUPPRESSED,
        IncidentStatus.RESOLVED,  # migration 0007
    }
