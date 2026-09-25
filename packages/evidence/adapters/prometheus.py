"""Controlled Prometheus adapter.

No caller ever supplies PromQL. A caller names a metric from `METRICS` (an
allow-list of templates over the simulated services' own instrumentation,
simulator/services/common/telemetry.py) and a scope-validated service; the
adapter renders the query. docs/architecture/08-evidence-model.md: "Allow-
listed PromQL templates + parameters (not arbitrary PromQL)".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from packages.evidence import limits
from packages.evidence.adapters._http import endpoint, from_unix, get_json, iso
from packages.evidence.errors import BackendUnavailableError, InvalidQueryError
from packages.evidence.models import Observation
from packages.evidence.types import EvidenceType, SourceSystem

_RATE = "1m"


@dataclass(frozen=True)
class MetricTemplate:
    name: str
    promql: str  # `{sel}` is replaced with the scoped label selector
    unit: str
    description: str


def _latency(quantile: str) -> str:
    return (
        f"histogram_quantile({quantile}, sum by (le) "
        f"(rate(http_request_duration_seconds_bucket{{sel}}[{_RATE}])))"
    )


METRICS: dict[str, MetricTemplate] = {
    t.name: t
    for t in [
        MetricTemplate(
            "request_rate",
            f"sum(rate(http_requests_total{{sel}}[{_RATE}]))",
            "req/s",
            "Inbound request rate",
        ),
        # `(errors or total * 0) / total`: a service that has never returned
        # a 5xx has no error-counter series at all, and a plain ratio would
        # then read as "no data" instead of 0%.
        MetricTemplate(
            "error_rate",
            f"(sum(rate(http_requests_errors_total{{sel}}[{_RATE}])) "
            f"or sum(rate(http_requests_total{{sel}}[{_RATE}])) * 0) "
            f"/ sum(rate(http_requests_total{{sel}}[{_RATE}]))",
            "ratio",
            "Share of inbound requests answered with 5xx",
        ),
        MetricTemplate("latency_p50", _latency("0.5"), "s", "Median request latency"),
        MetricTemplate("latency_p95", _latency("0.95"), "s", "p95 request latency"),
        MetricTemplate("latency_p99", _latency("0.99"), "s", "p99 request latency"),
        MetricTemplate(
            "dependency_error_rate",
            f"(sum by (dependency) (rate(dependency_call_errors_total{{sel}}[{_RATE}])) "
            f"or sum by (dependency) "
            f"(rate(dependency_call_duration_seconds_count{{sel}}[{_RATE}])) * 0) "
            f"/ sum by (dependency) "
            f"(rate(dependency_call_duration_seconds_count{{sel}}[{_RATE}]))",
            "ratio",
            "Share of outbound calls to each dependency that failed",
        ),
        MetricTemplate(
            "cpu_usage", "max(process_cpu_usage_ratio{sel})", "ratio", "Process CPU usage"
        ),
        MetricTemplate(
            "memory_usage",
            "max(process_memory_usage_bytes{sel})",
            "bytes",
            "Process resident memory",
        ),
        MetricTemplate("availability", "max(up{sel})", "bool", "Scrape target up (1) or down (0)"),
    ]
}

LATENCY_METRICS = {"0.5": "latency_p50", "0.95": "latency_p95", "0.99": "latency_p99"}

# Same thresholds as infrastructure/prometheus/alerts/service-alerts.yml, so
# "degraded" here means exactly what would have alerted.
HEALTH_THRESHOLDS: dict[str, float] = {
    "error_rate": 0.1,
    "latency_p95": 1.0,
    "cpu_usage": 0.85,
    "memory_usage": 200 * 1024 * 1024,
}
HEALTH_SIGNALS = ("availability", "error_rate", "latency_p95", "request_rate", "cpu_usage")


def render(metric: str, service: str, environment: str) -> str:
    template = METRICS.get(metric)
    if template is None:
        raise InvalidQueryError(f"unknown metric {metric!r}; allowed: {sorted(METRICS)}")
    selector = f'{{service="{service}",environment="{environment}"}}'
    return template.promql.replace("{sel}", selector)


def _value(raw: str) -> float | None:
    number = float(raw)
    return None if math.isnan(number) or math.isinf(number) else number


def _series_labels(metric: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in sorted(metric.items()) if k != "__name__"}


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None, "avg": None, "first": None, "last": None}
    return {
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "first": values[0],
        "last": values[-1],
    }


def _matrix(response: dict[str, Any]) -> list[dict[str, Any]]:
    if response.get("status") != "success":
        raise BackendUnavailableError("prometheus returned a non-success status")
    return list(response["data"]["result"])


def summarize_range(response: dict[str, Any]) -> tuple[list[dict[str, Any]], list[float]]:
    """Compact series + the flat list of all non-null values (for averages)."""
    series_out: list[dict[str, Any]] = []
    all_values: list[float] = []
    for series in _matrix(response)[: limits.MAX_SERIES]:
        points = [(float(ts), _value(v)) for ts, v in series["values"]]
        stride = max(1, math.ceil(len(points) / limits.MAX_POINTS_PER_SERIES))
        values = [v for _, v in points if v is not None]
        all_values.extend(values)
        series_out.append(
            {
                "labels": _series_labels(series["metric"]),
                "points": [[iso(from_unix(ts)), v] for ts, v in points[::stride]],
                "stats": _stats(values),
            }
        )
    return series_out, all_values


def instant_value(response: dict[str, Any]) -> float | None:
    result = _matrix(response)
    if not result:
        return None
    values = [_value(series["value"][1]) for series in result]
    present = [v for v in values if v is not None]
    return max(present) if present else None


class PrometheusAdapter:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def _range(self, query: str, start: datetime, end: datetime, step: int) -> dict[str, Any]:
        params = {"query": query, "start": start.timestamp(), "end": end.timestamp(), "step": step}
        return get_json(self._client, "/api/v1/query_range", params=params, source="prometheus")

    def _instant(self, query: str, at: datetime) -> dict[str, Any]:
        params = {"query": query, "time": at.timestamp()}
        return get_json(self._client, "/api/v1/query", params=params, source="prometheus")

    def metric_window(
        self, *, metric: str, service: str, environment: str, start: datetime, end: datetime
    ) -> Observation:
        query = render(metric, service, environment)
        length = end - start
        step = max(15, math.ceil(length.total_seconds() / limits.MAX_POINTS_PER_SERIES))
        baseline_start, baseline_end = start - length, start

        current = self._range(query, start, end, step)
        baseline = self._range(query, baseline_start, baseline_end, step)
        instant = self._instant(query, end)

        series, current_values = summarize_range(current)
        _, baseline_values = summarize_range(baseline)
        current_avg = _stats(current_values)["avg"]
        baseline_avg = _stats(baseline_values)["avg"]
        current_value = instant_value(instant)
        comparison = _compare(baseline_avg, current_avg)
        labels: dict[str, list[str]] = {}
        for s in series:
            for key, value in s["labels"].items():
                labels.setdefault(key, [])
                if value not in labels[key]:
                    labels[key].append(value)

        template = METRICS[metric]
        normalized = {
            "metric": metric,
            "unit": template.unit,
            "description": template.description,
            "service": service,
            "window": {"start": iso(start), "end": iso(end), "step_seconds": step},
            "current_value": current_value,
            "window_stats": _stats(current_values),
            "baseline": {
                "window": {"start": iso(baseline_start), "end": iso(baseline_end)},
                "avg": baseline_avg,
            },
            "comparison": comparison,
            "relevant_labels": labels,
            "series": series,
        }
        summary = (
            f"{metric} for {service}: current={_fmt(current_value, template.unit)}, "
            f"window avg={_fmt(current_avg, template.unit)} vs baseline avg="
            f"{_fmt(baseline_avg, template.unit)} ({comparison['direction']})"
        )
        return Observation(
            evidence_type=EvidenceType.METRIC,
            source_system=SourceSystem.PROMETHEUS,
            operation=f"metric_window:{metric}",
            subject_service=service,
            query_spec={
                "template": metric,
                "params": {"service": service, "environment": environment},
                "promql": query,
                "step_seconds": step,
            },
            source_reference={
                "endpoint": endpoint(self._client, "/api/v1/query_range"),
                "query": query,
                "windows": {
                    "current": [iso(start), iso(end)],
                    "baseline": [iso(baseline_start), iso(baseline_end)],
                    "instant_at": iso(end),
                },
            },
            raw_response={"current": current, "baseline": baseline, "instant": instant},
            raw_truncated=len(_matrix(current)) > limits.MAX_SERIES,
            normalized_payload=normalized,
            summary=summary,
            result_count=len(series),
            observed_at=end,
            window_start=start,
            window_end=end,
        )

    def service_health(self, *, service: str, environment: str, at: datetime) -> Observation:
        queries = {
            name: render(name, service, environment) for name in (*HEALTH_SIGNALS, "memory_usage")
        }
        raw = {name: self._instant(query, at) for name, query in queries.items()}
        signals = {name: instant_value(response) for name, response in raw.items()}

        violations = [
            name
            for name, threshold in HEALTH_THRESHOLDS.items()
            if signals.get(name) is not None and signals[name] > threshold  # type: ignore[operator]
        ]
        if signals["availability"] is None or signals["availability"] == 0:
            status = "down"
        elif violations:
            status = "degraded"
        else:
            status = "healthy"

        normalized = {
            "service": service,
            "at": iso(at),
            "status": status,
            "signals": {
                name: {"value": value, "unit": METRICS[name].unit}
                for name, value in signals.items()
            },
            "thresholds": HEALTH_THRESHOLDS,
            "violations": violations,
        }
        detail = ", ".join(violations) if violations else "no threshold violations"
        return Observation(
            evidence_type=EvidenceType.METRIC,
            source_system=SourceSystem.PROMETHEUS,
            operation="service_health",
            subject_service=service,
            query_spec={
                "template": "service_health",
                "params": {"service": service, "environment": environment},
                "promql": queries,
            },
            source_reference={
                "endpoint": endpoint(self._client, "/api/v1/query"),
                "instant_at": iso(at),
            },
            raw_response=raw,
            raw_truncated=False,
            normalized_payload=normalized,
            summary=f"{service} is {status} at {iso(at)} ({detail})",
            result_count=sum(1 for v in signals.values() if v is not None),
            observed_at=at,
            window_start=at - timedelta(minutes=1),
            window_end=at,
        )


def _compare(baseline: float | None, current: float | None) -> dict[str, Any]:
    if baseline is None or current is None:
        return {"direction": "unknown", "delta": None, "ratio": None}
    delta = current - baseline
    ratio = (current / baseline) if baseline not in (0, 0.0) else None
    if abs(delta) <= max(abs(baseline) * 0.1, 1e-9):
        direction = "flat"
    else:
        direction = "up" if delta > 0 else "down"
    return {"direction": direction, "delta": delta, "ratio": ratio}


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "ratio":
        return f"{value * 100:.2f}%"
    if unit == "s":
        return f"{value * 1000:.1f}ms"
    if unit == "bytes":
        return f"{value / 1024 / 1024:.1f}MB"
    return f"{value:.3g} {unit}"
