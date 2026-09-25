"""The investigation model interface -- provider-neutral.

The engine talks to an `InvestigationModel`; it never imports a model SDK.
A model receives a `DecisionRequest` and returns a `ModelTurn`: its visible text and the raw,
*unvalidated* tool calls it made. Validation is the engine's job
(`packages.domain.investigation.interpret_turn`), never the adapter's.

A `DecisionRequest` is split by stability:

- **stable** -- `system_prompt` and `tools`: identical for every iteration of
  every investigation run with the same prompt version and criteria. This is
  the prefix a provider may cache; `stable_prefix_digest()` fingerprints it.
- **dynamic** -- `transcript`: the incident context, the model's own prior
  turns, tool results and engine notices, rebuilt from persisted steps every
  iteration. Never marked for caching by any adapter.

Prompt caching itself is provider-specific and lives in the adapter; the
engine only records the adapter's `ModelTurn.cache` metadata.

Implementations: `ClaudeInvestigationModel` (packages/agents/claude.py) and
`FakeInvestigationModel` (packages/agents/fake.py, deterministic, for tests).
"""

from __future__ import annotations

import hashlib
import json
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
    system_prompt: str  # stable
    tools: list[ToolDefinition]  # stable
    transcript: list[TranscriptEntry]  # dynamic

    def stable_prefix_digest(self) -> str:
        return stable_prefix_digest(self.system_prompt, self.tools)

    def stable_prefix_chars(self) -> int:
        return len(self.system_prompt) + len(_canonical_tools(self.tools))


def _canonical_tools(tools: list[ToolDefinition]) -> str:
    return json.dumps(
        [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_prefix_digest(system_prompt: str, tools: list[ToolDefinition]) -> str:
    """sha256 over the stable prefix: equal digests mean a byte-identical
    cacheable prefix, which is recorded per turn to explain cache hits/misses."""
    return hashlib.sha256((system_prompt + "\x00" + _canonical_tools(tools)).encode()).hexdigest()


def no_cache_info(reason: str = "provider does not support prompt caching") -> dict[str, Any]:
    return {"requested": False, "supported": False, "strategy": "none", "note": reason}


@dataclass(frozen=True)
class ModelTurn:
    text: str
    actions: list[ModelAction]
    stop_reason: Literal["tool_use", "end_turn", "max_tokens", "other"]
    usage: dict[str, int]
    provider_payload: dict[str, Any]
    served_model: str
    latency_ms: int
    # Provider-neutral prompt-cache metadata, recorded on the MODEL_TURN step:
    # requested, supported, strategy, prefix_digest, prefix_chars,
    # min_cacheable_tokens, read_tokens, write_tokens (when the provider
    # reports them). Empty/"supported": False for providers without caching.
    cache: dict[str, Any] = field(default_factory=no_cache_info)


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
