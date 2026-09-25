"""`ClaudeInvestigationModel`: the Anthropic Messages API behind the
provider-neutral `InvestigationModel` interface.

The only module in the codebase that imports the Anthropic SDK
(`tests/unit/test_boundaries.py` enforces it). It renders the neutral
transcript as Messages API turns, makes one `messages.create` call, and maps
the response back -- it does not validate, persist, or decide anything.

- Tools are the application's investigation tools plus the three decision
  tools; `tool_choice` stays `auto` (forced tool choice is rejected by
  some current models, and the prompt tells the model to act through tools).
- Adaptive thinking and effort are sent only when the model's profile says
  the API accepts them (packages/agents/config.py).
- Prompt caching (Anthropic's official `cache_control`): one explicit
  ephemeral breakpoint on the system block. Render order is tools -> system
  -> messages, so that caches exactly the stable prefix -- tool definitions
  plus the frozen system prompt -- shared by every iteration of every
  investigation. Nothing in `messages` is marked: incident context, tool
  results and notices are dynamic and are never cached. Below the model's
  minimum cacheable length the API silently doesn't cache; the usage fields
  (`cache_read_input_tokens` / `cache_creation_input_tokens`) are the only
  ground truth and are recorded on every turn.
- Assistant turns are replayed from the stored content blocks unchanged,
  thinking blocks included, as the API requires across tool use.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

from packages.agents.config import ModelSpec, profile_for
from packages.agents.model import (
    AssistantEntry,
    ContextEntry,
    DecisionRequest,
    ModelError,
    ModelTurn,
    ObservationEntry,
)
from packages.domain.investigation import ModelAction

_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)
_TERMINAL = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.BadRequestError,
    anthropic.UnprocessableEntityError,
    anthropic.RequestTooLargeError,
)


def _block_param(block: Any) -> dict[str, Any] | None:
    """Only the fields the API accepts back, per block type."""
    kind = block.type
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if kind == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    return None


def render_messages(request: DecisionRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for entry in request.transcript:
        if isinstance(entry, ContextEntry):
            messages.append({"role": "user", "content": entry.text})
        elif isinstance(entry, AssistantEntry):
            messages.append({"role": "assistant", "content": entry.provider_payload["content"]})
        elif isinstance(entry, ObservationEntry):
            content: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    **({"is_error": True} if r.is_error else {}),
                }
                for r in entry.results
            ]
            content += [{"type": "text", "text": notice} for notice in entry.notices]
            if content:
                messages.append({"role": "user", "content": content})
    return messages


class ClaudeInvestigationModel:
    provider = "anthropic"

    def __init__(
        self,
        spec: ModelSpec,
        client: anthropic.Anthropic | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        self.spec = spec
        self.model_name = spec.model
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=spec.timeout_seconds, max_retries=spec.max_retries
        )

    @property
    def caches_stable_prefix(self) -> bool:
        return self.spec.prompt_cache == "stable_prefix"

    def build_request(self, request: DecisionRequest) -> dict[str, Any]:
        system_block: dict[str, Any] = {"type": "text", "text": request.system_prompt}
        if self.caches_stable_prefix:
            system_block["cache_control"] = {"type": "ephemeral"}
        kwargs: dict[str, Any] = {
            "model": self.spec.model,
            "max_tokens": self.spec.max_tokens,
            "system": [system_block],
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ],
            "messages": render_messages(request),
        }
        if self.spec.thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}
        if self.spec.effort:
            kwargs["output_config"] = {"effort": self.spec.effort}
        return kwargs

    def decide(self, request: DecisionRequest) -> ModelTurn:
        started = time.monotonic()
        try:
            response = self._client.messages.create(**self.build_request(request))
        except _RETRYABLE as exc:
            raise ModelError(type(exc).__name__, _message(exc), retryable=True) from exc
        except _TERMINAL as exc:
            raise ModelError(type(exc).__name__, _message(exc), retryable=False) from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code == 429 or exc.status_code >= 500
            raise ModelError(f"http_{exc.status_code}", _message(exc), retryable=retryable) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.stop_reason == "refusal":
            raise ModelError("refusal", "the model declined the request", retryable=False)
        truncated = response.stop_reason == "max_tokens"
        content = [p for p in (_block_param(b) for b in response.content) if p is not None]
        text = "\n".join(b.text for b in response.content if b.type == "text")
        # A tool call cut off by max_tokens may carry partial input: never act on it.
        actions = (
            []
            if truncated
            else [
                ModelAction(call_id=b.id, name=b.name, arguments=_arguments(b.input))
                for b in response.content
                if b.type == "tool_use"
            ]
        )
        usage = response.usage
        stop = response.stop_reason
        read = usage.cache_read_input_tokens or 0
        written = usage.cache_creation_input_tokens or 0
        return ModelTurn(
            text=text,
            actions=actions,
            stop_reason=stop if stop in ("tool_use", "end_turn", "max_tokens") else "other",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": written,
            },
            provider_payload={
                "content": content,
                "request_id": getattr(response, "_request_id", None),
                "stop_reason": response.stop_reason,
            },
            served_model=response.model,
            latency_ms=latency_ms,
            cache=self.cache_info(request, read=read, written=written),
        )

    def cache_info(self, request: DecisionRequest, *, read: int, written: int) -> dict[str, Any]:
        profile = profile_for(self.spec.provider, self.spec.model)
        return {
            "requested": self.caches_stable_prefix,
            "supported": profile.prompt_caching,
            "strategy": "stable_prefix" if self.caches_stable_prefix else "none",
            "breakpoints": ["system"] if self.caches_stable_prefix else [],
            "prefix_digest": request.stable_prefix_digest(),
            "prefix_chars": request.stable_prefix_chars(),
            "min_cacheable_tokens": profile.min_cacheable_tokens,
            "read_tokens": read,
            "write_tokens": written,
            "hit": read > 0,
        }


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):  # defensive: always parse, never string-match
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"__unparseable__": value[:200]}
        return parsed if isinstance(parsed, dict) else {"__value__": parsed}
    return {"__value__": value}


def _message(exc: Exception) -> str:
    """Bounded summary for the trace: class, request id, and the API's own
    error type + message (e.g. an invalid parameter, or a billing problem) --
    without it a 400 is undiagnosable. Recorded on the MODEL_ERROR step,
    not logged."""
    request_id = getattr(getattr(exc, "response", None), "headers", {}).get("request-id", "")
    summary = f"{type(exc).__name__} request_id={request_id}"
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        summary += f" {error.get('type', '')}: {str(error.get('message', ''))[:200]}"
    return summary[:300]
