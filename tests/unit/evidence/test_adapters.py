"""Adapter behavior against canned backend responses (no live backends)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from packages.evidence import limits
from packages.evidence.adapters.history import score_candidate
from packages.evidence.adapters.loki import LokiAdapter, build_logql, normalize_trace_id
from packages.evidence.adapters.prometheus import PrometheusAdapter, render, summarize_range
from packages.evidence.adapters.tempo import TempoAdapter, render_traceql, summarize_trace
from packages.evidence.errors import BackendTimeoutError, BackendUnavailableError, InvalidQueryError
from packages.evidence.types import EvidenceType, SourceSystem

END = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
START = END - timedelta(minutes=30)


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://backend", transport=httpx.MockTransport(handler))


# --- Prometheus -------------------------------------------------------------


def _matrix(values: list[tuple[float, str]], labels: dict[str, str] | None = None) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": labels or {}, "values": [[t, v] for t, v in values]}],
        },
    }


def test_render_only_accepts_allow_listed_metrics():
    assert 'service="checkout-service",environment="production"' in render(
        "request_rate", "checkout-service", "production"
    )
    with pytest.raises(InvalidQueryError):
        render("sum(rate(anything[5m]))", "checkout-service", "production")


def test_summarize_range_downsamples_and_skips_nan():
    points = [(1_000 + i, "NaN" if i == 0 else str(i)) for i in range(200)]
    series, values = summarize_range(_matrix(points, {"__name__": "x", "service": "s"}))
    assert len(series[0]["points"]) <= limits.MAX_POINTS_PER_SERIES
    assert series[0]["labels"] == {"service": "s"}
    assert series[0]["points"][0][1] is None
    assert min(values) == 1.0 and series[0]["stats"]["max"] == 199.0


def test_metric_window_returns_current_baseline_and_instant_in_one_observation():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("query_range"):
            start = float(request.url.params["start"])
            value = "0.3" if start >= START.timestamp() else "0.01"  # current vs baseline window
            return httpx.Response(200, json=_matrix([(start, value), (start + 60, value)]))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "0.25"]}]},
            },
        )

    obs = PrometheusAdapter(_client(handler)).metric_window(
        metric="error_rate",
        service="checkout-service",
        environment="production",
        start=START,
        end=END,
    )
    assert seen.count("/api/v1/query_range") == 2 and seen.count("/api/v1/query") == 1
    assert obs.evidence_type is EvidenceType.METRIC and obs.source_system is SourceSystem.PROMETHEUS
    assert obs.normalized_payload["current_value"] == 0.25
    assert obs.normalized_payload["baseline"]["avg"] == pytest.approx(0.01)
    assert obs.normalized_payload["comparison"]["direction"] == "up"
    assert set(obs.raw_response) == {"current", "baseline", "instant"}
    assert "promql" in obs.query_spec and obs.query_spec["template"] == "error_rate"


def test_service_health_classifies_with_alert_rule_thresholds():
    values = {"up": "1", "http_requests_errors_total": "0.4"}

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        value = next((v for k, v in values.items() if k in query), "0.001")
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, value]}]},
            },
        )

    obs = PrometheusAdapter(_client(handler)).service_health(
        service="checkout-service", environment="production", at=END
    )
    assert obs.normalized_payload["status"] == "degraded"
    assert obs.normalized_payload["violations"] == ["error_rate"]

    values["up"] = "0"
    down = PrometheusAdapter(_client(handler)).service_health(
        service="checkout-service", environment="production", at=END
    )
    assert down.normalized_payload["status"] == "down"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.TimeoutException("slow"), BackendTimeoutError),
        (httpx.Response(503), BackendUnavailableError),
        (httpx.Response(400, text="bad_data: parse error near 'secret'"), InvalidQueryError),
    ],
)
def test_backend_failures_map_to_stable_errors_without_backend_text(response, error):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    with pytest.raises(error) as excinfo:
        PrometheusAdapter(_client(handler)).service_health(
            service="checkout-service", environment="production", at=END
        )
    assert "secret" not in str(excinfo.value)


# --- Loki -------------------------------------------------------------------


def test_logql_is_built_from_structured_filters_only():
    query = build_logql(
        service="checkout-service",
        severities=("ERROR", "WARNING"),
        trace_id="3d4e432cd5c8cb124cd6e9e304ee2cc",
        request_id="144bbad2-13b6-43c5-b769-eecca77700d5",
    )
    assert query.startswith('{service="checkout-service", severity=~"ERROR|WARNING"}')
    assert '|= "03d4e432cd5c8cb124cd6e9e304ee2cc"' in query  # Tempo-style id, zero-padded
    for bad in (
        {"severities": ("PANIC",)},
        {"trace_id": 'abc" |~ ".*'},
        {"request_id": 'x" or 1=1'},
    ):
        kwargs = {"severities": None, "trace_id": None, "request_id": None, **bad}
        with pytest.raises(InvalidQueryError):
            build_logql(service="checkout-service", **kwargs)


def test_normalize_trace_id_pads_to_32_hex():
    assert normalize_trace_id("ABC0123456789DEF") == "0" * 16 + "abc0123456789def"


def test_query_logs_groups_bounds_and_sanitizes():
    lines = [
        json.dumps({"severity": "ERROR", "message": "checkout.failed_chaos", "order_id": str(i)})
        for i in range(30)
    ] + [
        json.dumps({"severity": "INFO", "message": "\x1b[31mhttp.request.completed\x1b[0m"}),
        "x" * (limits.MAX_LOG_LINE_CHARS + 500),  # an oversized, unstructured line
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("query_range"):
            values = [[str(1_790_000_000_000_000_000 + i), line] for i, line in enumerate(lines)]
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "result": [{"stream": {"service": "checkout-service"}, "values": values}]
                    },
                },
            )
        count = "30" if "ERROR" in request.url.params["query"] else "32"
        return httpx.Response(200, json={"data": {"result": [{"value": [1, count]}]}})

    obs = LokiAdapter(_client(handler)).query_logs(
        service="checkout-service",
        start=START,
        end=END,
        severities=None,
        trace_id=None,
        request_id=None,
        limit=5,
    )
    data = obs.normalized_payload
    assert data["matching_lines_total"] == 32 and data["error_lines_total"] == 30
    assert data["representative"][0]["message"] == "checkout.failed_chaos"
    assert data["representative"][0]["count"] == 30
    assert any(g["message"] == "http.request.completed" for g in data["representative"])
    assert obs.raw_truncated
    assert all(
        len(line) <= limits.MAX_LOG_LINE_CHARS
        for stream in obs.raw_response["streams"]
        for _, line in stream["values"]
    )


# --- Tempo ------------------------------------------------------------------


def _b64(hex_id: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_id)).decode()


def _span(span_id: str, parent: str | None, name: str, start: int, dur_ms: int, error=False):
    span = {
        "spanId": _b64(span_id),
        "name": name,
        "kind": "SPAN_KIND_SERVER",
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + dur_ms * 1_000_000),
        "attributes": [
            {"key": "http.status_code", "value": {"intValue": "500" if error else "200"}}
        ],
        "status": {"code": "STATUS_CODE_ERROR"} if error else {},
    }
    if parent:
        span["parentSpanId"] = _b64(parent)
    return span


def _batch(service: str, spans: list[dict]) -> dict:
    return {
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"scope": {}, "spans": spans}],
    }


TRACE = {
    "batches": [
        _batch("checkout-service", [_span("a" * 16, None, "POST /checkout", 1_000, 30)]),
        _batch("payment-service", [_span("b" * 16, "a" * 16, "POST /payments/charge", 2_000, 25)]),
        _batch(
            "inventory-service",
            [_span("c" * 16, "b" * 16, "POST /inventory/reserve", 3_000, 20, error=True)],
        ),
    ]
}


def test_summarize_trace_builds_tree_edges_and_error_spans():
    summary = summarize_trace("f" * 32, TRACE)
    assert summary["found"] and summary["span_count"] == 3
    assert summary["services"] == ["checkout-service", "inventory-service", "payment-service"]
    assert summary["root"] == {"service": "checkout-service", "name": "POST /checkout"}
    assert [s["depth"] for s in summary["span_tree"]] == [0, 1, 2]
    assert summary["service_edges"] == [
        {"from": "checkout-service", "to": "payment-service", "calls": 1},
        {"from": "payment-service", "to": "inventory-service", "calls": 1},
    ]
    assert summary["error_span_count"] == 1
    assert summary["error_spans"][0]["service"] == "inventory-service"


def test_trace_not_found_is_recorded_as_absence_not_an_error():
    obs = TempoAdapter(_client(lambda r: httpx.Response(404))).get_trace(trace_id="ab" * 16)
    assert obs.normalized_payload == {"trace_id": "ab" * 16, "found": False, "span_count": 0}
    assert obs.result_count == 0


def test_trace_search_orders_candidates_before_cutting_to_the_limit():
    """Tempo returns *some* matches, not the newest: the adapter over-fetches,
    orders, then cuts -- otherwise "recent" can silently mean "old"."""
    starts = [5, 1, 9, 3, 7]  # Tempo's arbitrary order
    traces = [
        {"traceID": f"{i:032x}", "startTimeUnixNano": str(s * 10**9), "durationMs": s}
        for i, s in enumerate(starts, start=1)
    ]
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["limit"] = request.url.params["limit"]
        return httpx.Response(200, json={"traces": traces})

    adapter = TempoAdapter(_client(handler))
    recent = adapter.search(
        service="checkout-service",
        start=START,
        end=END,
        mode="recent",
        min_duration_ms=None,
        limit=2,
    )
    assert int(seen["limit"]) > 2
    assert [t["duration_ms"] for t in recent.normalized_payload["traces"]] == [9, 7]
    slow = adapter.search(
        service="checkout-service", start=START, end=END, mode="slow", min_duration_ms=1, limit=2
    )
    assert [t["duration_ms"] for t in slow.normalized_payload["traces"]] == [9, 7]
    assert recent.normalized_payload["candidates_matched"] == 5


def test_traceql_comes_from_fixed_templates():
    assert render_traceql("checkout-service", "errors", None) == (
        '{ resource.service.name = "checkout-service" && status = error }'
    )
    assert "duration > 750ms" in render_traceql("checkout-service", "slow", 750)
    with pytest.raises(InvalidQueryError):
        render_traceql("checkout-service", "anything", None)


# --- history scoring --------------------------------------------------------


def test_similarity_score_is_deterministic_and_explained():
    from packages.domain.views import IncidentSummary

    candidate = IncidentSummary(
        id="00000000-0000-0000-0000-000000000001",
        status="CANCELLED",
        severity="critical",
        service="checkout-service",
        environment="production",
        regions=("us-east-1",),
        alert_types=("HighErrorRate", "HighP95Latency"),
        created_at=END - timedelta(days=3),
        closed_at=END - timedelta(days=3) + timedelta(minutes=9),
    )
    score, signals = score_candidate(
        service="checkout-service",
        environment="production",
        regions=("us-east-1",),
        alert_types=("HighErrorRate",),
        reference_time=END,
        candidate=candidate,
    )
    assert signals == {
        "same_service": 0.4,
        "alert_type_overlap": 0.125,
        "same_environment": 0.15,
        "region_overlap": 0.1,
        "recency": 0.09,
    }
    assert score == pytest.approx(sum(signals.values()))
