"""`OpenRouterInvestigationModel`: OpenRouter's OpenAI-compatible Chat
Completions API behind the provider-neutral `InvestigationModel` interface.

The only module in `packages/agents` that makes HTTP calls
(`tests/unit/test_boundaries.py` enforces it). Like the Claude adapter it
renders the neutral transcript, makes one request, and maps the response
back -- it does not validate, persist, or decide anything.

- Tools are sent as `function` tools with `tool_choice: auto`; arguments come
  back as JSON strings and are parsed here, never string-matched.
- Assistant turns are replayed from the stored message (content, tool calls
  and any `reasoning_details`, which reasoning models need back across tool
  use).
- Prompt caching: no cache hints are sent (the model profile decides; unknown
  OpenRouter models run conservatively). Providers that cache implicitly
  report `prompt_tokens_details.cached_tokens`; that is recorded as-is.
- The API key is passed in by the factory and lives only in the request
  header: it is never part of the spec, the payload stored on a turn, or an
  error message.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

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

API_URL = "https://openrouter.ai/api/v1/chat/completions"
# Fields of an assistant message that are sent back on later turns.
_REPLAYED = ("content", "tool_calls", "reasoning_details")


def render_messages(request: DecisionRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": request.system_prompt}]
    for entry in request.transcript:
        if isinstance(entry, ContextEntry):
            messages.append({"role": "user", "content": entry.text})
        elif isinstance(entry, AssistantEntry):
            stored = entry.provider_payload.get("message", {})
            message = {"role": "assistant", **{k: stored[k] for k in _REPLAYED if k in stored}}
            message.setdefault("content", "")
            messages.append(message)
        elif isinstance(entry, ObservationEntry):
            messages += [
                {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
                for r in entry.results
            ]
            if entry.notices:
                messages.append({"role": "user", "content": "\n\n".join(entry.notices)})
    return messages


class OpenRouterInvestigationModel:
    provider = "openrouter"

    def __init__(
        self,
        spec: ModelSpec,
        *,
        api_key: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.spec = spec
        self.model_name = spec.model
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=spec.timeout_seconds)

    def build_request(self, request: DecisionRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.spec.model,
            "max_tokens": self.spec.max_tokens,
            "messages": render_messages(request),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ],
            "tool_choice": "auto",
        }
        if self.spec.effort:
            body["reasoning"] = {"effort": self.spec.effort}
        return body

    def decide(self, request: DecisionRequest) -> ModelTurn:
        started = time.monotonic()
        try:
            response = self._client.post(
                API_URL,
                json=self.build_request(request),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "X-Title": "Incident Intelligence",
                },
            )
        except httpx.TimeoutException as exc:
            raise ModelError("timeout", type(exc).__name__, retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ModelError("connection", type(exc).__name__, retryable=True) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or "error" in data:
            raise _error(response.status_code, data)
        choices = data.get("choices") or []
        if not choices:
            raise ModelError("empty_response", "no choices in the response", retryable=True)
        choice = choices[0]
        if choice.get("error"):
            raise _error(502, {"error": choice["error"]})
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")
        if finish == "error":
            # The upstream generation failed part-way: whatever tool call came
            # back is truncated. A model failure to retry, never a turn to act on.
            native = choice.get("native_finish_reason")
            raise ModelError(
                "upstream_generation_error",
                f"openrouter: generation failed upstream (provider={data.get('provider')}, "
                f"native_finish_reason={native})"[:300],
                retryable=True,
            )
        truncated = finish == "length"
        # A tool call cut off by max_tokens may carry partial input: never act on it.
        actions = (
            []
            if truncated
            else [
                ModelAction(
                    call_id=call.get("id") or f"call_{i}",
                    name=(call.get("function") or {}).get("name", ""),
                    arguments=_arguments((call.get("function") or {}).get("arguments")),
                )
                for i, call in enumerate(message.get("tool_calls") or [])
            ]
        )
        usage = data.get("usage") or {}
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        written = int((usage.get("prompt_tokens_details") or {}).get("cache_write_tokens") or 0)
        stop = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}.get(
            finish or "", "other"
        )
        return ModelTurn(
            text=message.get("content") or "",
            actions=actions,
            stop_reason=stop,  # type: ignore[arg-type]
            usage={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": written,
                "reasoning_tokens": int(
                    (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
                ),
            },
            provider_payload={
                "message": {k: message[k] for k in _REPLAYED if message.get(k) is not None},
                "generation_id": data.get("id"),
                "upstream_provider": data.get("provider"),
                "finish_reason": finish,
                "native_finish_reason": choice.get("native_finish_reason"),
                "cost": usage.get("cost"),
            },
            served_model=data.get("model") or self.spec.model,
            latency_ms=latency_ms,
            cache=self.cache_info(request, read=cached, written=written),
        )

    def cache_info(self, request: DecisionRequest, *, read: int, written: int) -> dict[str, Any]:
        profile = profile_for(self.spec.provider, self.spec.model)
        return {
            "requested": False,
            "supported": profile.prompt_caching,
            "strategy": "none",
            "note": "no cache hints sent; provider-side implicit caching is recorded if reported",
            "prefix_digest": request.stable_prefix_digest(),
            "prefix_chars": request.stable_prefix_chars(),
            "read_tokens": read,
            "write_tokens": written,
            "hit": read > 0,
        }


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"__unparseable__": value[:200]}
        return parsed if isinstance(parsed, dict) else {"__value__": parsed}
    return {} if value is None else {"__value__": value}


def _error(status: int, data: dict[str, Any]) -> ModelError:
    """Bounded, credential-free summary for the MODEL_ERROR step."""
    raw = data.get("error")
    error: dict[str, Any] = raw if isinstance(raw, dict) else {}
    code = error.get("code", status)
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = status
    message = str(error.get("message") or "")[:200]
    raw_meta = error.get("metadata")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    upstream = meta.get("provider_name")
    summary = f"openrouter http_{code}" + (f" upstream={upstream}" if upstream else "")
    summary = f"{summary}: {message}" if message else summary
    # OpenRouter's generic "Provider returned error" hides the cause (e.g. an
    # upstream free-tier rate limit) in metadata.raw / limit_source.
    if meta.get("limit_source"):
        summary += f" limit_source={meta['limit_source']}"
    raw = meta.get("raw")
    if isinstance(raw, str) and raw:
        summary += f" ({raw[:160]})"
    retryable = code in (408, 429) or code >= 500
    return ModelError(f"http_{code}", summary[:300], retryable=retryable)
