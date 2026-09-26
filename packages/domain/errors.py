"""Domain-level exceptions.

Framework-independent so both the API layer and tests can catch them
without importing FastAPI or SQLAlchemy.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain-layer errors."""


class InvalidAlertError(DomainError):
    """Raised when an inbound alert fails domain validation."""


class IncidentNotFoundError(DomainError):
    """A command or query referenced an incident id that does not exist."""


class EvidenceRefConflictError(DomainError):
    """An evidence id was re-registered with different content or a
    different incident than it was first registered with -- evidence
    references are immutable, so this is a rejection, never an overwrite."""


class ConcurrentModificationError(DomainError):
    """An optimistic-concurrency `version` check failed; the caller should
    retry the (idempotent) command."""


class InvalidIncidentTransitionError(DomainError):
    """The requested lifecycle change isn't allowed from the incident's current state."""


class InvestigationNotFoundError(DomainError):
    pass


class LeaseLostError(DomainError):
    """This worker no longer owns the investigation (its lease expired and
    another worker claimed it). The caller must stop writing immediately."""


class RemediationNotFoundError(DomainError):
    pass


class RemediationStateError(DomainError):
    """The command isn't valid for the remediation's current status."""


class ApprovalMismatchError(DomainError):
    """An approval did not bind to the remediation's current proposal hash
    and policy decision (the proposal changed, or the approver saw a stale one)."""


class ApproverNotAuthorizedError(DomainError):
    """The approver lacks a role the policy decision requires."""


class InvalidProposalError(DomainError):
    """A proposal failed validation before policy could evaluate it."""
