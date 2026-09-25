"""The tool inventory from docs/architecture/07-agent-tool-architecture.md,
each mapped onto one evidence-service operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from packages.evidence.models import EvidenceItem
from packages.evidence.service import EvidenceService
from packages.tools import contracts as c
from packages.tools.findings import InvestigationResult

Handler = Callable[[EvidenceService, c.ToolContext, Any], EvidenceItem]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[c.ToolInput]
    handler: Handler


def _common(ctx: c.ToolContext, name: str) -> dict[str, Any]:
    return {"requested_by": f"tool:{name}", "investigation_id": ctx.investigation_id}


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec(
            "get_metrics",
            "A service metric over a time window: compact series, current value, and a "
            "baseline comparison against the preceding window of equal length.",
            c.GetMetricsInput,
            lambda s, ctx, a: s.get_metric_window(
                ctx.incident_id,
                metric=a.metric,
                service=a.service,
                start=a.start,
                end=a.end,
                **_common(ctx, "get_metrics"),
            ),
        ),
        ToolSpec(
            "get_service_health",
            "Point-in-time health of a service (availability, error rate, p95 latency, "
            "request rate, CPU, memory) classified healthy/degraded/down.",
            c.GetServiceHealthInput,
            lambda s, ctx, a: s.get_service_health(
                ctx.incident_id, service=a.service, at=a.at, **_common(ctx, "get_service_health")
            ),
        ),
        ToolSpec(
            "get_logs",
            "Representative log entries (grouped by message) and error counts for a service "
            "in a window, optionally filtered by severity, trace id, or request id.",
            c.GetLogsInput,
            lambda s, ctx, a: s.get_logs(
                ctx.incident_id,
                service=a.service,
                start=a.start,
                end=a.end,
                severities=tuple(a.severities) if a.severities else None,
                trace_id=a.trace_id,
                request_id=a.request_id,
                limit=a.limit,
                **_common(ctx, "get_logs"),
            ),
        ),
        ToolSpec(
            "get_trace",
            "One trace summarized: span tree, service-to-service edges, error and slowest spans.",
            c.GetTraceInput,
            lambda s, ctx, a: s.get_trace(
                ctx.incident_id, trace_id=a.trace_id, **_common(ctx, "get_trace")
            ),
        ),
        ToolSpec(
            "get_traces",
            "Traces for a service in a window: most recent, erroring, or slower than a threshold.",
            c.GetTracesInput,
            lambda s, ctx, a: s.get_traces(
                ctx.incident_id,
                service=a.service,
                start=a.start,
                end=a.end,
                mode=a.mode,
                min_duration_ms=a.min_duration_ms,
                limit=a.limit,
                **_common(ctx, "get_traces"),
            ),
        ),
        ToolSpec(
            "get_deploys",
            "The service's current deployment and every deployment/rollback in a window.",
            c.GetDeploysInput,
            lambda s, ctx, a: s.get_recent_deployments(
                ctx.incident_id,
                service=a.service,
                start=a.start,
                end=a.end,
                limit=a.limit,
                **_common(ctx, "get_deploys"),
            ),
        ),
        ToolSpec(
            "get_config_history",
            "The service's effective configuration and every config change in a window.",
            c.GetConfigHistoryInput,
            lambda s, ctx, a: s.get_config_changes(
                ctx.incident_id,
                service=a.service,
                start=a.start,
                end=a.end,
                limit=a.limit,
                **_common(ctx, "get_config_history"),
            ),
        ),
        ToolSpec(
            "get_git_diff",
            "Commits touching the service's code in a window (or one commit by sha): "
            "metadata, changed files, and a diff summary -- not the patch itself.",
            c.GetGitDiffInput,
            lambda s, ctx, a: s.get_code_changes(
                ctx.incident_id,
                service=a.service,
                start=a.start,
                end=a.end,
                sha=a.sha,
                limit=a.limit,
                **_common(ctx, "get_git_diff"),
            ),
        ),
        ToolSpec(
            "get_recent_commits",
            "The most recent commits touching the service's code, each with its distance "
            "from the incident's start.",
            c.GetRecentCommitsInput,
            lambda s, ctx, a: s.get_recent_commits(
                ctx.incident_id,
                service=a.service,
                limit=a.limit,
                **_common(ctx, "get_recent_commits"),
            ),
        ),
        ToolSpec(
            "search_historical_incidents",
            "Past incidents most similar to this one on structured fields (service, "
            "environment, region, alert types, recency), with the score breakdown.",
            c.SearchHistoricalIncidentsInput,
            lambda s, ctx, a: s.search_similar_incidents(
                ctx.incident_id, limit=a.limit, **_common(ctx, "search_historical_incidents")
            ),
        ),
    ]
}

TERMINAL_TOOL = "submit_findings"
TERMINAL_TOOL_DESCRIPTION = (
    "End the investigation with ranked, evidence-cited hypotheses (cite evidence_id values "
    "returned by other tools) and either a selected root cause or an inconclusive reason."
)


def tool_definitions() -> list[dict[str, Any]]:
    """Name / description / JSON Schema for every tool, including the
    terminal `submit_findings` -- the shape a model-facing tool list takes.
    Not handed to any model in Phase 4."""
    definitions = [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_model.model_json_schema(),
        }
        for spec in TOOLS.values()
    ]
    definitions.append(
        {
            "name": TERMINAL_TOOL,
            "description": TERMINAL_TOOL_DESCRIPTION,
            "input_schema": InvestigationResult.model_json_schema(),
        }
    )
    return definitions
