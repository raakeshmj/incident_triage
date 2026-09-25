"""ClaudeInvestigationModel against a stub client: request rendering,
response mapping, and error classification. No network."""

from __future__ import annotations

import anthropic
import httpx2
import pytest
from anthropic.types import Message

from packages.agents.claude import ClaudeInvestigationModel, render_messages
from packages.agents.config import ModelSpec
from packages.agents.model import (
    AssistantEntry,
    ContextEntry,
    DecisionRequest,
    ModelError,
    ObservationEntry,
    ToolDefinition,
    ToolResultEntry,
)
from packages.domain.investigation import ModelAction

SPEC = ModelSpec(
    provider="anthropic",
    model="claude-sonnet-4-6",
    thinking="adaptive",
    effort="medium",
    max_tokens=16000,
    timeout_seconds=30,
    max_retries=0,
)


def _message(content, stop_reason="tool_use"):
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "content": content,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 10,
            },
        }
    )


class StubClient:
    def __init__(self, response=None, error=None):
        self.calls = []
        self._response = response
        self._error = error
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


REQUEST = DecisionRequest(
    system_prompt="system",
    tools=[ToolDefinition(name="get_logs", description="d", input_schema={"type": "object"})],
    transcript=[
        ContextEntry(text="context"),
        AssistantEntry(
            text="",
            actions=[ModelAction(call_id="toolu_a", name="get_logs", arguments={})],
            provider_payload={
                "content": [
                    {"type": "thinking", "thinking": "t", "signature": "s"},
                    {"type": "tool_use", "id": "toolu_a", "name": "get_logs", "input": {}},
                ]
            },
        ),
        ObservationEntry(
            results=[ToolResultEntry(call_id="toolu_a", content='{"x":1}', is_error=True)],
            notices=["Budget: turn 2 of 15"],
        ),
    ],
)


def test_transcript_renders_replayed_blocks_and_one_result_per_tool_call():
    messages = render_messages(REQUEST)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"][0]["type"] == "thinking"  # replayed unchanged
    results = messages[2]["content"]
    assert results[0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_a",
        "content": '{"x":1}',
        "is_error": True,
    }
    assert results[1] == {"type": "text", "text": "Budget: turn 2 of 15"}


def test_request_uses_caching_adaptive_thinking_and_effort():
    client = StubClient(_message([{"type": "text", "text": "hi", "citations": None}], "end_turn"))
    ClaudeInvestigationModel(SPEC, client=client).decide(REQUEST)  # type: ignore[arg-type]
    sent = client.calls[0]
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "medium"}
    assert sent["tools"][0]["name"] == "get_logs"


def test_response_maps_to_unvalidated_actions_usage_and_a_replayable_payload():
    client = StubClient(
        _message(
            [
                {"type": "thinking", "thinking": "reasoning", "signature": "sig"},
                {"type": "text", "text": "Checking logs.", "citations": None},
                {"type": "tool_use", "id": "toolu_1", "name": "get_logs", "input": {"limit": 5}},
            ]
        )
    )
    turn = ClaudeInvestigationModel(SPEC, client=client).decide(REQUEST)  # type: ignore[arg-type]
    assert turn.text == "Checking logs."
    assert turn.actions == [ModelAction(call_id="toolu_1", name="get_logs", arguments={"limit": 5})]
    assert turn.usage == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
    }
    assert turn.provider_payload["content"][0] == {
        "type": "thinking",
        "thinking": "reasoning",
        "signature": "sig",
    }
    assert turn.served_model == "claude-sonnet-4-6"


def test_truncated_responses_yield_no_actions():
    client = StubClient(
        _message(
            [{"type": "tool_use", "id": "toolu_1", "name": "get_logs", "input": {}}], "max_tokens"
        )
    )
    turn = ClaudeInvestigationModel(SPEC, client=client).decide(REQUEST)  # type: ignore[arg-type]
    assert turn.actions == [] and turn.stop_reason == "max_tokens"


def _error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers={"request-id": "req_1"})
    return cls("boom", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (_error(anthropic.RateLimitError, 429), True),
        (_error(anthropic.InternalServerError, 500), True),
        (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), True),
        (_error(anthropic.AuthenticationError, 401), False),
        (_error(anthropic.BadRequestError, 400), False),
        (_error(anthropic.NotFoundError, 404), False),
    ],
)
def test_api_errors_are_classified_retryable_or_terminal(error, retryable):
    model = ClaudeInvestigationModel(SPEC, client=StubClient(error=error))  # type: ignore[arg-type]
    with pytest.raises(ModelError) as excinfo:
        model.decide(REQUEST)
    assert excinfo.value.retryable is retryable
    assert "boom" not in str(excinfo.value)  # our summary, not the error body


def test_the_api_error_type_and_message_reach_the_trace_bounded():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(400, request=request, headers={"request-id": "req_9"})
    body = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "credit balance too low" + "x" * 500},
    }
    error = anthropic.BadRequestError("Error code: 400", response=response, body=body)
    model = ClaudeInvestigationModel(SPEC, client=StubClient(error=error))  # type: ignore[arg-type]
    with pytest.raises(ModelError) as excinfo:
        model.decide(REQUEST)
    message = str(excinfo.value)
    assert "req_9" in message and "invalid_request_error: credit balance too low" in message
    assert len(message) <= 300


def test_refusal_is_terminal():
    client = StubClient(_message([], "refusal"))
    with pytest.raises(ModelError) as excinfo:
        ClaudeInvestigationModel(SPEC, client=client).decide(REQUEST)  # type: ignore[arg-type]
    assert excinfo.value.code == "refusal" and not excinfo.value.retryable
