"""Domain-level exceptions.

Framework-independent so both the API layer and tests can catch them
without importing FastAPI or SQLAlchemy.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain-layer errors."""


class InvalidAlertError(DomainError):
    """Raised when an inbound alert fails domain validation."""
