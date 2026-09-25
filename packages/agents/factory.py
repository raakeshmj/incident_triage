"""Build an `InvestigationModel` from a `ModelSpec`, by provider.

`PROVIDERS` maps a provider name (INVESTIGATION_PROVIDER) to a builder. A
new provider is one builder here (plus, optionally, a profile table in
config.py); the investigation engine never changes. Builders import their
SDK lazily and resolve credentials only when called, so nothing that doesn't
actually construct a model -- the engine, the worker's control plane, the
test suite, local startup -- needs an SDK import or an API key.
"""

from __future__ import annotations

from collections.abc import Callable

from packages.agents.config import AnthropicCredentials, ModelConfigError, ModelSpec
from packages.agents.model import InvestigationModel

ModelFactory = Callable[[ModelSpec], InvestigationModel]


def _anthropic(spec: ModelSpec) -> InvestigationModel:
    key = AnthropicCredentials().anthropic_api_key
    if key is None:
        raise ModelConfigError("ANTHROPIC_API_KEY is not set (process environment or .env)")
    from packages.agents.claude import ClaudeInvestigationModel

    return ClaudeInvestigationModel(spec, api_key=key.get_secret_value())


PROVIDERS: dict[str, ModelFactory] = {"anthropic": _anthropic}


def known_providers() -> list[str]:
    return sorted(PROVIDERS)


def validate_provider(provider: str) -> None:
    """Startup check: fails on an unknown provider name without touching
    credentials or the network."""
    if provider not in PROVIDERS:
        raise ModelConfigError(
            f"unknown investigation provider {provider!r}; known: {known_providers()}"
        )


def build_investigation_model(
    spec: ModelSpec, credentials: AnthropicCredentials | None = None
) -> InvestigationModel:
    validate_provider(spec.provider)
    if spec.provider == "anthropic" and credentials is not None:
        key = credentials.anthropic_api_key
        if key is None:
            raise ModelConfigError("ANTHROPIC_API_KEY is not set (process environment or .env)")
        from packages.agents.claude import ClaudeInvestigationModel

        return ClaudeInvestigationModel(spec, api_key=key.get_secret_value())
    return PROVIDERS[spec.provider](spec)
