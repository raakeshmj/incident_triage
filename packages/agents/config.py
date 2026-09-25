"""Runtime model and investigation configuration -- the one place that knows
which model runs and how it's called (ADR-0021).

Changing the runtime model is configuration, not code:

    INVESTIGATION_MODEL=claude-sonnet-4-6    # default
    INVESTIGATION_MODEL=claude-haiku-4-5
    INVESTIGATION_MODEL=claude-opus-5-5

Per-model API differences (whether adaptive thinking exists, which effort
levels are accepted) live in `MODEL_PROFILES`, so switching models never
means editing request code. An investigation records the model and settings
it started with and keeps them when resumed, even if configuration changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.domain.investigation import InvestigationBudget, StoppingCriteria

DEFAULT_INVESTIGATION_MODEL = "claude-sonnet-4-6"
PROMPT_VERSION = "investigation-v1"

_EFFORTS_46 = frozenset({"low", "medium", "high", "max"})
_EFFORTS_47 = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass(frozen=True)
class ModelProfile:
    """What a model's API accepts. `thinking`: "adaptive" (supported),
    "required" (always on; can't be disabled), or "none" (not used)."""

    thinking: Literal["adaptive", "required", "none"]
    efforts: frozenset[str]


MODEL_PROFILES: dict[str, ModelProfile] = {
    # Haiku 4.5 only takes budget_tokens thinking and rejects `effort`; the
    # investigation runs it without thinking.
    "claude-haiku-4-5": ModelProfile(thinking="none", efforts=frozenset()),
    "claude-sonnet-4-6": ModelProfile(thinking="adaptive", efforts=_EFFORTS_46),
    "claude-opus-4-6": ModelProfile(thinking="adaptive", efforts=_EFFORTS_46),
    "claude-opus-4-7": ModelProfile(thinking="adaptive", efforts=_EFFORTS_47),
    "claude-opus-4-8": ModelProfile(thinking="adaptive", efforts=_EFFORTS_47),
    "claude-sonnet-5": ModelProfile(thinking="adaptive", efforts=_EFFORTS_47),
    "claude-opus-5": ModelProfile(thinking="adaptive", efforts=_EFFORTS_47),
    "claude-opus-5-5": ModelProfile(thinking="required", efforts=_EFFORTS_47),
    "claude-fable-5-1": ModelProfile(thinking="required", efforts=_EFFORTS_47),
}
# Unknown model ids still work -- conservatively, with no thinking/effort
# parameters -- so a newly released model is a config change, not a code change.
UNKNOWN_MODEL_PROFILE = ModelProfile(thinking="none", efforts=frozenset())


class InvestigationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", protected_namespaces=())

    investigation_model_provider: str = "anthropic"
    investigation_model: str = DEFAULT_INVESTIGATION_MODEL
    investigation_effort: str | None = "medium"
    investigation_thinking: Literal["auto", "off"] = "auto"
    investigation_max_tokens: int = Field(default=16_000, ge=1_024)
    investigation_api_timeout_seconds: float = Field(default=120.0, gt=0)
    investigation_api_max_retries: int = Field(default=2, ge=0)
    investigation_model_attempts: int = Field(default=3, ge=1)
    investigation_retry_backoff_seconds: float = Field(default=2.0, ge=0)

    investigation_max_iterations: int = 15
    investigation_max_tool_calls: int = 25
    investigation_max_identical_tool_calls: int = 2
    investigation_max_evidence_items: int = 40
    investigation_max_wall_clock_seconds: int = 900
    investigation_max_total_tokens: int | None = 600_000

    investigation_debounce_seconds: int = 60
    investigation_lease_seconds: int = 180

    def budget(self) -> InvestigationBudget:
        return InvestigationBudget(
            max_iterations=self.investigation_max_iterations,
            max_tool_calls=self.investigation_max_tool_calls,
            max_identical_tool_calls=self.investigation_max_identical_tool_calls,
            max_evidence_items=self.investigation_max_evidence_items,
            max_wall_clock_seconds=self.investigation_max_wall_clock_seconds,
            max_total_tokens=self.investigation_max_total_tokens,
        )

    def criteria(self) -> StoppingCriteria:
        return StoppingCriteria()


@dataclass(frozen=True)
class ModelSpec:
    """A fully resolved model configuration, persisted on the investigation."""

    provider: str
    model: str
    thinking: Literal["adaptive", "none"]
    effort: str | None
    max_tokens: int
    timeout_seconds: float
    max_retries: int
    prompt_version: str = PROMPT_VERSION

    def settings(self) -> dict[str, Any]:
        return {
            "thinking": self.thinking,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_persisted(cls, provider: str, model: str, settings: dict[str, Any]) -> ModelSpec:
        return cls(provider=provider, model=model, **settings)


class ModelConfigError(ValueError):
    pass


def resolve_model_spec(settings: InvestigationSettings) -> ModelSpec:
    model = settings.investigation_model.strip()
    profile = MODEL_PROFILES.get(model, UNKNOWN_MODEL_PROFILE)
    if profile.thinking == "none":
        thinking: Literal["adaptive", "none"] = "none"
    elif settings.investigation_thinking == "off" and profile.thinking == "required":
        raise ModelConfigError(f"{model} does not allow thinking to be disabled")
    else:
        thinking = "none" if settings.investigation_thinking == "off" else "adaptive"
    effort = settings.investigation_effort
    if not profile.efforts:
        effort = None
    elif effort is not None and effort not in profile.efforts:
        raise ModelConfigError(
            f"effort {effort!r} is not supported by {model}; use one of {sorted(profile.efforts)}"
        )
    return ModelSpec(
        provider=settings.investigation_model_provider,
        model=model,
        thinking=thinking,
        effort=effort,
        max_tokens=settings.investigation_max_tokens,
        timeout_seconds=settings.investigation_api_timeout_seconds,
        max_retries=settings.investigation_api_max_retries,
    )
