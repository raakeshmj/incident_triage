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

from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

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
PROVIDER_PROFILES: dict[str, dict[str, ModelProfile]] = {
    "anthropic": ANTHROPIC_PROFILES,
    # OpenRouter models run with the conservative unknown-model profile (no
    # thinking/effort/cache parameters); add entries here to opt a model in.
    "openrouter": {},
}
# Kept for existing imports: the Anthropic table.
MODEL_PROFILES = ANTHROPIC_PROFILES
# Unknown provider/model pairs still work -- conservatively, with no
# thinking/effort/cache parameters -- so a new model is a config change.
UNKNOWN_MODEL_PROFILE = ModelProfile(thinking="none", efforts=frozenset())


@dataclass(frozen=True)
class ChatCapabilities:
    """What an OpenAI-compatible Chat Completions endpoint accepts, declared by
    configuration (never guessed from a model name). The adapter sends only
    what is declared; everything it can't enforce stays enforced by the
    engine's own validation, which never depends on these flags.

    - tools: function tools are sent (the investigation needs them; a model
      without tool calling can't investigate and fails visibly).
    - tool_choice: `tool_choice: "auto"` is sent (some endpoints reject it).
    - parallel_tool_calls: sent as given when not None.
    - strict_tools: `strict: true` on tools whose JSON schema is compatible
      with strict structured outputs; incompatible schemas are sent without.
    - reasoning: how to send `INVESTIGATION_EFFORT` -- "none" (never),
      "reasoning_effort" (OpenAI style), "reasoning_object" (OpenRouter style).
    - max_tokens_param: "max_tokens" or "max_completion_tokens".
    - context_tokens: the model's context window, when known (recorded).
    - prompt_caching: the provider caches prompts; its reported cached tokens
      are recorded (no hints are sent -- OpenAI-compatible APIs cache
      automatically or not at all).
    """

    tools: bool = True
    tool_choice: bool = True
    parallel_tool_calls: bool | None = None
    strict_tools: bool = False
    reasoning: Literal["none", "reasoning_effort", "reasoning_object"] = "none"
    max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    context_tokens: int | None = None
    prompt_caching: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ChatCapabilities:
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


# Providers served through the generic OpenAI-compatible adapter:
# provider -> (fixed base URL or None = INVESTIGATION_BASE_URL, default capabilities)
OPENAI_COMPATIBLE_PROVIDERS: dict[str, tuple[str | None, ChatCapabilities]] = {
    "openai_compatible": (None, ChatCapabilities()),
    # reasoning stays "none" unless configured (INVESTIGATION_REASONING=reasoning_object)
    "openrouter": ("https://openrouter.ai/api/v1", ChatCapabilities()),
}


def validate_base_url(url: str) -> str:
    """https (or http for localhost) with a host; no credentials or query
    string, since the base URL is persisted with the investigation."""
    parts = urlsplit(url.strip())
    local = parts.hostname in ("localhost", "127.0.0.1")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ModelConfigError(f"INVESTIGATION_BASE_URL must be an http(s) URL, got {url!r}")
    if parts.scheme == "http" and not local:
        raise ModelConfigError("INVESTIGATION_BASE_URL must use https (http only for localhost)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ModelConfigError(
            "INVESTIGATION_BASE_URL must not carry credentials, a query or a fragment; "
            "put the key in INVESTIGATION_API_KEY"
        )
    return url.strip().rstrip("/")


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

    # --- OpenAI-compatible providers (INVESTIGATION_PROVIDER=openai_compatible) ---
    # The minimum: INVESTIGATION_BASE_URL + INVESTIGATION_API_KEY + INVESTIGATION_MODEL.
    # Capabilities default to the conservative set; declare more only if the
    # endpoint supports them (docs/operations.md).
    investigation_base_url: str | None = None
    investigation_tool_choice: bool = True
    investigation_parallel_tool_calls: bool | None = None
    investigation_strict_tools: bool = False
    investigation_reasoning: Literal["none", "reasoning_effort", "reasoning_object"] | None = None
    investigation_max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    investigation_context_tokens: int | None = None
    investigation_provider_prompt_caching: bool = False

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
    # OpenAI-compatible providers only: where to send requests and what the
    # endpoint accepts. Non-secret by construction (validate_base_url); the
    # API key is never part of the spec.
    base_url: str | None = None
    capabilities: dict[str, Any] | None = None

    def settings(self) -> dict[str, Any]:
        return {
            "thinking": self.thinking,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "prompt_cache": self.prompt_cache,
            "prompt_version": self.prompt_version,
            **({"base_url": self.base_url} if self.base_url else {}),
            **({"capabilities": self.capabilities} if self.capabilities else {}),
        }

    @classmethod
    def from_persisted(cls, provider: str, model: str, settings: dict[str, Any]) -> ModelSpec:
        return cls(provider=provider, model=model, **settings)


class AnthropicCredentials(BaseSettings):
    """The API key, from the process environment or `.env`. Kept apart from
    `ModelSpec` so it is never persisted, logged or part of a trace."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr | None = None


class OpenRouterCredentials(BaseSettings):
    """OPENROUTER_API_KEY, from the process environment or `.env`; like the
    Anthropic key, never persisted, logged or part of a trace."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: SecretStr | None = None


class OpenAICompatibleCredentials(BaseSettings):
    """INVESTIGATION_API_KEY for the generic OpenAI-compatible provider."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    investigation_api_key: SecretStr | None = None


# provider -> (credential settings class, field, environment variable name)
PROVIDER_CREDENTIALS: dict[str, tuple[type[BaseSettings], str, str]] = {
    "anthropic": (AnthropicCredentials, "anthropic_api_key", "ANTHROPIC_API_KEY"),
    "openrouter": (OpenRouterCredentials, "openrouter_api_key", "OPENROUTER_API_KEY"),
    "openai_compatible": (
        OpenAICompatibleCredentials,
        "investigation_api_key",
        "INVESTIGATION_API_KEY",
    ),
}


def missing_credentials(provider: str) -> str | None:
    """The environment variable a live run of `provider` still needs, or None.
    Reads presence only; the value is never returned."""
    entry = PROVIDER_CREDENTIALS.get(provider)
    if entry is None:
        return None
    settings_cls, field_name, env_name = entry
    return None if getattr(settings_cls(), field_name) is not None else env_name


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
    compatible = OPENAI_COMPATIBLE_PROVIDERS.get(provider)
    base_url, capabilities = None, None
    if compatible is not None:
        fixed_url, defaults = compatible
        caps = ChatCapabilities(
            tools=True,
            tool_choice=settings.investigation_tool_choice,
            parallel_tool_calls=settings.investigation_parallel_tool_calls,
            strict_tools=settings.investigation_strict_tools,
            reasoning=settings.investigation_reasoning or defaults.reasoning,
            max_tokens_param=settings.investigation_max_tokens_param,
            context_tokens=settings.investigation_context_tokens,
            prompt_caching=settings.investigation_provider_prompt_caching,
        )
        url = fixed_url or settings.investigation_base_url
        if not url:
            raise ModelConfigError(f"INVESTIGATION_BASE_URL is required for provider {provider!r}")
        base_url, capabilities = validate_base_url(url), caps.to_dict()
        # effort is sent only when the endpoint declares a reasoning parameter
        if caps.reasoning == "none":
            effort = None
    elif not profile.efforts:
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
        base_url=base_url,
        capabilities=capabilities,
    )
