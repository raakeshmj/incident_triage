"""Outbound dependency calls: W3C trace propagation + dependency metrics.

Used for the checkout -> payment -> inventory chain so a single trace spans
all three services, and so each hop's failures/latency are visible as
`dependency_call_*` metrics on the *caller* (in addition to the callee's
own `http_requests_*`) -- the same signal a real production dependency
failure produces.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import Status, StatusCode
from simulator.services.common.telemetry import ServiceTelemetry, get_logger

log = get_logger("http_client")


async def call_downstream(
    client: httpx.AsyncClient,
    telemetry: ServiceTelemetry,
    *,
    dependency: str,
    method: str,
    url: str,
    request_id: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}) or {})
    if request_id:
        headers["x-request-id"] = request_id
    inject(headers)

    with telemetry.tracer.start_as_current_span(
        f"CALL {dependency}",
        kind=trace.SpanKind.CLIENT,
        attributes={"peer.service": dependency, "http.method": method, "http.url": url},
    ) as span:
        start = time.monotonic()
        attrs = {"service": telemetry.config.service_name, "dependency": dependency}
        try:
            response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            telemetry.metrics.dependency_errors.add(1, {**attrs, "error": type(exc).__name__})
            span.set_status(Status(StatusCode.ERROR))
            span.record_exception(exc)
            log.warning("dependency.call_failed", dependency=dependency, error=str(exc))
            raise
        finally:
            telemetry.metrics.dependency_duration.record(time.monotonic() - start, attrs)

        span.set_attribute("http.status_code", response.status_code)
        if response.status_code >= 500:
            telemetry.metrics.dependency_errors.add(1, attrs)
            span.set_status(Status(StatusCode.ERROR))
            log.warning(
                "dependency.call_error_status",
                dependency=dependency,
                status_code=response.status_code,
            )
        return response
