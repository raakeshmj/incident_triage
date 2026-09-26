"""Shared enums for the domain layer.

See docs/architecture/03-domain-model.md and
docs/architecture/04-incident-state-machine.md.
"""

from __future__ import annotations

from enum import Enum


class AlertSource(str, Enum):
    PROMETHEUS = "prometheus"
    PAGERDUTY = "pagerduty"
    GENERIC = "generic"


class AlertSeverity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class AlertStatus(str, Enum):
    FIRING = "firing"
    RESOLVED = "resolved"


class IncidentStatus(str, Enum):
    """Full state machine enum (04-incident-state-machine.md).

    Phase 1 only ever creates incidents in TRIAGING -- everything past
    that point (INVESTIGATING onward) belongs to later phases. The full
    enum is defined now so the `incidents.status` column and any future
    transition code don't need a schema change later.
    """

    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    RCA_READY = "RCA_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REMEDIATION_IN_PROGRESS = "REMEDIATION_IN_PROGRESS"
    VERIFYING = "VERIFYING"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    SUPPRESSED = "SUPPRESSED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


# Incident statuses that are NOT eligible to receive a newly-correlated
# alert -- mirrors the partial unique index `incidents_open_correlation_key`
# in docs/architecture/06-database-design.md. Keep these two definitions
# in sync; tests/unit/domain/test_correlation.py pins this list.
CLOSED_INCIDENT_STATUSES = (
    IncidentStatus.CLOSED,
    IncidentStatus.CANCELLED,
    IncidentStatus.SUPPRESSED,
    # Phase 8 (migration 0007): a verified recovery closes the incident for
    # correlation -- a later alert is a new incident, not an addendum.
    IncidentStatus.RESOLVED,
)
