"""The investigation's tool surface: the Phase 4 read-only tool contracts,
under the names the model sees, plus the three decision tools.

    model tool call -> InvestigationToolset -> ToolExecutor (packages/tools)
                    -> EvidenceService -> adapter -> telemetry backend

Nothing here widens what a tool can do: every evidence tool is an existing
`packages.tools` contract, executed with a `ToolContext` whose incident and
investigation are bound by the engine -- the model never supplies them.
`get_incident_evidence` is the one addition: a read-only listing of evidence
already recorded for the incident (it creates no new evidence record).

Results are compacted before they reach the model: full raw responses stay in
the evidence store, addressable by `evidence_id`.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.agents.model import ToolDefinition
from packages.domain.investigation import DECISION_TOOLS
from packages.evidence.errors import EvidenceError
from packages.evidence.service import EvidenceService
from packages.tools.contracts import ToolContext, ToolSuccess
from packages.tools.executor import ToolBudget, ToolExecutor
from packages.tools.registry import DECISION_TOOL_SPECS, TOOLS

# model-facing name -> packages.tools contract name
EVIDENCE_TOOL_ALIASES: dict[str, str] = {
    "get_service_health": "get_service_health",
    "get_metric_window": "get_metrics",
    "get_logs": "get_logs",
    "get_traces": "get_traces",
    "get_trace": "get_trace",
    "get_recent_deployments": "get_deploys",
    "get_config_changes": "get_config_history",
    "get_code_changes": "get_git_diff",
    "get_recent_commits": "get_recent_commits",
    "search_similar_incidents": "search_historical_incidents",
}
GET_INCIDENT_EVIDENCE = "get_incident_evidence"
EVIDENCE_TOOL_NAMES = frozenset({*EVIDENCE_TOOL_ALIASES, GET_INCIDENT_EVIDENCE})

MAX_RESULT_CHARS = 6_000
MAX_LISTED_EVIDENCE = 40


class GetIncidentEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=20, ge=1, le=MAX_LISTED_EVIDENCE)


@dataclass
class ToolOutcome:
    ok: bool
    observation: str  # exactly what the model sees
    evidence_ids: list[str] = field(default_factory=list)
    new_evidence: int = 0  # evidence records this call created
    error_code: str | None = None
    summary: str | None = None
    latency_ms: int = 0


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve Pydantic `$defs`/`$ref` so each tool schema is self-contained."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = node["$ref"].split("/")[-1]
                return resolve(copy.deepcopy(defs[target]))
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def compact(value: Any, limit: int = MAX_RESULT_CHARS) -> str:
    """JSON for the model, shrunk to `limit` characters by trimming long
    lists first (keeping their head and a count), then hard-truncating."""
    text = json.dumps(value, default=str, separators=(",", ":"))
    keep = 8
    while len(text) > limit and keep >= 1:
        value = _trim_lists(value, keep)
        text = json.dumps(value, default=str, separators=(",", ":"))
        keep //= 2
    if len(text) > limit:
        text = text[: limit - 60] + '..."[truncated; full record in the evidence store]"'
    return text


def _is_series(node: list[Any]) -> bool:
    """[[timestamp, value], ...] -- a metric series."""
    return bool(node) and all(
        isinstance(p, list) and len(p) == 2 and isinstance(p[0], str) for p in node
    )


def _trim_lists(node: Any, keep: int) -> Any:
    if isinstance(node, list):
        if _is_series(node) and len(node) > keep:
            # Evenly spaced samples, first and last included: a head-only cut
            # would keep just the oldest points and hide when a change began.
            step = (len(node) - 1) / max(keep - 1, 1)
            picks = sorted({round(i * step) for i in range(keep)})
            return [node[i] for i in picks] + [f"... downsampled from {len(node)} points"]
        head = [_trim_lists(v, keep) for v in node[:keep]]
        return head + ([f"... {len(node) - keep} more"] if len(node) > keep else [])
    if isinstance(node, dict):
        return {k: _trim_lists(v, keep) for k, v in node.items()}
    return node


class InvestigationToolset:
    def __init__(
        self,
        evidence: EvidenceService,
        *,
        incident_id: uuid.UUID,
        investigation_id: uuid.UUID,
        max_tool_calls: int,
        max_identical_calls: int,
        prior_calls: list[tuple[str, dict[str, Any]]] | None = None,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        self._evidence = evidence
        self._incident_id = incident_id
        self._executor = ToolExecutor(
            evidence,
            ToolContext(
                incident_id=incident_id,
                investigation_id=investigation_id,
                caller=f"investigation:{investigation_id}",
            ),
            ToolBudget(
                max_calls=max_tool_calls,
                max_identical_calls=max_identical_calls,
                retry_backoff_seconds=retry_backoff_seconds,
            ),
            prior_calls=[(EVIDENCE_TOOL_ALIASES.get(t, t), a) for t, a in prior_calls or []],
        )

    @staticmethod
    def definitions() -> list[ToolDefinition]:
        tools = [
            ToolDefinition(
                name=alias,
                description=TOOLS[contract].description,
                input_schema=inline_refs(TOOLS[contract].input_model.model_json_schema()),
            )
            for alias, contract in EVIDENCE_TOOL_ALIASES.items()
        ]
        tools.append(
            ToolDefinition(
                name=GET_INCIDENT_EVIDENCE,
                description="List evidence already recorded for this incident (ids, types, "
                "summaries). Reads existing records; queries no backend.",
                input_schema=inline_refs(GetIncidentEvidenceInput.model_json_schema()),
            )
        )
        tools += [
            ToolDefinition(
                name=name,
                description=DECISION_TOOL_SPECS[name][0],
                input_schema=inline_refs(DECISION_TOOL_SPECS[name][1].model_json_schema()),
            )
            for name in DECISION_TOOLS
        ]
        return tools

    def execute(self, tool: str, arguments: dict[str, Any]) -> ToolOutcome:
        started = time.monotonic()
        if tool == GET_INCIDENT_EVIDENCE:
            outcome = self._incident_evidence(arguments)
        else:
            outcome = self._evidence_tool(tool, arguments)
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        return outcome

    def _evidence_tool(self, tool: str, arguments: dict[str, Any]) -> ToolOutcome:
        result = self._executor.execute(EVIDENCE_TOOL_ALIASES[tool], arguments)
        if isinstance(result, ToolSuccess):
            payload = {
                "evidence_id": str(result.evidence_id),
                "evidence_type": result.evidence_type,
                "source": result.source_system,
                "service": result.subject_service,
                "observed_at": result.observed_at.isoformat(),
                "summary": result.summary,
                "data": result.data,
            }
            return ToolOutcome(
                ok=True,
                observation=compact(payload),
                evidence_ids=[str(result.evidence_id)],
                new_evidence=1,
                summary=result.summary,
            )
        return ToolOutcome(
            ok=False,
            observation=compact({"error": result.error.model_dump()}),
            error_code=result.error.code,
            summary=result.error.message[:300],
        )

    def _incident_evidence(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            args = GetIncidentEvidenceInput.model_validate(arguments)
            records = self._evidence.get_incident_evidence(self._incident_id)
        except ValidationError as exc:
            return ToolOutcome(
                ok=False,
                observation=compact({"error": {"code": "invalid_argument", "message": str(exc)}}),
                error_code="invalid_argument",
            )
        except EvidenceError as exc:
            return ToolOutcome(
                ok=False,
                observation=compact({"error": {"code": exc.code, "message": str(exc)}}),
                error_code=exc.code,
            )
        listed = records[-args.limit :]
        items = [
            {
                "evidence_id": str(r.evidence_id),
                "evidence_type": r.evidence_type.value,
                "operation": r.operation,
                "service": r.subject_service,
                "observed_at": r.observed_at.isoformat(),
                "summary": r.summary[:300],
            }
            for r in listed
        ]
        return ToolOutcome(
            ok=True,
            observation=compact({"total_recorded": len(records), "evidence": items}),
            evidence_ids=[i["evidence_id"] for i in items],
            summary=f"{len(items)} of {len(records)} recorded evidence items listed",
        )
