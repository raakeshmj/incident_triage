"""Controlled Loki adapter.

LogQL is built here from structured, validated filters -- a service (always
the stream selector, so every query is service-scoped), optional severities
from an allow-list, and optional trace/request ids matched by format. There
is no free-text filter parameter. Results are bounded twice: at most
`MAX_LOG_LINES_FETCHED` lines leave Loki (each truncated in the stored raw
record too), and the normalized view groups them into at most
`MAX_LOG_LINES_RETURNED` representative (severity, message) groups.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from datetime import datetime
from typing import Any

import httpx

from packages.evidence import limits
from packages.evidence.adapters._http import endpoint, from_unix, get_json, iso
from packages.evidence.errors import BackendUnavailableError, InvalidQueryError
from packages.evidence.models import Observation
from packages.evidence.sanitize import clean_text
from packages.evidence.types import EvidenceType, SourceSystem

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
ERROR_SEVERITIES = ("ERROR", "CRITICAL")
TRACE_ID = re.compile(r"^[0-9a-f]{16,32}$")
REQUEST_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
# Every structured (structlog JSON) line carries this key; plain-text lines
# (uvicorn's own access log) don't, so this keeps queries to structured logs.
_STRUCTURED_LINE_FILTER = '|= "\\"message\\""'
_KEEP_FIELDS = ("request_id", "trace_id", "span_id")
_DROP_FIELDS = {"service", "environment", "region", "timestamp", "severity", "message"}


def normalize_trace_id(trace_id: str) -> str:
    """Lowercase, 32-hex. Tempo strips leading zeros from ids it returns;
    the services log the full 32-character form."""
    value = trace_id.strip().lower()
    if not TRACE_ID.match(value):
        raise InvalidQueryError("trace_id must be 16-32 hex characters")
    return value.rjust(32, "0")


def build_logql(
    *,
    service: str,
    severities: tuple[str, ...] | None,
    trace_id: str | None,
    request_id: str | None,
) -> str:
    selector = f'service="{service}"'
    if severities:
        unknown = [s for s in severities if s not in SEVERITIES]
        if unknown:
            raise InvalidQueryError(f"unknown severities {unknown}; allowed: {SEVERITIES}")
        selector += f', severity=~"{"|".join(sorted(set(severities)))}"'
    query = "{" + selector + "} " + _STRUCTURED_LINE_FILTER
    if trace_id is not None:
        query += f' |= "{normalize_trace_id(trace_id)}"'
    if request_id is not None:
        if not REQUEST_ID.match(request_id):
            raise InvalidQueryError("request_id has an unexpected format")
        query += f' |= "{request_id}"'
    return query


def _parse_line(line: str) -> dict[str, Any]:
    try:
        parsed = json.loads(line)
    except ValueError:
        return {"severity": None, "message": clean_text(line, limits.MAX_LOG_MESSAGE_CHARS)}
    if not isinstance(parsed, dict):
        return {"severity": None, "message": clean_text(line, limits.MAX_LOG_MESSAGE_CHARS)}
    entry: dict[str, Any] = {
        "timestamp": parsed.get("timestamp"),
        "severity": parsed.get("severity"),
        "message": clean_text(str(parsed.get("message", "")), limits.MAX_LOG_MESSAGE_CHARS),
    }
    for key in _KEEP_FIELDS:
        if key in parsed:
            entry[key] = clean_text(str(parsed[key]), 64)
    fields = {
        clean_text(str(k), 64): clean_text(str(v), 200)
        for k, v in parsed.items()
        if k not in _DROP_FIELDS and k not in _KEEP_FIELDS
    }
    if fields:
        entry["fields"] = dict(sorted(fields.items())[:10])
    return entry


class LokiAdapter:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def _count(self, query: str, start: datetime, end: datetime) -> int:
        seconds = max(1, int((end - start).total_seconds()))
        response = get_json(
            self._client,
            "/loki/api/v1/query",
            params={
                "query": f"sum(count_over_time({query} [{seconds}s]))",
                "time": end.timestamp(),
            },
            source="loki",
        )
        result = response.get("data", {}).get("result", []) if response else []
        return int(float(result[0]["value"][1])) if result else 0

    def query_logs(
        self,
        *,
        service: str,
        start: datetime,
        end: datetime,
        severities: tuple[str, ...] | None,
        trace_id: str | None,
        request_id: str | None,
        limit: int,
    ) -> Observation:
        query = build_logql(
            service=service, severities=severities, trace_id=trace_id, request_id=request_id
        )
        error_query = build_logql(
            service=service, severities=ERROR_SEVERITIES, trace_id=trace_id, request_id=request_id
        )
        fetch_limit = limits.MAX_LOG_LINES_FETCHED
        response = get_json(
            self._client,
            "/loki/api/v1/query_range",
            params={
                "query": query,
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": fetch_limit,
                "direction": "backward",
            },
            source="loki",
        )
        if response.get("status") != "success":
            raise BackendUnavailableError("loki returned a non-success status")
        matching_total = self._count(query, start, end)
        error_total = self._count(error_query, start, end)

        # Bounded raw copy: every stream kept, each line truncated; the
        # timestamp-ordered flat list drives normalization.
        raw_streams: list[dict[str, Any]] = []
        lines: list[tuple[int, str]] = []
        truncated = False
        for stream in response["data"]["result"]:
            values = []
            for ts, line in stream["values"]:
                if len(line) > limits.MAX_LOG_LINE_CHARS:
                    truncated = True
                    line = line[: limits.MAX_LOG_LINE_CHARS]
                values.append([ts, line])
                lines.append((int(ts), line))
            raw_streams.append({"stream": stream["stream"], "values": values})
        lines.sort(key=lambda item: item[0], reverse=True)
        if len(lines) >= fetch_limit:
            truncated = True

        groups: OrderedDict[tuple[Any, str], dict[str, Any]] = OrderedDict()
        for _, line in lines:
            entry = _parse_line(line)
            key = (entry.get("severity"), entry["message"])
            group = groups.get(key)
            if group is None:
                groups[key] = {
                    "severity": entry.get("severity"),
                    "message": entry["message"],
                    "count": 1,
                    "last_seen": entry.get("timestamp"),
                    "first_seen": entry.get("timestamp"),
                    "example": entry,
                }
            else:
                group["count"] += 1
                group["first_seen"] = entry.get("timestamp")
        ranked = sorted(
            groups.values(),
            key=lambda g: (-g["count"], g["severity"] or "", g["message"]),
        )[: min(limit, limits.MAX_LOG_LINES_RETURNED)]
        severity_counts: dict[str, int] = {}
        for g in groups.values():
            severity_counts[g["severity"] or "UNSTRUCTURED"] = (
                severity_counts.get(g["severity"] or "UNSTRUCTURED", 0) + g["count"]
            )

        normalized = {
            "service": service,
            "window": {"start": iso(start), "end": iso(end)},
            "filters": {
                "severities": list(severities) if severities else None,
                "trace_id": normalize_trace_id(trace_id) if trace_id else None,
                "request_id": request_id,
            },
            "matching_lines_total": matching_total,
            "error_lines_total": error_total,
            "lines_fetched": len(lines),
            "fetch_truncated": truncated,
            "severity_counts_in_fetched": dict(sorted(severity_counts.items())),
            "representative": ranked,
        }
        top = f"; top: {ranked[0]['message']!r} x{ranked[0]['count']}" if ranked else ""
        observed = _latest_timestamp(lines) or end
        return Observation(
            evidence_type=EvidenceType.LOG,
            source_system=SourceSystem.LOKI,
            operation="logs",
            subject_service=service,
            query_spec={
                "template": "service_logs",
                "params": {
                    "service": service,
                    "severities": list(severities) if severities else None,
                    "trace_id": trace_id,
                    "request_id": request_id,
                    "limit": limit,
                },
                "logql": query,
                "error_count_logql": error_query,
                "fetch_limit": fetch_limit,
            },
            source_reference={
                "endpoint": endpoint(self._client, "/loki/api/v1/query_range"),
                "query": query,
                "window": [iso(start), iso(end)],
            },
            raw_response={
                "streams": raw_streams,
                "counts": {"matching": matching_total, "errors": error_total},
            },
            raw_truncated=truncated,
            normalized_payload=normalized,
            summary=(
                f"{matching_total} matching log lines for {service} "
                f"({error_total} ERROR/CRITICAL) in window{top}"
            ),
            result_count=len(lines),
            observed_at=observed,
            window_start=start,
            window_end=end,
        )


def _latest_timestamp(lines: list[tuple[int, str]]) -> datetime | None:
    if not lines:
        return None
    return from_unix(lines[0][0] / 1e9)
