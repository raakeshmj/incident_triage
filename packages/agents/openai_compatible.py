"""`OpenAICompatibleInvestigationModel`: any OpenAI-compatible Chat
Completions endpoint behind the provider-neutral `InvestigationModel`
interface. Configured by three values -- base URL, API key, model -- plus
optional, declared capabilities (`ChatCapabilities`); no provider is
special-cased here (OpenRouter is a preset: a fixed base URL).

The only module in `packages/agents` that makes HTTP calls
(`tests/unit/test_boundaries.py`). Like the Claude adapter it renders the
neutral transcript, makes ONE request per decision and maps the response
back; it does not validate, persist, retry, or decide anything:

- Request: `POST <base_url>/chat/completions` with model, messages, function
  tools, and only the parameters the capabilities declare (`tool_choice`,
  `parallel_tool_calls`, `strict`, a reasoning parameter, the max-tokens
  parameter name). Nothing undeclared is sent.
- Response: tool calls are read from the structured `tool_calls` field and
  their JSON arguments parsed (malformed JSON is flagged, never guessed).
  A truncated generation (`finish_reason` length) never yields actions; a
  failed one (`finish_reason` error) is a retryable model error.
- Errors are normalized to authentication, rate_limit, model_unavailable,
  bad_request, timeout, upstream_error, malformed_response -- with a bounded,
  scrubbed diagnostic (keys and bearer tokens removed, even when a provider
  echoes a partial key back).
- Retries and budgets belong to the engine (INVESTIGATION_MODEL_ATTEMPTS /
  backoff); the HTTP client makes no retries of its own.
- Hidden reasoning is never stored: reasoning fields are dropped and inline
  `<think>` blocks are stripped from the stored/replayed content; only
  reasoning token counts are kept.
- Prompt caching: OpenAI-compatible APIs cache automatically or not at all,
  so no hints are sent; reported cached tokens are recorded as-is.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from packages.agents.config import ChatCapabilities, ModelSpec
from packages.agents.model import (
    AssistantEntry,
    ContextEntry,
    DecisionRequest,
    ModelError,
    ModelTurn,
    ObservationEntry,
    ToolDefinition,
)
from packages.domain.investigation import ModelAction

_THINK = re.compile(r"<think>.*?</think>\s*", re.S | re.I)
_CREDENTIAL_SHAPES = (
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"\b(?:sk|pk|rk|gsk|xai)-[A-Za-z0-9_\-*.]{6,}"),
)
_TERMINAL = frozenset({"authentication", "model_unavailable", "bad_request"})


def visible_text(content: Any) -> str:
    """The model's visible output, without inline chain-of-thought."""
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return _THINK.sub("", content or "").strip() if isinstance(content, str) else ""


def render_messages(request: DecisionRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": request.system_prompt}]
    for entry in request.transcript:
        if isinstance(entry, ContextEntry):
            messages.append({"role": "user", "content": entry.text})
        elif isinstance(entry, AssistantEntry):
            stored = entry.provider_payload.get("message", {})
            message: dict[str, Any] = {"role": "assistant", "content": stored.get("content") or ""}
            if stored.get("tool_calls"):
                message["tool_calls"] = stored["tool_calls"]
            messages.append(message)
        elif isinstance(entry, ObservationEntry):
            messages += [
                {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
                for r in entry.results
            ]
            if entry.notices:
                messages.append({"role": "user", "content": "\n\n".join(entry.notices)})
    return messages


def strict_compatible(schema: Any) -> bool:
    """Whether a JSON schema satisfies strict structured outputs: every object
    closed (`additionalProperties: false`) with all properties required, and
    no keywords strict mode rejects. Anything else is sent without `strict`."""
    if isinstance(schema, list):
        return all(strict_compatible(s) for s in schema)
    if not isinstance(schema, dict):
        return True
    if any(
        k in schema
        for k in (
            "pattern",
            "format",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "minimum",
            "maximum",
            "patternProperties",
        )
    ):
        return False
    if schema.get("type") == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is not False:
            return False
        if set(schema.get("required") or []) != set(props):
            return False
    return all(strict_compatible(v) for v in schema.values() if isinstance(v, dict | list))


class OpenAICompatibleInvestigationModel:
    def __init__(
        self,
        spec: ModelSpec,
        *,
        api_key: str,
        base_url: str | None = None,
        client: httpx.Client | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        url = base_url or spec.base_url
        if not url:
            raise ValueError("an OpenAI-compatible model needs a base URL")
        self.spec = spec
        self.provider = spec.provider
        self.model_name = spec.model
        self.capabilities = ChatCapabilities.from_dict(spec.capabilities)
        self._url = url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._headers = {"Authorization": f"Bearer {api_key}", **(extra_headers or {})}
        # no transport retries: attempts and backoff are the engine's
        self._client = client or httpx.Client(timeout=spec.timeout_seconds)

    # --- request ---------------------------------------------------------------------

    def _tool(self, tool: ToolDefinition) -> dict[str, Any]:
        function: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        }
        if self.capabilities.strict_tools and strict_compatible(tool.input_schema):
            function["strict"] = True
        return {"type": "function", "function": function}

    def build_request(self, request: DecisionRequest) -> dict[str, Any]:
        caps = self.capabilities
        body: dict[str, Any] = {
            "model": self.spec.model,
            caps.max_tokens_param: self.spec.max_tokens,
            "messages": render_messages(request),
        }
        if caps.tools and request.tools:
            body["tools"] = [self._tool(t) for t in request.tools]
            if caps.tool_choice:
                body["tool_choice"] = "auto"
            if caps.parallel_tool_calls is not None:
                body["parallel_tool_calls"] = caps.parallel_tool_calls
        if self.spec.effort and caps.reasoning == "reasoning_effort":
            body["reasoning_effort"] = self.spec.effort
        elif self.spec.effort and caps.reasoning == "reasoning_object":
            body["reasoning"] = {"effort": self.spec.effort}
        return body

    # --- one decision ------------------------------------------------------------------

    def decide(self, request: DecisionRequest) -> ModelTurn:
        if not self.capabilities.tools:
            raise ModelError(
                "bad_request",
                "the endpoint is declared without tool calling; investigations need it",
                retryable=False,
            )
        started = time.monotonic()
        try:
            response = self._client.post(
                self._url, json=self.build_request(request), headers=self._headers
            )
        except httpx.TimeoutException as exc:
            raise ModelError(
                "timeout", f"request timed out ({type(exc).__name__})", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelError(
                "upstream_error", f"connection failed ({type(exc).__name__})", retryable=True
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            raise self._error(response.status_code, data if isinstance(data, dict) else {})
        if not isinstance(data, dict):
            raise ModelError(
                "malformed_response", "response body is not a JSON object", retryable=True
            )
        if data.get("error"):
            raise self._error(200, data)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ModelError("malformed_response", "no choices in the response", retryable=True)
        choice = choices[0]
        if choice.get("error"):
            raise self._error(502, {"error": choice["error"]})
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ModelError("malformed_response", "choice has no message", retryable=True)
        finish = choice.get("finish_reason")
        if finish == "error":
            # generation failed part-way: any tool call is truncated. Retry, never act.
            raise ModelError(
                "upstream_error",
                self._scrub(
                    f"generation failed upstream (provider={data.get('provider')}, "
                    f"native_finish_reason={choice.get('native_finish_reason')})"
                ),
                retryable=True,
            )
        if finish == "content_filter":
            raise ModelError("bad_request", "the provider filtered the response", retryable=False)

        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ModelError("malformed_response", "tool_calls is not a list", retryable=True)
        truncated = finish == "length"
        # A tool call cut off by max_tokens may carry partial input: never act on it.
        actions = (
            []
            if truncated
            else [
                ModelAction(
                    call_id=str(call.get("id") or f"call_{i}"),
                    name=str((call.get("function") or {}).get("name", "")),
                    arguments=parse_arguments((call.get("function") or {}).get("arguments")),
                )
                for i, call in enumerate(raw_calls)
                if isinstance(call, dict)
            ]
        )
        text = visible_text(message.get("content"))
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached = int(prompt_details.get("cached_tokens") or 0)
        written = int(prompt_details.get("cache_write_tokens") or 0)
        stop = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}.get(
            finish or "", "tool_use" if actions else "other"
        )
        stored: dict[str, Any] = {"content": text}
        if raw_calls and not truncated:
            stored["tool_calls"] = [
                {
                    "id": a.call_id,
                    "type": "function",
                    "function": {
                        "name": a.name,
                        "arguments": (c.get("function") or {}).get("arguments") or "{}",
                    },
                }
                for a, c in zip(actions, [c for c in raw_calls if isinstance(c, dict)], strict=True)
            ]
        return ModelTurn(
            text=text,
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
                "message": stored,
                "generation_id": data.get("id"),
                "upstream_provider": data.get("provider"),
                "finish_reason": finish,
                "native_finish_reason": choice.get("native_finish_reason"),
                "usage_reported": bool(usage),
                "cost": usage.get("cost"),
            },
            served_model=str(data.get("model") or self.spec.model),
            latency_ms=latency_ms,
            cache=self.cache_info(request, read=cached, written=written),
        )

    def cache_info(self, request: DecisionRequest, *, read: int, written: int) -> dict[str, Any]:
        caching = self.capabilities.prompt_caching
        return {
            "requested": False,
            "supported": caching,
            "strategy": "provider_automatic" if caching else "none",
            "note": "no cache hints are sent; cached tokens the provider reports are recorded",
            "prefix_digest": request.stable_prefix_digest(),
            "prefix_chars": request.stable_prefix_chars(),
            "read_tokens": read,
            "write_tokens": written,
            "hit": read > 0,
        }

    # --- errors ------------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        text = text.replace(self._api_key, "[REDACTED]") if self._api_key else text
        for shape in _CREDENTIAL_SHAPES:
            text = shape.sub("[REDACTED]", text)
        return text[:300]

    def _error(self, status: int, data: dict[str, Any]) -> ModelError:
        raw = data.get("error")
        error: dict[str, Any] = raw if isinstance(raw, dict) else {"message": raw} if raw else {}
        code = error.get("code", status)
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = status
        category = _category(code, str(error.get("type") or ""), str(error.get("code") or ""))
        raw_meta = error.get("metadata")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        summary = f"{category} http_{code}"
        if meta.get("provider_name"):
            summary += f" upstream={meta['provider_name']}"
        if error.get("message"):
            summary += f": {str(error['message'])[:200]}"
        # aggregators hide the real cause (e.g. an upstream rate limit) in metadata
        if meta.get("limit_source"):
            summary += f" limit_source={meta['limit_source']}"
        if isinstance(meta.get("raw"), str) and meta["raw"]:
            summary += f" ({meta['raw'][:160]})"
        return ModelError(category, self._scrub(summary), retryable=category not in _TERMINAL)


def _category(status: int, error_type: str, error_code: str) -> str:
    hint = f"{error_type} {error_code}".lower()
    if status in (401, 402, 403) or "auth" in hint or "api_key" in hint:
        return "authentication"
    if status == 429 or "rate_limit" in hint:
        return "rate_limit"
    if status == 404 or "model_not_found" in hint:
        return "model_unavailable"
    if status == 408:
        return "timeout"
    if status in (400, 413, 422) or "invalid_request" in hint:
        return "bad_request"
    return "upstream_error"


def parse_arguments(value: Any) -> dict[str, Any]:
    """Tool-call arguments as a dict; malformed JSON is flagged for the
    engine's validation to reject, never repaired or string-matched."""
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
