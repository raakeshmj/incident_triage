"""Build an `InvestigationModel` from a `ModelSpec`.

Provider adapters are imported lazily, so nothing that doesn't actually
construct a Claude model ever imports the Anthropic SDK -- the engine, the
worker's control plane, and the test suite included.
"""

from __future__ import annotations

from collections.abc import Callable

from packages.agents.config import AnthropicCredentials, ModelConfigError, ModelSpec
from packages.agents.model import InvestigationModel

ModelFactory = Callable[[ModelSpec], InvestigationModel]


def build_investigation_model(
    spec: ModelSpec, credentials: AnthropicCredentials | None = None
) -> InvestigationModel:
    if spec.provider == "anthropic":
        from packages.agents.claude import ClaudeInvestigationModel

        key = (credentials or AnthropicCredentials()).anthropic_api_key
        if key is None:
            raise ModelConfigError("ANTHROPIC_API_KEY is not set (process environment or .env)")
        return ClaudeInvestigationModel(spec, api_key=key.get_secret_value())
    raise ModelConfigError(f"unknown investigation model provider {spec.provider!r}")
