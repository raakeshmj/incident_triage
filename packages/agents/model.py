"""The investigation model interface -- provider-neutral.

The engine talks to an `InvestigationModel`; it never imports a model SDK.
A model receives a `DecisionRequest` (system prompt, tool definitions, the
neutral transcript) and returns a `ModelTurn`: its visible text and the raw,
*unvalidated* tool calls it made. Validation is the engine's job
(`packages.domain.investigation.interpret_turn`), never the adapter's.

Implementations: `ClaudeInvestigationModel` (packages/agents/claude.py) and
`FakeInvestigationModel` (packages/agents/fake.py, deterministic, for tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from packages.domain.investigation import ModelAction


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ContextEntry:
    """The initial incident context (first user message)."""

    text: str


@dataclass(frozen=True)
class AssistantEntry:
    """A previous model turn, replayed exactly. `provider_payload` is opaque
    to everything except the adapter that produced it."""

    text: str
    actions: list[ModelAction]
    provider_payload: dict[str, Any]


@dataclass(frozen=True)
class ToolResultEntry:
    call_id: str
    content: str
    is_error: bool


@dataclass(frozen=True)
class ObservationEntry:
    """Everything the application says back after a model turn: one result
    per tool call it made, then any engine notices."""

    results: list[ToolResultEntry] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


TranscriptEntry = ContextEntry | AssistantEntry | ObservationEntry


@dataclass(frozen=True)
class DecisionRequest:
    system_prompt: str
    tools: list[ToolDefinition]
    transcript: list[TranscriptEntry]


@dataclass(frozen=True)
class ModelTurn:
    text: str
    actions: list[ModelAction]
    stop_reason: Literal["tool_use", "end_turn", "max_tokens", "other"]
    usage: dict[str, int]
    provider_payload: dict[str, Any]
    served_model: str
    latency_ms: int


class ModelError(Exception):
    """A model call failed. `retryable` decides whether the engine backs off
    and tries again or ends the investigation as FAILED."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class InvestigationModel(Protocol):
    provider: str
    model_name: str

    def decide(self, request: DecisionRequest) -> ModelTurn: ...
