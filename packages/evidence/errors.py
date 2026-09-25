"""Evidence-service errors.

Each maps to one stable `code`, which the tool layer (packages/tools)
surfaces to a future agent as a terminal tool error -- never a raw
exception or a backend's own error text.
"""

from __future__ import annotations


class EvidenceError(Exception):
    code = "evidence_error"
    retryable = False


class InvalidQueryError(EvidenceError):
    """A parameter failed validation (bad window, unknown metric, malformed id...)."""

    code = "invalid_argument"


class ScopeViolationError(EvidenceError):
    """The query targets something outside the incident's authorization scope."""

    code = "scope_violation"


class IncidentNotFoundError(EvidenceError):
    code = "incident_not_found"


class EvidenceNotFoundError(EvidenceError):
    code = "evidence_not_found"


class BackendUnavailableError(EvidenceError):
    """The telemetry backend errored or was unreachable. Transient."""

    code = "backend_unavailable"
    retryable = True


class BackendTimeoutError(BackendUnavailableError):
    code = "backend_timeout"
