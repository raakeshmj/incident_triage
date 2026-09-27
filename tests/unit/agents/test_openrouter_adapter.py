"""The OpenRouter adapter against a mocked transport: request rendering,
response mapping, error classification, and that the key never leaves the
Authorization header."""

from __future__ import annotations

import json

import httpx
import pytest

from packages.agents.config import ModelSpec, missing_credentials
from packages.agents.factory import build_investigation_model, validate_provider
from packages.agents.model import (
    AssistantEntry,
    ContextEntry,
    DecisionRequest,
    ModelError,
    ObservationEntry,
    ToolDefinition,
    ToolResultEntry,
)
from packages.agents.openrouter import OpenRouterInvestigationModel, render_messages
from packages.domain.investigation import ModelAction

KEY = "sk-or-test-not-a-real-key"
SPEC = ModelSpec(
    provider="openrouter",
    model="vendor/some-model:free",
    thinking="none",
    effort=None,
    max_tokens=4096,
    timeout_seconds=30,
    max_retries=0,
)
TOOL = ToolDefinition("get_service_health", "health", {"type": "object", "properties": {}})
PRIOR = AssistantEntry(
    text="checking",
    actions=[ModelAction(call_id="c1", name="get_service_health", arguments={})],
    provider_payload={
        "message": {
            "content": "checking",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_service_health", "arguments": "{}"},
                }
            ],
            "reasoning_details": [{"type": "reasoning.text", "text": "think"}],
        }
    },
)
REQUEST = DecisionRequest(
    system_prompt="SYSTEM",
    tools=[TOOL],
    transcript=[
        ContextEntry(text="incident"),
        PRIOR,
        ObservationEntry(
            results=[ToolResultEntry(call_id="c1", content='{"ok": true}', is_error=False)],
            notices=["2 iterations left"],
        ),
    ],
)


def _model(handler) -> OpenRouterInvestigationModel:
    return OpenRouterInvestigationModel(
        SPEC, api_key=KEY, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _ok(message: dict, finish: str = "tool_calls", usage: dict | None = None) -> dict:
    return {
        "id": "gen-1",
        "model": "vendor/some-model:free",
        "provider": "SomeHost",
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
    }


def test_renders_system_context_replayed_turns_tool_results_and_notices():
    messages = render_messages(REQUEST)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "user"]
    assert messages[0]["content"] == "SYSTEM"
    assert messages[2]["tool_calls"][0]["id"] == "c1"
    assert messages[2]["reasoning_details"] == [{"type": "reasoning.text", "text": "think"}]
    assert messages[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}
    assert messages[4]["content"] == "2 iterations left"


def test_request_and_response_mapping():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_ok(
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {
                                "name": "get_service_health",
                                "arguments": '{"service": "checkout-service"}',
                            },
                        }
                    ],
                },
                usage={
                    "prompt_tokens": 500,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 300},
                    "completion_tokens_details": {"reasoning_tokens": 12},
                },
            ),
        )

    turn = _model(handler).decide(REQUEST)
    body = seen["body"]
    assert body["model"] == SPEC.model and body["tool_choice"] == "auto"
    assert body["tools"][0]["function"]["name"] == "get_service_health"
    assert "reasoning" not in body  # the unknown-model profile sends no effort
    assert seen["auth"] == f"Bearer {KEY}"
    assert turn.actions == [
        ModelAction(
            call_id="c2", name="get_service_health", arguments={"service": "checkout-service"}
        )
    ]
    assert turn.stop_reason == "tool_use"
    assert turn.usage["input_tokens"] == 500 and turn.usage["cache_read_input_tokens"] == 300
    assert turn.cache["read_tokens"] == 300 and turn.cache["requested"] is False
    assert turn.provider_payload["upstream_provider"] == "SomeHost"
    assert KEY not in json.dumps(turn.provider_payload) + json.dumps(turn.cache)


def test_truncated_turns_never_act_and_bad_arguments_are_flagged_not_guessed():
    call = {"id": "c3", "function": {"name": "x", "arguments": '{"service": "chec'}}
    turn = _model(lambda r: httpx.Response(200, json=_ok({"tool_calls": [call]}, "length"))).decide(
        REQUEST
    )
    assert turn.actions == [] and turn.stop_reason == "max_tokens"
    turn = _model(lambda r: httpx.Response(200, json=_ok({"tool_calls": [call]}))).decide(REQUEST)
    assert "__unparseable__" in turn.actions[0].arguments


@pytest.mark.parametrize(
    ("status", "body", "retryable"),
    [
        (401, {"error": {"code": 401, "message": "No auth credentials found"}}, False),
        (402, {"error": {"code": 402, "message": "Insufficient credits"}}, False),
        (404, {"error": {"code": 404, "message": "No endpoints found"}}, False),
        (429, {"error": {"code": 429, "message": "Rate limit exceeded"}}, True),
        (200, {"error": {"code": 502, "message": "upstream failed"}}, True),
        (503, {}, True),
    ],
)
def test_errors_are_classified_and_credential_free(status, body, retryable):
    with pytest.raises(ModelError) as caught:
        _model(lambda r: httpx.Response(status, json=body)).decide(REQUEST)
    assert caught.value.retryable is retryable
    assert KEY not in str(caught.value)


def test_timeouts_are_retryable():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ModelError) as caught:
        _model(handler).decide(REQUEST)
    assert caught.value.retryable


def test_provider_is_registered_and_credentials_are_checked_without_reading_them(monkeypatch):
    validate_provider("openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.chdir("/")  # no .env here
    assert missing_credentials("openrouter") == "OPENROUTER_API_KEY"
    with pytest.raises(Exception, match="OPENROUTER_API_KEY is not set"):
        build_investigation_model(SPEC)
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    assert missing_credentials("openrouter") is None
    assert isinstance(build_investigation_model(SPEC), OpenRouterInvestigationModel)


def test_upstream_rate_limits_keep_openrouters_explanation():
    body = {
        "error": {
            "code": 429,
            "message": "Provider returned error",
            "metadata": {
                "provider_name": "SomeHost",
                "limit_source": "upstream_provider",
                "raw": "vendor/some-model:free is temporarily rate-limited upstream.",
            },
        }
    }
    with pytest.raises(ModelError) as caught:
        _model(lambda r: httpx.Response(429, json=body)).decide(REQUEST)
    text = str(caught.value)
    assert caught.value.retryable
    assert "upstream=SomeHost" in text and "limit_source=upstream_provider" in text
    assert "rate-limited upstream" in text


def test_a_generation_that_failed_upstream_is_retried_not_acted_on():
    """Seen in the first real run: finish_reason "error" with a truncated
    conclude_investigation call. It must never reach the engine as a turn."""
    call = {
        "id": "c9",
        "function": {
            "name": "conclude_investigation",
            "arguments": '{"confidence": 0.9, "selected_hypothesis_key": "H1"',
        },
    }
    body = _ok({"tool_calls": [call]}, "error")
    with pytest.raises(ModelError) as caught:
        _model(lambda r: httpx.Response(200, json=body)).decide(REQUEST)
    assert caught.value.retryable and caught.value.code == "upstream_generation_error"
