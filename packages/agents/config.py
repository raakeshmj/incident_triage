"""Runtime model and investigation configuration -- the one place that knows
which model runs and how it's called (ADR-0021).

Changing the runtime provider or model is configuration, not code:

    INVESTIGATION_PROVIDER=anthropic         # placeholder default
    INVESTIGATION_MODEL=claude-haiku-4-5     # placeholder default

Nothing here needs a credential: the provider's credentials are resolved
only when a model is actually built to be called (packages/agents/factory.py).

Per-model API differences (whether adaptive thinking exists, which effort
levels are accepted, the minimum cacheable prompt length) live in
`PROVIDER_PROFILES`, so switching models never means editing request code.
A provider/model pair without a profile runs conservatively: no thinking,
no effort, no prompt-cache hints. An investigation records the model and
settings it started with and keeps them when resumed, even if
configuration changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.domain.investigation import InvestigationBudget, StoppingCriteria

DEFAULT_INVESTIGATION_MODEL = "claude-haiku-4-5"
# v2: budget moved out of the (now byte-stable, cacheable) system prompt into
# the incident context; hypotheses carry cause_category + component.
PROMPT_VERSION = "investigation-v2"

_EFFORTS_46 = frozenset({"low", "medium", "high", "max"})
_EFFORTS_47 = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass(frozen=True)
class ModelProfile:
    """What a model's API accepts. `thinking`: "adaptive" (supported),
    "required" (always on; can't be disabled), or "none" (not used)."""

    thinking: Literal["adaptive", "required", "none"]
    efforts: frozenset[str]
    # Whether the provider supports prompt-cache hints for this model, and the
    # shortest prefix it will cache (shorter prefixes are silently not cached).
    prompt_caching: bool = False
    min_cacheable_tokens: int | None = None


def _claude(
    thinking: Literal["adaptive", "required", "none"], efforts: frozenset[str], min_cache: int
) -> ModelProfile:
    return ModelProfile(
        thinking=thinking, efforts=efforts, prompt_caching=True, min_cacheable_tokens=min_cache
    )


# Anthropic Messages API models. Minimum cacheable prefix per the prompt
# caching docs; it is not monotonic across generations (Haiku 4.5: 4096).
ANTHROPIC_PROFILES: dict[str, ModelProfile] = {
    # Haiku 4.5 only takes budget_tokens thinking and rejects `effort`; the
    # investigation runs it without thinking.
    "claude-haiku-4-5": _claude("none", frozenset(), 4096),
    "claude-sonnet-4-6": _claude("adaptive", _EFFORTS_46, 1024),
    "claude-opus-4-6": _claude("adaptive", _EFFORTS_46, 4096),
    "claude-opus-4-7": _claude("adaptive", _EFFORTS_47, 2048),
    "claude-opus-4-8": _claude("adaptive", _EFFORTS_47, 1024),
    "claude-sonnet-5": _claude("adaptive", _EFFORTS_47, 1024),
    "claude-opus-5": _claude("adaptive", _EFFORTS_47, 512),
    "claude-opus-5-5": _claude("required", _EFFORTS_47, 512),
    "claude-fable-5-1": _claude("required", _EFFORTS_47, 512),
}
# provider -> model -> profile. A new provider adds a table here plus a
# builder in packages/agents/factory.py; the engine doesn't change.
PROVIDER_PROFILES: dict[str, dict[str, ModelProfile]] = {"anthropic": ANTHROPIC_PROFILES}
# Kept for existing imports: the Anthropic table.
MODEL_PROFILES = ANTHROPIC_PROFILES
# Unknown provider/model pairs still work -- conservatively, with no
# thinking/effort/cache parameters -- so a new model is a config change.
UNKNOWN_MODEL_PROFILE = ModelProfile(thinking="none", efforts=frozenset())


def profile_for(provider: str, model: str) -> ModelProfile:
    return PROVIDER_PROFILES.get(provider, {}).get(model, UNKNOWN_MODEL_PROFILE)


class InvestigationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", protected_namespaces=(), populate_by_name=True
    )

    investigation_model_provider: str = Field(
        default="anthropic",
        validation_alias=AliasChoices(
            "INVESTIGATION_PROVIDER",
            "INVESTIGATION_MODEL_PROVIDER",
            "investigation_model_provider",
        ),
    )
    investigation_model: str = DEFAULT_INVESTIGATION_MODEL
    investigation_effort: str | None = "medium"
    investigation_thinking: Literal["auto", "off"] = "auto"
    investigation_max_tokens: int = Field(default=16_000, ge=1_024)
    investigation_api_timeout_seconds: float = Field(default=120.0, gt=0)
    investigation_api_max_retries: int = Field(default=2, ge=0)
    investigation_model_attempts: int = Field(default=3, ge=1)
    investigation_retry_backoff_seconds: float = Field(default=2.0, ge=0)
    # auto: send the provider's prompt-cache hint on the stable prefix
    # (system prompt + tool definitions) where the provider supports it.
    investigation_prompt_cache: Literal["auto", "off"] = "auto"

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
    # "stable_prefix": ask the provider to cache the system prompt + tools;
    # "off": no cache hints. Resolved from settings and the model profile.
    prompt_cache: Literal["stable_prefix", "off"] = "off"
    prompt_version: str = PROMPT_VERSION

    def settings(self) -> dict[str, Any]:
        return {
            "thinking": self.thinking,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "prompt_cache": self.prompt_cache,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_persisted(cls, provider: str, model: str, settings: dict[str, Any]) -> ModelSpec:
        return cls(provider=provider, model=model, **settings)


class AnthropicCredentials(BaseSettings):
    """The API key, from the process environment or `.env`. Kept apart from
    `ModelSpec` so it is never persisted, logged or part of a trace."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr | None = None


class ModelConfigError(ValueError):
    pass


def resolve_model_spec(settings: InvestigationSettings) -> ModelSpec:
    model = settings.investigation_model.strip()
    provider = settings.investigation_model_provider.strip()
    profile = profile_for(provider, model)
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
    cache_on = settings.investigation_prompt_cache == "auto" and profile.prompt_caching
    return ModelSpec(
        provider=provider,
        model=model,
        thinking=thinking,
        effort=effort,
        max_tokens=settings.investigation_max_tokens,
        timeout_seconds=settings.investigation_api_timeout_seconds,
        max_retries=settings.investigation_api_max_retries,
        prompt_cache="stable_prefix" if cache_on else "off",
    )
