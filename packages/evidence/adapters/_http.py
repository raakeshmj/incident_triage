"""Shared read-only HTTP plumbing for the telemetry adapters.

Backend failures become evidence-service errors with stable codes; a
backend's own error body never propagates past this point (it can contain
anything, including text from the telemetry itself).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from packages.evidence.errors import (
    BackendTimeoutError,
    BackendUnavailableError,
    InvalidQueryError,
)


def get_json(
    client: httpx.Client,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    source: str,
    allow_not_found: bool = False,
) -> Any:
    try:
        response = client.get(path, params=params, headers={"Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise BackendTimeoutError(f"{source} timed out") from exc
    except httpx.HTTPError as exc:
        raise BackendUnavailableError(f"{source} unreachable ({type(exc).__name__})") from exc

    if response.status_code == 404 and allow_not_found:
        return None
    if 400 <= response.status_code < 500:
        raise InvalidQueryError(f"{source} rejected the query (HTTP {response.status_code})")
    if response.status_code >= 500:
        raise BackendUnavailableError(f"{source} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise BackendUnavailableError(f"{source} returned a non-JSON response") from exc


def endpoint(client: httpx.Client, path: str) -> str:
    return str(client.base_url.join(path))


def from_unix(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
