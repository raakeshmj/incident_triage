"""Prompt construction and caching, offline (stub SDK client, fake models).

Stable = system prompt + tool definitions; dynamic = the transcript. Only the
stable prefix is ever marked for caching, only for providers/models whose
profile supports it, and a provider without caching works unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from anthropic.types import Message

from packages.agents.claude import ClaudeInvestigationModel
from packages.agents.config import (
    InvestigationSettings,
    ModelSpec,
    profile_for,
    resolve_model_spec,
)
from packages.agents.context import build_context, render_context
from packages.agents.model import (
    ContextEntry,
    DecisionRequest,
    ObservationEntry,
    ToolResultEntry,
    stable_prefix_digest,
)
from packages.agents.prompts import system_prompt
from packages.agents.toolset import InvestigationToolset
from packages.domain.investigation import InvestigationBudget, StoppingCriteria
from packages.domain.views import AlertView, IncidentView
from tests.investigation_support import CATALOG

TOOLS = InvestigationToolset.definitions()
SYSTEM = system_prompt(StoppingCriteria())


def _spec(model: str = "claude-haiku-4-5", cache: str = "stable_prefix") -> ModelSpec:
    return ModelSpec(
        provider="anthropic",
        model=model,
        thinking="none",
        effort=None,
        max_tokens=1024,
        timeout_seconds=5,
        max_retries=0,
        prompt_cache=cache,  # type: ignore[arg-type]
    )


class StubClient:
    def __init__(self, read: int = 0, written: int = 0):
        self.calls: list[dict] = []
        self.messages = self
        self._usage = {
            "input_tokens": 200,
            "output_tokens": 10,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": written,
        }

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return Message.model_validate(
            {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": kwargs["model"],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "content": [{"type": "text", "text": "ok", "citations": None}],
                "usage": self._usage,
            }
        )


def _incident(service: str, severity: str = "critical") -> IncidentView:
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    return IncidentView.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "status": "INVESTIGATING",
            "service": service,
            "environment": "production",
            "severity": severity,
            "correlation_key": "k",
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
            "version": 2,
            "attempt_count": 1,
            "alerts": [
                AlertView.model_validate(
                    {
                        "id": "00000000-0000-0000-0000-00000000000a",
                        "source": "prometheus",
                        "external_id": "x",
                        "fingerprint": "f",
                        "labels": {"alertname": "HighErrorRate", "service": service},
                        "annotations": {"summary": f"errors on {service}"},
                        "severity": severity,
                        "status": "firing",
                        "received_at": now - timedelta(minutes=1),
                        "resolved_at": None,
                    }
                )
            ],
        }
    )


def _request(service: str = "checkout-service", results: int = 0) -> DecisionRequest:
    context = build_context(
        _incident(service), [], CATALOG, InvestigationBudget(), datetime(2026, 9, 1, 12, tzinfo=UTC)
    )
    transcript: list = [ContextEntry(text=render_context(context))]
    for i in range(results):
        transcript.append(
            ObservationEntry(
                results=[ToolResultEntry(call_id=f"t{i}", content="{}", is_error=False)]
            )
        )
    return DecisionRequest(system_prompt=SYSTEM, tools=TOOLS, transcript=transcript)


def _sent(spec: ModelSpec, request: DecisionRequest, **usage) -> tuple[dict, dict]:
    client = StubClient(**usage)
    turn = ClaudeInvestigationModel(spec, client=client).decide(request)  # type: ignore[arg-type]
    return client.calls[0], turn.cache


def _has_cache_marker(value) -> bool:
    return "cache_control" in json.dumps(value)


def test_stable_content_is_separated_from_dynamic_content():
    # nothing incident- or investigation-specific leaks into the stable prefix
    for fragment in ("checkout-service", "payment-service", "2026-", "max_turns", "HighErrorRate"):
        assert fragment not in SYSTEM
    assert "checkout-service" in _request().transcript[0].text  # type: ignore[union-attr]
    # and the stable prefix is byte-identical whatever the budget or incident
    assert (
        _request("checkout-service").stable_prefix_digest()
        == _request("payment-service", results=3).stable_prefix_digest()
    )


def test_only_the_stable_prefix_is_marked_for_caching():
    sent, cache = _sent(_spec(), _request(results=2))
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent  # no automatic whole-transcript breakpoint
    assert not _has_cache_marker(sent["messages"])  # dynamic context/tool results never
    assert not _has_cache_marker(sent["tools"])  # covered by the system breakpoint
    assert cache["requested"] and cache["strategy"] == "stable_prefix"
    assert cache["breakpoints"] == ["system"]


def test_cache_hints_follow_the_provider_profile_and_settings(monkeypatch):
    for env in ("INVESTIGATION_PROVIDER", "INVESTIGATION_MODEL_PROVIDER", "INVESTIGATION_MODEL"):
        monkeypatch.delenv(env, raising=False)

    def spec_for(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return resolve_model_spec(InvestigationSettings(_env_file=None))  # type: ignore[call-arg]

    assert spec_for(INVESTIGATION_MODEL="claude-haiku-4-5").prompt_cache == "stable_prefix"
    assert spec_for(INVESTIGATION_PROMPT_CACHE="off").prompt_cache == "off"
    monkeypatch.setenv("INVESTIGATION_PROMPT_CACHE", "auto")
    # a provider/model with no profile gets no cache hints (and no thinking/effort)
    other = spec_for(INVESTIGATION_PROVIDER="someprovider", INVESTIGATION_MODEL="m-1")
    assert (other.provider, other.prompt_cache, other.thinking) == ("someprovider", "off", "none")
    assert profile_for("someprovider", "m-1").prompt_caching is False


def test_cache_off_sends_no_markers_and_says_so():
    sent, cache = _sent(_spec(cache="off"), _request())
    assert not _has_cache_marker(sent)
    assert cache["requested"] is False and cache["strategy"] == "none"


def test_cache_metadata_is_captured_from_usage():
    request = _request()
    _, miss = _sent(_spec(), request, written=4200)
    _, hit = _sent(_spec(), request, read=4200)
    assert (miss["write_tokens"], miss["hit"]) == (4200, False)
    assert (hit["read_tokens"], hit["hit"]) == (4200, True)
    assert miss["prefix_digest"] == hit["prefix_digest"] == request.stable_prefix_digest()
    assert miss["min_cacheable_tokens"] == 4096  # Haiku 4.5: shorter prefixes silently miss
    assert miss["prefix_chars"] == len(SYSTEM) + len(
        json.dumps(
            [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in TOOLS
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def test_changing_incident_state_changes_only_the_uncached_part():
    early, later = _request(results=0), _request(results=3)
    sent_early, _ = _sent(_spec(), early)
    sent_later, _ = _sent(_spec(), later)
    assert sent_early["system"] == sent_later["system"]
    assert sent_early["tools"] == sent_later["tools"]
    # the newer state is present in full -- never served from an older request
    assert len(sent_later["messages"]) == len(sent_early["messages"]) + 3
    other_incident, _ = _sent(_spec(), _request("payment-service"))
    assert other_incident["messages"] != sent_early["messages"]
    assert other_incident["system"] == sent_early["system"]


def test_digest_changes_when_the_stable_prefix_changes():
    assert stable_prefix_digest(SYSTEM, TOOLS) != stable_prefix_digest(SYSTEM + " ", TOOLS)
    assert stable_prefix_digest(SYSTEM, TOOLS) != stable_prefix_digest(SYSTEM, TOOLS[:-1])


def test_the_api_key_is_not_part_of_any_request_or_cache_metadata():
    secret = "sk-ant-test-" + "x" * 40
    client = StubClient()
    model = ClaudeInvestigationModel(_spec(), client=client, api_key=secret)  # type: ignore[arg-type]
    turn = model.decide(_request())
    assert secret not in json.dumps(client.calls[0], default=str)
    assert secret not in json.dumps(turn.cache) + json.dumps(turn.provider_payload, default=str)
