"""Request-scoped logging context.

Thin wrapper over structlog's contextvars support, naming exactly the
fields Phase 1 needs to carry through every log line: correlation/request
id, incident id, command idempotency key, and event id (Phase 1
requirement 10). Any log statement emitted while inside `bind_context(...)`
picks these up automatically without having to pass them explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import structlog


@contextmanager
def bind_context(
    *,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    incident_id: str | None = None,
    alert_id: str | None = None,
    event_id: str | None = None,
) -> Iterator[None]:
    fields = {
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "incident_id": incident_id,
        "alert_id": alert_id,
        "event_id": event_id,
    }
    bound = {k: v for k, v in fields.items() if v is not None}
    tokens = structlog.contextvars.bind_contextvars(**bound)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
