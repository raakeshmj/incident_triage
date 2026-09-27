"""The generic OpenAI-compatible provider over a mocked transport: any base
URL / model / key is configuration, only declared capabilities are sent,
responses and errors normalize to the neutral types, and credentials never
leave the Authorization header."""

from __future__ import annotations

import json

import httpx
import pytest

from packages.agents.config import (
    InvestigationSettings,
    ModelConfigError,
    ModelSpec,
    missing_credentials,
    resolve_model_spec,
)
from packages.agents.factory import build_investigation_model
from packages.agents.model import ContextEntry, DecisionRequest, ModelError, ToolDefinition
from packages.agents.openai_compatible import (
    OpenAICompatibleInvestigationModel,
    strict_compatible,
    visible_text,
)

KEY = "sk-test-" + "Q" * 40
LOOSE = ToolDefinition(
    "get_logs",
    "logs",
    {"type": "object", "properties": {"service": {"type": "string"}, "limit": {"type": "integer"}}},
)
STRICT = ToolDefinition(
    "ping",
    "ping",
    {
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
        "additionalProperties": False,
    },
)
REQUEST = DecisionRequest("SYSTEM", [LOOSE, STRICT], [ContextEntry("incident")])


def _spec(**caps) -> ModelSpec:
    return ModelSpec(
        provider="openai_compatible",
        model="vendor/any-model",
        thinking="none",
        effort=caps.pop("effort", None),
        max_tokens=2048,
        timeout_seconds=5,
        max_retries=0,
        base_url=caps.pop("base_url", "https://llm.example.test/v1"),
        capabilities=caps or None,
    )


def _model(handler, **caps) -> OpenAICompatibleInvestigationModel:
    return OpenAICompatibleInvestigationModel(
        _spec(**caps), api_key=KEY, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _reply(message: dict, finish: str = "tool_calls", **extra) -> dict:
    return {
        "id": "gen-9",
        "model": "vendor/any-model",
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 7},
        **extra,
    }


def _call(name: str, args: str, i: int = 1) -> dict:
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": args}}


def _capture(response: dict):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response)

    return seen, handler


# --- configuration only ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "model", "key"),
    [
        ("https://api.one.example/v1", "one/model-a", "sk-one-" + "a" * 30),
        ("https://gateway.two.example/openai/v2/", "two:model-b:free", "key-two-" + "b" * 30),
        ("http://localhost:11434/v1", "llama3.3", "local-" + "c" * 20),
    ],
)
def test_base_url_model_and_key_are_configuration_only(monkeypatch, base_url, model, key):
    monkeypatch.chdir("/")  # never the project's .env
    monkeypatch.setenv("INVESTIGATION_PROVIDER", "openai_compatible")
    monkeypatch.setenv("INVESTIGATION_BASE_URL", base_url)
    monkeypatch.setenv("INVESTIGATION_MODEL", model)
    monkeypatch.setenv("INVESTIGATION_API_KEY", key)
    spec = resolve_model_spec(InvestigationSettings(_env_file=None))  # type: ignore[call-arg]
    assert spec.base_url == base_url.rstrip("/") and spec.model == model
    assert key not in json.dumps(spec.settings())  # the persisted settings carry no key
    built = build_investigation_model(spec)
    seen, handler = _capture(_reply({"content": "ok"}, "stop"))
    built._client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
    built.decide(REQUEST)
    assert seen["url"] == base_url.rstrip("/") + "/chat/completions"
    assert seen["auth"] == f"Bearer {key}"
    assert seen["body"]["model"] == model
    restored = ModelSpec.from_persisted(spec.provider, spec.model, spec.settings())
    assert restored == spec  # a resumed investigation reaches the same endpoint


def test_missing_configuration_is_a_clean_error(monkeypatch):
    monkeypatch.chdir("/")
    monkeypatch.setenv("INVESTIGATION_PROVIDER", "openai_compatible")
    monkeypatch.delenv("INVESTIGATION_BASE_URL", raising=False)
    monkeypatch.delenv("INVESTIGATION_API_KEY", raising=False)
    with pytest.raises(ModelConfigError, match="INVESTIGATION_BASE_URL"):
        resolve_model_spec(InvestigationSettings(_env_file=None))  # type: ignore[call-arg]
    assert missing_credentials("openai_compatible") == "INVESTIGATION_API_KEY"
    with pytest.raises(ModelConfigError, match="INVESTIGATION_API_KEY"):
        build_investigation_model(_spec())


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@llm.example/v1",
        "https://llm.example/v1?api_key=abc",
        "http://llm.example/v1",
        "ftp://llm.example/v1",
    ],
)
def test_base_urls_that_would_persist_secrets_or_leak_are_refused(monkeypatch, url):
    monkeypatch.setenv("INVESTIGATION_PROVIDER", "openai_compatible")
    monkeypatch.setenv("INVESTIGATION_BASE_URL", url)
    with pytest.raises(ModelConfigError):
        resolve_model_spec(InvestigationSettings(_env_file=None))  # type: ignore[call-arg]


# --- request shape: only declared capabilities -------------------------------------------


def test_default_capabilities_send_tools_and_tool_choice_only():
    seen, handler = _capture(_reply({"content": "ok"}, "stop"))
    _model(handler, effort="high").decide(REQUEST)
    body = seen["body"]
    assert body["tool_choice"] == "auto" and len(body["tools"]) == 2
    assert body["max_tokens"] == 2048
    for absent in ("reasoning_effort", "reasoning", "parallel_tool_calls", "max_completion_tokens"):
        assert absent not in body
    assert all("strict" not in t["function"] for t in body["tools"])
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}


def test_unsupported_capabilities_are_omitted_and_declared_ones_sent():
    seen, handler = _capture(_reply({"content": "ok"}, "stop"))
    _model(
        handler,
        tool_choice=False,
        parallel_tool_calls=False,
        max_tokens_param="max_completion_tokens",
    ).decide(REQUEST)
    body = seen["body"]
    assert "tool_choice" not in body and body["parallel_tool_calls"] is False
    assert body["max_completion_tokens"] == 2048 and "max_tokens" not in body


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [
        ("none", {}),
        ("reasoning_effort", {"reasoning_effort": "low"}),
        ("reasoning_object", {"reasoning": {"effort": "low"}}),
    ],
)
def test_reasoning_is_sent_only_when_declared(reasoning, expected):
    seen, handler = _capture(_reply({"content": "ok"}, "stop"))
    _model(handler, reasoning=reasoning, effort="low").decide(REQUEST)
    got = {k: seen["body"][k] for k in ("reasoning_effort", "reasoning") if k in seen["body"]}
    assert got == expected


def test_effort_is_dropped_at_configuration_time_without_a_reasoning_parameter(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_PROVIDER", "openai_compatible")
    monkeypatch.setenv("INVESTIGATION_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("INVESTIGATION_EFFORT", "high")
    assert resolve_model_spec(InvestigationSettings(_env_file=None)).effort is None  # type: ignore[call-arg]
    monkeypatch.setenv("INVESTIGATION_REASONING", "reasoning_effort")
    assert resolve_model_spec(InvestigationSettings(_env_file=None)).effort == "high"  # type: ignore[call-arg]


def test_strict_structured_output_only_for_compatible_schemas():
    seen, handler = _capture(_reply({"content": "ok"}, "stop"))
    _model(handler, strict_tools=True).decide(REQUEST)
    strict = {t["function"]["name"]: t["function"].get("strict") for t in seen["body"]["tools"]}
    assert strict == {"get_logs": None, "ping": True}
    assert strict_compatible(STRICT.input_schema) and not strict_compatible(LOOSE.input_schema)
    assert not strict_compatible(
        {**STRICT.input_schema, "properties": {"t": {"type": "string", "format": "uuid"}}}
    )


# --- response normalization ---------------------------------------------------------------


def test_multiple_tool_calls_normalize_to_neutral_actions():
    calls = [_call("get_logs", '{"service": "svc"}', 1), _call("ping", '{"target": "db"}', 2)]
    turn = _model(
        lambda r: httpx.Response(200, json=_reply({"content": None, "tool_calls": calls}))
    ).decide(REQUEST)
    assert [(a.call_id, a.name, a.arguments) for a in turn.actions] == [
        ("c1", "get_logs", {"service": "svc"}),
        ("c2", "ping", {"target": "db"}),
    ]
    assert turn.stop_reason == "tool_use" and turn.usage["input_tokens"] == 50
    assert [c["id"] for c in turn.provider_payload["message"]["tool_calls"]] == ["c1", "c2"]


def test_text_only_malformed_and_truncated_calls():
    text = _model(lambda r: httpx.Response(200, json=_reply({"content": "done"}, "stop"))).decide(
        REQUEST
    )
    assert text.actions == [] and text.text == "done" and text.stop_reason == "end_turn"
    bad = _model(
        lambda r: httpx.Response(200, json=_reply({"tool_calls": [_call("ping", '{"target": ')]}))
    ).decide(REQUEST)
    assert "__unparseable__" in bad.actions[0].arguments  # flagged for the engine to reject
    cut = _model(
        lambda r: httpx.Response(200, json=_reply({"tool_calls": [_call("ping", "{}")]}, "length"))
    ).decide(REQUEST)
    assert cut.actions == [] and cut.stop_reason == "max_tokens"


def test_hidden_reasoning_is_never_stored():
    message = {
        "content": "<think>secret plan</think>Checking logs.",
        "reasoning": "long hidden chain of thought",
        "reasoning_details": [{"type": "reasoning.text", "text": "more"}],
        "tool_calls": [_call("ping", '{"target": "db"}')],
    }
    turn = _model(lambda r: httpx.Response(200, json=_reply(message))).decide(REQUEST)
    stored = json.dumps(turn.provider_payload)
    assert turn.text == "Checking logs." and "secret plan" not in stored
    assert "chain of thought" not in stored and "reasoning_details" not in stored
    assert visible_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"


def test_usage_and_cached_tokens_are_recorded_when_reported():
    reply = _reply({"content": "ok"}, "stop")
    reply["usage"] = {
        "prompt_tokens": 900,
        "completion_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 640},
        "completion_tokens_details": {"reasoning_tokens": 12},
    }
    turn = _model(lambda r: httpx.Response(200, json=reply), prompt_caching=True).decide(REQUEST)
    assert turn.usage["cache_read_input_tokens"] == 640 and turn.usage["reasoning_tokens"] == 12
    assert turn.cache["read_tokens"] == 640 and turn.cache["requested"] is False
    assert turn.cache["strategy"] == "provider_automatic"
    no_usage = _reply({"content": "ok"}, "stop")
    del no_usage["usage"]
    turn = _model(lambda r: httpx.Response(200, json=no_usage)).decide(REQUEST)
    assert turn.usage["input_tokens"] == 0 and turn.provider_payload["usage_reported"] is False


# --- errors ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "category", "retryable"),
    [
        (
            401,
            {"error": {"message": f"Incorrect API key provided: {KEY[:12]}****{KEY[-4:]}"}},
            "authentication",
            False,
        ),
        (401, {"error": {"message": f"bad key {KEY}"}}, "authentication", False),
        (403, {"error": {"message": "forbidden"}}, "authentication", False),
        (
            404,
            {"error": {"message": "model not found", "code": "model_not_found"}},
            "model_unavailable",
            False,
        ),
        (
            400,
            {"error": {"message": "context_length_exceeded", "type": "invalid_request_error"}},
            "bad_request",
            False,
        ),
        (429, {"error": {"message": "Rate limit reached"}}, "rate_limit", True),
        (500, {"error": {"message": "internal"}}, "upstream_error", True),
        (503, {}, "upstream_error", True),
        (200, {"error": {"code": 502, "message": "upstream failed"}}, "upstream_error", True),
        (408, {}, "timeout", True),
    ],
)
def test_provider_errors_normalize_and_never_carry_credentials(status, body, category, retryable):
    with pytest.raises(ModelError) as caught:
        _model(lambda r: httpx.Response(status, json=body)).decide(REQUEST)
    err = caught.value
    assert (err.code, err.retryable) == (category, retryable)
    assert KEY not in str(err) and KEY[:12] not in str(err) and "Bearer" not in str(err)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop"}]}),
        httpx.Response(
            200, json={"choices": [{"message": {"tool_calls": "x"}, "finish_reason": "stop"}]}
        ),
    ],
)
def test_malformed_responses_are_retryable_model_errors(response):
    with pytest.raises(ModelError) as caught:
        _model(lambda r: response).decide(REQUEST)
    assert caught.value.code == "malformed_response" and caught.value.retryable


def test_timeouts_failed_generations_and_filters():
    def slow(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ModelError) as caught:
        _model(slow).decide(REQUEST)
    assert (caught.value.code, caught.value.retryable) == ("timeout", True)
    for finish, code, retryable in (
        ("error", "upstream_error", True),
        ("content_filter", "bad_request", False),
    ):
        reply = _reply({"tool_calls": [_call("ping", '{"target"')]}, finish)
        with pytest.raises(ModelError) as caught:
            _model(lambda r, reply=reply: httpx.Response(200, json=reply)).decide(REQUEST)
        assert (caught.value.code, caught.value.retryable) == (code, retryable)


def test_one_request_per_decision_retries_belong_to_the_engine():
    calls = []

    def flaky(request):
        calls.append(1)
        return httpx.Response(503, json={})

    with pytest.raises(ModelError):
        _model(flaky).decide(REQUEST)
    assert len(calls) == 1


def test_credentials_never_appear_in_the_turn_or_persisted_settings():
    turn = _model(
        lambda r: httpx.Response(200, json=_reply({"tool_calls": [_call("ping", "{}")]}))
    ).decide(REQUEST)
    everything = json.dumps(
        [turn.provider_payload, turn.cache, turn.usage, turn.text, _spec().settings()], default=str
    )
    assert KEY not in everything and "Bearer" not in everything
