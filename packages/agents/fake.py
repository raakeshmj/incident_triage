"""`FakeInvestigationModel`: a deterministic, scripted `InvestigationModel`.

The default test suite never calls a real model API. A script is a list of
steps, consumed one per `decide` call; each step is either
- a `ModelTurn` (returned as-is),
- a callable `(DecisionRequest) -> ModelTurn` (to react to what tools
  returned -- e.g. cite an evidence id that came back), or
- an exception instance (raised: API failures, timeouts, rate limits).
Every request it receives is kept in `requests`, so tests can assert on
exactly what a model would have been shown.
"""

from __future__ import annotations

import itertools
import json
import uuid
from collections.abc import Callable
from typing import Any

from packages.agents.model import (
    DecisionRequest,
    ModelTurn,
    ObservationEntry,
)
from packages.domain.investigation import ModelAction

ScriptStep = ModelTurn | Callable[[DecisionRequest], ModelTurn] | Exception

_ids = itertools.count(1)


def turn(*calls: tuple[str, dict[str, Any]], text: str = "", stop: str = "tool_use") -> ModelTurn:
    """Build a turn from (tool name, arguments) pairs."""
    actions = [ModelAction(call_id=f"call_{next(_ids)}", name=n, arguments=a) for n, a in calls]
    return ModelTurn(
        text=text,
        actions=actions,
        stop_reason=stop if actions or stop != "tool_use" else "end_turn",  # type: ignore[arg-type]
        usage={"input_tokens": 1000, "output_tokens": 200},
        provider_payload={
            "content": [{"type": "text", "text": text}]
            + [
                {"type": "tool_use", "id": a.call_id, "name": a.name, "input": a.arguments}
                for a in actions
            ]
        },
        served_model="fake-model",
        latency_ms=5,
    )


def last_results(request: DecisionRequest) -> list[dict[str, Any]]:
    """Parsed tool results from the most recent observation entry."""
    for entry in reversed(request.transcript):
        if isinstance(entry, ObservationEntry) and entry.results:
            out = []
            for r in entry.results:
                try:
                    out.append(json.loads(r.content))
                except ValueError:
                    out.append({"text": r.content, "is_error": r.is_error})
            return out
    return []


def evidence_ids(request: DecisionRequest, evidence_type: str | None = None) -> list[str]:
    """Every evidence id any tool has returned so far (optionally by type)."""
    ids: list[str] = []
    for entry in request.transcript:
        if not isinstance(entry, ObservationEntry):
            continue
        for r in entry.results:
            try:
                data = json.loads(r.content)
            except ValueError:
                continue
            if (
                isinstance(data, dict)
                and "evidence_id" in data
                and (evidence_type is None or data.get("evidence_type") == evidence_type)
            ):
                ids.append(data["evidence_id"])
    return ids


class FakeInvestigationModel:
    provider = "fake"

    def __init__(self, script: list[ScriptStep], model_name: str = "fake-model") -> None:
        self.model_name = model_name
        self._script = list(script)
        self.requests: list[DecisionRequest] = []

    def decide(self, request: DecisionRequest) -> ModelTurn:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("FakeInvestigationModel script exhausted")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, ModelTurn):
            return step
        return step(request)

    @property
    def remaining(self) -> int:
        return len(self._script)


def fake_uuid() -> str:
    return str(uuid.uuid4())
