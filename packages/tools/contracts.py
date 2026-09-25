"""Tool input/output schemas.

Inputs are strict (`extra="forbid"`): an argument a tool doesn't define is
an error, not something silently ignored -- a model can't smuggle in, say,
an `incident_id` or a `promql` field. Enumerations (metric names,
severities, trace search modes) are closed sets, so the JSON schema a
future agent sees already constrains query shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from packages.evidence import limits

MetricName = Literal[
    "request_rate",
    "error_rate",
    "latency_p50",
    "latency_p95",
    "latency_p99",
    "dependency_error_rate",
    "cpu_usage",
    "memory_usage",
    "availability",
]
Severity = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
TraceSearchMode = Literal["recent", "errors", "slow"]

_SERVICE = Field(
    default=None,
    description="Service to query; defaults to the incident's own service. Must be the "
    "incident's service or a direct dependency neighbor.",
    pattern=r"^[a-z0-9][a-z0-9-]{0,62}$",
)
_START = Field(default=None, description="Window start (ISO 8601, timezone-aware).")
_END = Field(default=None, description="Window end (ISO 8601, timezone-aware); defaults to now.")


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GetMetricsInput(ToolInput):
    metric: MetricName = Field(description="Allow-listed metric; no raw PromQL is accepted.")
    service: str | None = _SERVICE
    start: AwareDatetime | None = _START
    end: AwareDatetime | None = _END


class GetServiceHealthInput(ToolInput):
    service: str | None = _SERVICE
    at: AwareDatetime | None = Field(default=None, description="Instant to evaluate; default now.")


class GetLogsInput(ToolInput):
    service: str | None = _SERVICE
    start: AwareDatetime | None = _START
    end: AwareDatetime | None = _END
    severities: list[Severity] | None = Field(default=None, max_length=5)
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{16,32}$")
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F-]{8,64}$")
    limit: int = Field(default=10, ge=1, le=limits.MAX_LOG_LINES_RETURNED)


class GetTraceInput(ToolInput):
    trace_id: str = Field(pattern=r"^[0-9a-fA-F]{16,32}$")


class GetTracesInput(ToolInput):
    service: str | None = _SERVICE
    start: AwareDatetime | None = _START
    end: AwareDatetime | None = _END
    mode: TraceSearchMode = "recent"
    min_duration_ms: int | None = Field(default=None, ge=1, le=600_000)
    limit: int = Field(default=10, ge=1, le=limits.MAX_TRACES)


class GetDeploysInput(ToolInput):
    service: str | None = _SERVICE
    start: AwareDatetime | None = _START
    end: AwareDatetime | None = _END
    limit: int = Field(default=10, ge=1, le=limits.MAX_DEPLOYMENTS)


class GetConfigHistoryInput(ToolInput):
    service: str | None = _SERVICE
    start: AwareDatetime | None = _START
    end: AwareDatetime | None = _END
    limit: int = Field(default=10, ge=1, le=limits.MAX_CONFIG_CHANGES)


class GetGitDiffInput(ToolInput):
    service: str | None = _SERVICE
    start: AwareDatetime | None = _START
    end: AwareDatetime | None = _END
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    limit: int = Field(default=10, ge=1, le=limits.MAX_COMMITS)


class GetRecentCommitsInput(ToolInput):
    service: str | None = _SERVICE
    limit: int = Field(default=10, ge=1, le=limits.MAX_COMMITS)


class SearchHistoricalIncidentsInput(ToolInput):
    limit: int = Field(default=5, ge=1, le=limits.MAX_SIMILAR_INCIDENTS)


class ToolContext(BaseModel):
    """Bound by the orchestrator per investigation -- never by the model."""

    model_config = ConfigDict(frozen=True)

    incident_id: uuid.UUID
    investigation_id: uuid.UUID | None = None
    caller: str = "investigation-agent"


class ToolSuccess(BaseModel):
    """What goes back into a model's context: cite `evidence_id`."""

    model_config = ConfigDict(frozen=True)

    tool: str
    ok: Literal[True] = True
    evidence_id: uuid.UUID
    content_hash: str
    evidence_type: str
    source_system: str
    subject_service: str
    observed_at: datetime
    summary: str
    data: dict[str, Any]


class ToolErrorDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retryable: bool


class ToolFailure(BaseModel):
    """A clean, terminal tool error -- never a raw exception or backend text."""

    model_config = ConfigDict(frozen=True)

    tool: str
    ok: Literal[False] = False
    error: ToolErrorDetail


ToolResult = ToolSuccess | ToolFailure
