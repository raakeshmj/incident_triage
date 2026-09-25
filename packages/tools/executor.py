"""Runs tool calls for one investigation context.

- Arguments are validated against the tool's strict schema before anything
  runs; the incident comes from the bound `ToolContext`, never the call.
- Transient backend failures are retried once with backoff; the caller sees
  a clean success or a terminal `ToolFailure`, never an exception
  (07-agent-tool-architecture.md, "Retries within an investigation").
- Budgets (07, "Budgets"): a cap on total calls, and on identical calls
  (loop detection) -- exceeded budgets are tool errors, which is what lets
  a confused model degrade to "inconclusive" rather than loop.
- Decision tools (`update_hypotheses`, `conclude_investigation`,
  `declare_inconclusive`) are never executed here: the investigation engine
  and incident-core handle them.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from packages.evidence.errors import EvidenceError
from packages.evidence.hashing import canonical_json
from packages.evidence.service import EvidenceService
from packages.telemetry.logging import get_logger
from packages.telemetry.metrics import get_metrics
from packages.tools.contracts import (
    ToolContext,
    ToolErrorDetail,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)
from packages.tools.registry import DECISION_TOOL_SPECS, TOOLS

log = get_logger(__name__)
metrics = get_metrics()


@dataclass(frozen=True)
class ToolBudget:
    max_calls: int = 20
    max_identical_calls: int = 3
    max_attempts_per_call: int = 2
    retry_backoff_seconds: float = 0.5


class ToolExecutor:
    def __init__(
        self,
        service: EvidenceService,
        context: ToolContext,
        budget: ToolBudget | None = None,
        prior_calls: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        """`prior_calls` seeds the budgets from calls already made in this
        investigation (a resumed run keeps its loop detection)."""
        self._service = service
        self._context = context
        self._budget = budget or ToolBudget()
        self._calls = 0
        self._seen: Counter[str] = Counter()
        for tool, arguments in prior_calls or []:
            self._calls += 1
            self._seen[_call_key(tool, arguments)] += 1

    @property
    def calls_made(self) -> int:
        return self._calls

    def execute(self, tool: str, arguments: dict[str, Any] | None) -> ToolResult:
        arguments = arguments or {}
        spec = TOOLS.get(tool)
        if spec is None:
            if tool in DECISION_TOOL_SPECS:
                return _fail(
                    tool, "decision_tool", f"{tool} is handled by the investigation engine"
                )
            return _fail(tool, "unknown_tool", f"no tool named {tool!r}")

        if self._calls >= self._budget.max_calls:
            return _fail(
                tool, "budget_exceeded", f"tool call budget of {self._budget.max_calls} spent"
            )
        key = _call_key(tool, arguments)
        if self._seen[key] >= self._budget.max_identical_calls:
            return _fail(
                tool, "repeated_call", "this exact call was already made; try a different query"
            )
        self._calls += 1
        self._seen[key] += 1

        try:
            parsed = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            # Field locations and messages only -- `include_input=False` keeps
            # the offending value itself out of the error text.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)
            )
            return _fail(tool, "invalid_argument", problems)

        for attempt in range(1, self._budget.max_attempts_per_call + 1):
            try:
                item = spec.handler(self._service, self._context, parsed)
            except EvidenceError as exc:
                if exc.retryable and attempt < self._budget.max_attempts_per_call:
                    metrics.increment("tool.retry", tool=tool, code=exc.code)
                    time.sleep(self._budget.retry_backoff_seconds * attempt)
                    continue
                metrics.increment("tool.failed", tool=tool, code=exc.code)
                return _fail(tool, exc.code, str(exc), retryable=exc.retryable)
            metrics.increment("tool.succeeded", tool=tool)
            return ToolSuccess(
                tool=tool,
                evidence_id=item.evidence_id,
                content_hash=item.content_hash,
                evidence_type=item.evidence_type.value,
                source_system=item.source_system.value,
                subject_service=item.subject_service,
                observed_at=item.observed_at,
                summary=item.summary,
                data=item.data,
            )
        raise AssertionError("unreachable")  # pragma: no cover


def _call_key(tool: str, arguments: dict[str, Any]) -> str:
    return f"{tool}:{canonical_json(arguments)}"


def _fail(tool: str, code: str, message: str, *, retryable: bool = False) -> ToolFailure:
    log.info("tool.error", tool=tool, code=code)
    return ToolFailure(
        tool=tool, error=ToolErrorDetail(code=code, message=message, retryable=retryable)
    )
