"""Controlled Tempo adapter.

Two operations: look up one trace by id, and search a service's traces in a
window in one of three fixed modes (recent / errors / slow). TraceQL is
rendered from those templates -- never supplied by a caller. A trace comes
back summarized (span tree, service-to-service edges, error and slowest
spans), not dumped: docs/architecture/08-evidence-model.md, "Summarized span
tree stored, not every attribute" -- the raw response is still stored, span
count bounded.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from typing import Any

import httpx

from packages.evidence import limits
from packages.evidence.adapters._http import endpoint, from_unix, get_json, iso
from packages.evidence.adapters.loki import normalize_trace_id
from packages.evidence.errors import InvalidQueryError
from packages.evidence.models import Observation
from packages.evidence.sanitize import clean_text
from packages.evidence.types import EvidenceType, SourceSystem

SEARCH_MODES = ("recent", "errors", "slow")
MAX_RAW_SPANS = 1_000
# Tempo's search returns *some* `limit` matches, not the newest or slowest:
# over-fetch a bounded candidate set, then order and cut here.
SEARCH_CANDIDATES = 100
_HEX = re.compile(r"^[0-9a-f]+$")


def render_traceql(service: str, mode: str, min_duration_ms: int | None) -> str:
    base = f'resource.service.name = "{service}"'
    if mode == "recent":
        return "{ " + base + " }"
    if mode == "errors":
        return "{ " + base + " && status = error }"
    if mode == "slow":
        threshold = 500 if min_duration_ms is None else min_duration_ms
        if not 1 <= threshold <= 600_000:
            raise InvalidQueryError("min_duration_ms must be between 1 and 600000")
        return "{ " + base + f" && duration > {threshold}ms" + " }"
    raise InvalidQueryError(f"unknown trace search mode {mode!r}; allowed: {SEARCH_MODES}")


def _hex_id(value: str | None) -> str | None:
    if not value:
        return None
    if _HEX.match(value) and len(value) in (16, 32):
        return value
    try:
        return base64.b64decode(value).hex()
    except (binascii.Error, ValueError):
        return None


def _attr(attributes: list[dict[str, Any]], key: str) -> Any:
    for attribute in attributes:
        if attribute.get("key") == key:
            value = attribute.get("value", {})
            for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
                if kind in value:
                    return value[kind]
    return None


def _is_error(span: dict[str, Any]) -> bool:
    code = span.get("status", {}).get("code")
    return code in ("STATUS_CODE_ERROR", 2)


def _bounded_trace(trace: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    kept = 0
    truncated = False
    batches = []
    for batch in trace.get("batches", []):
        scopes = []
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            spans = scope.get("spans", [])
            room = MAX_RAW_SPANS - kept
            if len(spans) > room:
                truncated = True
                spans = spans[: max(room, 0)]
            kept += len(spans)
            scopes.append({**scope, "spans": spans})
        batches.append({**batch, "scopeSpans": scopes})
    return {"batches": batches}, truncated


def summarize_trace(trace_id: str, trace: dict[str, Any]) -> dict[str, Any]:
    spans: list[dict[str, Any]] = []
    for batch in trace.get("batches", []):
        service = _attr(batch.get("resource", {}).get("attributes", []), "service.name")
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            for span in scope.get("spans", []):
                start = int(span.get("startTimeUnixNano", 0))
                end = int(span.get("endTimeUnixNano", start))
                attributes = span.get("attributes", [])
                spans.append(
                    {
                        "span_id": _hex_id(span.get("spanId")),
                        "parent_span_id": _hex_id(span.get("parentSpanId")),
                        "service": service or _attr(attributes, "service.name"),
                        "name": clean_text(str(span.get("name", "")), 120),
                        "kind": str(span.get("kind", "")).removeprefix("SPAN_KIND_").lower(),
                        "start_ns": start,
                        "duration_ms": round((end - start) / 1e6, 3),
                        "error": _is_error(span),
                        "http_status": _attr(attributes, "http.status_code"),
                        "peer_service": _attr(attributes, "peer.service"),
                    }
                )
    if not spans:
        return {"trace_id": trace_id, "found": False, "span_count": 0}

    by_id = {s["span_id"]: s for s in spans if s["span_id"]}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for span in spans:
        parent = span["parent_span_id"] if span["parent_span_id"] in by_id else None
        children.setdefault(parent, []).append(span)
    for siblings in children.values():
        siblings.sort(key=lambda s: (s["start_ns"], s["span_id"] or ""))

    tree: list[dict[str, Any]] = []

    def walk(parent: str | None, depth: int) -> None:
        for span in children.get(parent, []):
            if len(tree) >= limits.MAX_SPANS_IN_SUMMARY:
                return
            tree.append({**{k: v for k, v in span.items() if k != "start_ns"}, "depth": depth})
            walk(span["span_id"], depth + 1)

    walk(None, 0)

    edges: dict[tuple[str, str], int] = {}
    for span in spans:
        parent = by_id.get(span["parent_span_id"])
        if (
            parent
            and parent["service"]
            and span["service"]
            and parent["service"] != span["service"]
        ):
            key = (parent["service"], span["service"])
            edges[key] = edges.get(key, 0) + 1

    roots = children.get(None, [])
    root = roots[0] if roots else spans[0]
    start = min(s["start_ns"] for s in spans)
    end = max(s["start_ns"] + s["duration_ms"] * 1e6 for s in spans)

    def brief(span: dict[str, Any]) -> dict[str, Any]:
        keys = ("span_id", "service", "name", "duration_ms", "http_status")
        return {k: span[k] for k in keys}

    return {
        "trace_id": trace_id,
        "found": True,
        "span_count": len(spans),
        "services": sorted({s["service"] for s in spans if s["service"]}),
        "root": {"service": root["service"], "name": root["name"]},
        "start": iso(from_unix(start / 1e9)),
        "duration_ms": round((end - start) / 1e6, 3),
        "error_span_count": sum(1 for s in spans if s["error"]),
        "error_spans": [brief(s) for s in spans if s["error"]][:10],
        "slowest_spans": [brief(s) for s in sorted(spans, key=lambda s: -s["duration_ms"])[:5]],
        "service_edges": [{"from": a, "to": b, "calls": n} for (a, b), n in sorted(edges.items())],
        "span_tree": tree,
        "span_tree_truncated": len(spans) > len(tree),
    }


class TempoAdapter:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get_trace(self, *, trace_id: str) -> Observation:
        normalized_id = normalize_trace_id(trace_id)
        path = f"/api/traces/{normalized_id}"
        trace = get_json(self._client, path, source="tempo", allow_not_found=True)
        raw, truncated = _bounded_trace(trace or {})
        summary = summarize_trace(normalized_id, trace or {})
        observed = (
            datetime.fromisoformat(summary["start"]) if summary.get("found") else from_unix(0)
        )
        text = (
            f"trace {normalized_id}: {summary['span_count']} spans across "
            f"{', '.join(summary['services'])}, {summary['duration_ms']}ms, "
            f"{summary['error_span_count']} error spans"
            if summary["found"]
            else f"trace {normalized_id} not found in Tempo"
        )
        return Observation(
            evidence_type=EvidenceType.TRACE,
            source_system=SourceSystem.TEMPO,
            operation="trace_by_id",
            subject_service=(summary.get("root") or {}).get("service") or "unknown",
            query_spec={"template": "trace_by_id", "params": {"trace_id": normalized_id}},
            source_reference={"endpoint": endpoint(self._client, path), "trace_id": normalized_id},
            raw_response=raw,
            raw_truncated=truncated,
            normalized_payload=summary,
            summary=text,
            result_count=summary["span_count"],
            observed_at=observed,
        )

    def search(
        self,
        *,
        service: str,
        start: datetime,
        end: datetime,
        mode: str,
        min_duration_ms: int | None,
        limit: int,
    ) -> Observation:
        traceql = render_traceql(service, mode, min_duration_ms)
        params = {
            "q": traceql,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "limit": SEARCH_CANDIDATES,
            "spss": 3,
        }
        response = get_json(self._client, "/api/search", params=params, source="tempo") or {}
        candidates = [
            {
                "trace_id": normalize_trace_id(t["traceID"]),
                "root_service": t.get("rootServiceName"),
                "root_name": clean_text(str(t.get("rootTraceName", "")), 120),
                "start": iso(from_unix(int(t.get("startTimeUnixNano", 0)) / 1e9)),
                "duration_ms": t.get("durationMs", 0),
                "matched_spans": (t.get("spanSet") or {}).get("matched", 0),
            }
            for t in response.get("traces", [])[:SEARCH_CANDIDATES]
        ]
        if mode == "slow":
            candidates.sort(key=lambda t: (-t["duration_ms"], t["trace_id"]))
        else:
            candidates.sort(key=lambda t: (t["start"], t["trace_id"]), reverse=True)
        traces = candidates[:limit]
        durations = sorted(t["duration_ms"] for t in traces)
        normalized = {
            "service": service,
            "mode": mode,
            "window": {"start": iso(start), "end": iso(end)},
            "trace_count": len(traces),
            "candidates_matched": len(candidates),
            "duration_ms": {
                "max": durations[-1] if durations else None,
                "median": durations[len(durations) // 2] if durations else None,
            },
            "traces": traces,
        }
        observed = datetime.fromisoformat(traces[0]["start"]) if traces and mode != "slow" else end
        return Observation(
            evidence_type=EvidenceType.TRACE,
            source_system=SourceSystem.TEMPO,
            operation=f"trace_search:{mode}",
            subject_service=service,
            query_spec={
                "template": f"trace_search_{mode}",
                "params": {
                    "service": service,
                    "mode": mode,
                    "min_duration_ms": min_duration_ms,
                    "limit": limit,
                },
                "traceql": traceql,
                "candidate_limit": SEARCH_CANDIDATES,
            },
            source_reference={
                "endpoint": endpoint(self._client, "/api/search"),
                "query": traceql,
                "window": [iso(start), iso(end)],
            },
            raw_response=response,
            raw_truncated=len(response.get("traces", [])) >= SEARCH_CANDIDATES,
            normalized_payload=normalized,
            summary=(
                f"{len(traces)} {mode} traces for {service} in window"
                + (f", max {durations[-1]}ms" if durations else "")
            ),
            result_count=len(traces),
            observed_at=observed,
            window_start=start,
            window_end=end,
        )
