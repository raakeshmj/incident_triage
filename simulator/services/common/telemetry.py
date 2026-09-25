"""OpenTelemetry traces + metrics, and structured JSON logging, for a
simulated production service.

One call -- `install_observability(app, config)` -- wires all three
signals with the same request identity, so a single request is
correlatable across logs/traces/metrics by `request_id` and `trace_id`:

- **Traces**: an OTLP/HTTP exporter to the local OTel Collector
  (`infrastructure/otel/`). One server span per inbound request, one
  client span per outbound dependency call (`http_client.call_downstream`),
  W3C `traceparent` propagated on outbound headers.
- **Metrics**: an OTel `PrometheusMetricReader`, exposed at `GET /metrics`
  in Prometheus exposition format for `infrastructure/prometheus/` to
  scrape directly (no collector hop needed for metrics -- see ADR-0016).
- **Logs**: structured JSON to stdout via structlog, carrying
  `timestamp`/`severity`/`service`/`environment`/`region`/`request_id`/
  `trace_id`/`span_id`/`message` plus caller-supplied fields, scraped by
  Promtail (`infrastructure/promtail/`) straight from the container's
  log driver -- no application-side Loki client needed.
"""

from __future__ import annotations

import logging
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import FastAPI, Response
from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.propagate import extract
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from simulator.services.common.config import ServiceConfig


def _add_severity(_logger: object, method_name: str, event_dict: dict) -> dict:
    event_dict["severity"] = method_name.upper()
    return event_dict


def _add_trace_context(_logger: object, _method_name: str, event_dict: dict) -> dict:
    span_ctx = trace.get_current_span().get_span_context()
    if span_ctx.is_valid:
        event_dict["trace_id"] = format(span_ctx.trace_id, "032x")
        event_dict["span_id"] = format(span_ctx.span_id, "016x")
    return event_dict


def configure_logging(config: ServiceConfig) -> None:
    """Call once per process, before the first `get_logger(...).info(...)`."""

    def add_service_fields(_logger: object, _method_name: str, event_dict: dict) -> dict:
        event_dict.setdefault("service", config.service_name)
        event_dict.setdefault("environment", config.environment)
        event_dict.setdefault("region", config.region)
        return event_dict

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_severity,
            add_service_fields,
            _add_trace_context,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


@dataclass
class ServiceMetrics:
    request_count: Any
    error_count: Any
    request_duration: Any
    in_flight: Any
    dependency_duration: Any
    dependency_errors: Any
    deployment_info: Any = field(default=None)


@dataclass
class ServiceTelemetry:
    config: ServiceConfig
    tracer: trace.Tracer
    meter: metrics.Meter
    metrics: ServiceMetrics


def _build_resource(config: ServiceConfig) -> Resource:
    return Resource.create(
        {
            "service.name": config.service_name,
            "service.version": config.version,
            "deployment.environment": config.environment,
            "service.region": config.region,
        }
    )


def _setup_tracing(config: ServiceConfig) -> trace.Tracer:
    provider = TracerProvider(resource=_build_resource(config))
    exporter = OTLPSpanExporter(endpoint=f"{config.otlp_endpoint}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(config.service_name)


# OTel's own histogram default bucket boundaries (0, 5, 10, 25, 50, 75,
# 100, 250, 500, ...) assume millisecond-scale values; recording *seconds*
# into them puts nearly every real request duration in the first bucket
# and makes `histogram_quantile` in Prometheus return nonsense (a p95 of
# several seconds for requests that took single-digit milliseconds --
# caught during Phase 3 verification, see
# docs/architecture/14-observability-and-chaos.md). Prometheus's own
# classic default latency buckets, in seconds, fix this.
_LATENCY_BUCKETS_SECONDS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
)


def _setup_metrics(config: ServiceConfig) -> tuple[metrics.Meter, ServiceMetrics]:
    reader = PrometheusMetricReader()
    provider = MeterProvider(resource=_build_resource(config), metric_readers=[reader])
    metrics.set_meter_provider(provider)
    meter = metrics.get_meter(config.service_name)

    service_metrics = ServiceMetrics(
        request_count=meter.create_counter(
            "http_requests_total", description="Total inbound HTTP requests"
        ),
        error_count=meter.create_counter(
            "http_requests_errors_total", description="Inbound HTTP requests that returned 5xx"
        ),
        request_duration=meter.create_histogram(
            "http_request_duration_seconds",
            unit="s",
            description="Inbound request latency",
            explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS_SECONDS,
        ),
        in_flight=meter.create_up_down_counter(
            "http_requests_in_flight", description="Inbound requests currently being handled"
        ),
        dependency_duration=meter.create_histogram(
            "dependency_call_duration_seconds",
            unit="s",
            description="Outbound call latency",
            explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS_SECONDS,
        ),
        dependency_errors=meter.create_counter(
            "dependency_call_errors_total", description="Outbound calls that failed"
        ),
    )

    def _read_cpu(_options: Any):
        import psutil

        yield metrics.Observation(
            psutil.Process().cpu_percent(interval=None) / 100.0, {"service": config.service_name}
        )

    def _read_memory(_options: Any):
        import psutil

        yield metrics.Observation(
            psutil.Process().memory_info().rss, {"service": config.service_name}
        )

    meter.create_observable_gauge(
        "process_cpu_usage_ratio",
        callbacks=[_read_cpu],
        description="Process CPU usage as a 0-1 ratio (psutil)",
    )
    meter.create_observable_gauge(
        "process_memory_usage_bytes",
        callbacks=[_read_memory],
        description="Process resident memory in bytes (psutil)",
    )
    return meter, service_metrics


def _install_deployment_gauge(meter: metrics.Meter, config: ServiceConfig, chaos: Any) -> None:
    def _read_deployment_info(_options: Any):
        override = chaos.deployment_override() if chaos else None
        version = (override or {}).get("version", config.version)
        previous_version = (override or {}).get("previous_version", config.previous_version)
        yield metrics.Observation(
            1,
            {
                "service": config.service_name,
                "version": version,
                "previous_version": previous_version,
                "environment": config.environment,
            },
        )

    meter.create_observable_gauge(
        "service_deployment_info",
        callbacks=[_read_deployment_info],
        description="Always 1; version/previous_version carried as labels",
    )


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """One server span + one structured log line + metrics per request."""

    def __init__(self, app: ASGIApp, *, telemetry: ServiceTelemetry) -> None:
        super().__init__(app)
        self._telemetry = telemetry

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        config = self._telemetry.config
        service_metrics = self._telemetry.metrics
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)

        token = otel_context.attach(extract(request.headers))
        structlog.contextvars.bind_contextvars(request_id=request_id)
        log = get_logger("http")
        service_metrics.in_flight.add(1, {"service": config.service_name})
        start = time.monotonic()
        try:
            with self._telemetry.tracer.start_as_current_span(
                f"{request.method} {route_path}",
                kind=trace.SpanKind.SERVER,
                attributes={
                    "http.method": request.method,
                    "http.route": route_path,
                    "service.name": config.service_name,
                },
            ) as span:
                log.info("http.request.started", method=request.method, path=route_path)
                try:
                    response = await call_next(request)
                except Exception:
                    span.set_status(Status(StatusCode.ERROR))
                    log.exception("http.request.failed", method=request.method, path=route_path)
                    raise
                duration = time.monotonic() - start
                attrs = {
                    "service": config.service_name,
                    "method": request.method,
                    "route": route_path,
                    "status_code": str(response.status_code),
                }
                span.set_attribute("http.status_code", response.status_code)
                if response.status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                    service_metrics.error_count.add(1, attrs)
                service_metrics.request_count.add(1, attrs)
                service_metrics.request_duration.record(
                    duration,
                    {"service": config.service_name, "method": request.method, "route": route_path},
                )
                response.headers["x-request-id"] = request_id
                log.info(
                    "http.request.completed",
                    method=request.method,
                    path=route_path,
                    status_code=response.status_code,
                    duration_ms=round(duration * 1000, 2),
                )
                return response
        finally:
            service_metrics.in_flight.add(-1, {"service": config.service_name})
            structlog.contextvars.unbind_contextvars("request_id")
            otel_context.detach(token)


def install_observability(
    app: FastAPI, config: ServiceConfig, *, chaos: Any = None
) -> ServiceTelemetry:
    configure_logging(config)
    tracer = _setup_tracing(config)
    meter, service_metrics = _setup_metrics(config)
    _install_deployment_gauge(meter, config, chaos)
    telemetry = ServiceTelemetry(config=config, tracer=tracer, meter=meter, metrics=service_metrics)
    app.add_middleware(ObservabilityMiddleware, telemetry=telemetry)

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return telemetry


def jittered(base_seconds: float, spread: float = 0.2) -> float:
    """Small helper so simulated dependency latency isn't perfectly flat."""
    return max(0.0, base_seconds + random.uniform(-spread, spread) * base_seconds)
