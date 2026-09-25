"""Adapters against the real Prometheus, Loki and Tempo of the local stack
(`make infra-up-full`), with the load generator providing live traffic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from packages.evidence.adapters.loki import LokiAdapter
from packages.evidence.adapters.prometheus import PrometheusAdapter
from packages.evidence.adapters.tempo import TempoAdapter

pytestmark = pytest.mark.stack


def _window() -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    return end - timedelta(minutes=10), end


def test_prometheus_metric_window_and_health(stack_urls):
    adapter = PrometheusAdapter(httpx.Client(base_url=stack_urls["prometheus"], timeout=10))
    start, end = _window()
    window = adapter.metric_window(
        metric="request_rate",
        service="checkout-service",
        environment="production",
        start=start,
        end=end,
    )
    assert window.normalized_payload["current_value"] > 0  # the load generator is running
    assert window.normalized_payload["series"][0]["points"]
    assert window.raw_response["current"]["status"] == "success"

    health = adapter.service_health(service="checkout-service", environment="production", at=end)
    assert health.normalized_payload["signals"]["availability"]["value"] == 1.0
    assert health.normalized_payload["status"] in {"healthy", "degraded"}


def test_loki_logs_are_service_scoped_bounded_and_counted(stack_urls):
    adapter = LokiAdapter(httpx.Client(base_url=stack_urls["loki"], timeout=10))
    start, end = _window()
    obs = adapter.query_logs(
        service="checkout-service",
        start=start,
        end=end,
        severities=("INFO",),
        trace_id=None,
        request_id=None,
        limit=5,
    )
    data = obs.normalized_payload
    assert data["matching_lines_total"] > 0
    assert data["lines_fetched"] <= 500
    assert len(data["representative"]) <= 5
    assert all(g["severity"] == "INFO" for g in data["representative"])
    assert all(
        s["stream"].get("service") == "checkout-service" for s in obs.raw_response["streams"]
    )


def test_tempo_search_then_lookup_then_the_same_trace_in_loki(stack_urls):
    tempo = TempoAdapter(httpx.Client(base_url=stack_urls["tempo"], timeout=10))
    start, end = _window()
    search = tempo.search(
        service="checkout-service",
        start=start,
        end=end,
        mode="recent",
        min_duration_ms=None,
        limit=3,
    )
    assert search.result_count > 0
    trace_id = search.normalized_payload["traces"][0]["trace_id"]
    assert len(trace_id) == 32

    trace = tempo.get_trace(trace_id=trace_id)
    summary = trace.normalized_payload
    assert summary["found"]
    assert summary["root"]["service"] == "checkout-service"
    assert any(
        (e["from"], e["to"]) == ("checkout-service", "payment-service")
        for e in summary["service_edges"]
    )

    # Trace ids stay correlated with application logs: the same id finds the
    # request's own log lines.
    loki = LokiAdapter(httpx.Client(base_url=stack_urls["loki"], timeout=10))
    logs = loki.query_logs(
        service="checkout-service",
        start=start,
        end=end + timedelta(seconds=5),
        severities=None,
        trace_id=trace_id,
        request_id=None,
        limit=5,
    )
    assert logs.normalized_payload["matching_lines_total"] >= 1
    assert all(
        g["example"].get("trace_id") == trace_id for g in logs.normalized_payload["representative"]
    )
