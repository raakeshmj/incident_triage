"""The runtime model is configuration: changing INVESTIGATION_MODEL changes
what is called, with no code change -- verified by injection, never by
calling real models."""

from __future__ import annotations

import pytest

from packages.agents.claude import ClaudeInvestigationModel
from packages.agents.config import (
    DEFAULT_INVESTIGATION_MODEL,
    InvestigationSettings,
    ModelConfigError,
    ModelSpec,
    resolve_model_spec,
)
from packages.agents.factory import build_investigation_model
from packages.agents.model import ContextEntry, DecisionRequest

REQUEST = DecisionRequest(system_prompt="s", tools=[], transcript=[ContextEntry(text="ctx")])


def _settings(monkeypatch, **env: str) -> InvestigationSettings:
    for key in list(env):
        monkeypatch.setenv(key, env[key])
    return InvestigationSettings(_env_file=None)  # type: ignore[call-arg]


def test_default_runtime_model_is_sonnet_4_6(monkeypatch):
    monkeypatch.delenv("INVESTIGATION_MODEL", raising=False)
    spec = resolve_model_spec(_settings(monkeypatch))
    assert DEFAULT_INVESTIGATION_MODEL == "claude-sonnet-4-6"
    assert (spec.provider, spec.model, spec.thinking, spec.effort) == (
        "anthropic",
        "claude-sonnet-4-6",
        "adaptive",
        "medium",
    )


@pytest.mark.parametrize(
    ("model", "thinking", "effort"),
    [
        ("claude-haiku-4-5", None, None),  # no adaptive thinking, rejects effort
        ("claude-sonnet-4-6", {"type": "adaptive"}, {"effort": "medium"}),
        ("claude-opus-5-5", {"type": "adaptive"}, {"effort": "medium"}),
    ],
)
def test_changing_the_env_var_changes_the_model_and_its_request_shape(
    monkeypatch, model, thinking, effort
):
    spec = resolve_model_spec(_settings(monkeypatch, INVESTIGATION_MODEL=model))
    built = build_investigation_model(spec)
    assert isinstance(built, ClaudeInvestigationModel)
    request = built.build_request(REQUEST)
    assert request["model"] == model
    assert request.get("thinking") == thinking
    assert request.get("output_config") == effort
    assert "tool_choice" not in request  # forced tool choice 400s on some models


def test_unknown_model_ids_work_conservatively(monkeypatch):
    spec = resolve_model_spec(_settings(monkeypatch, INVESTIGATION_MODEL="claude-future-9"))
    request = build_investigation_model(spec).build_request(REQUEST)  # type: ignore[attr-defined]
    assert request["model"] == "claude-future-9"
    assert "thinking" not in request and "output_config" not in request


def test_invalid_combinations_fail_at_configuration_time(monkeypatch):
    with pytest.raises(ModelConfigError):
        resolve_model_spec(
            _settings(
                monkeypatch, INVESTIGATION_MODEL="claude-opus-5-5", INVESTIGATION_THINKING="off"
            )
        )
    with pytest.raises(ModelConfigError):
        resolve_model_spec(
            _settings(
                monkeypatch, INVESTIGATION_MODEL="claude-sonnet-4-6", INVESTIGATION_EFFORT="xhigh"
            )
        )
    with pytest.raises(ModelConfigError):
        build_investigation_model(
            ModelSpec(
                provider="somewhere-else",
                model="m",
                thinking="none",
                effort=None,
                max_tokens=1024,
                timeout_seconds=1,
                max_retries=0,
            )
        )


def test_persisted_spec_round_trips_so_a_resumed_run_keeps_its_model(monkeypatch):
    spec = resolve_model_spec(_settings(monkeypatch, INVESTIGATION_MODEL="claude-haiku-4-5"))
    restored = ModelSpec.from_persisted(spec.provider, spec.model, spec.settings())
    assert restored == spec


def test_budgets_are_configurable(monkeypatch):
    settings = _settings(
        monkeypatch, INVESTIGATION_MAX_ITERATIONS="7", INVESTIGATION_MAX_TOOL_CALLS="9"
    )
    assert (settings.budget().max_iterations, settings.budget().max_tool_calls) == (7, 9)
