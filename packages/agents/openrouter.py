"""OpenRouter: a preset of the generic OpenAI-compatible adapter -- the fixed
base URL `https://openrouter.ai/api/v1`, OPENROUTER_API_KEY, and an
attribution header. All request/response handling is in
`packages/agents/openai_compatible.py`.
"""

from __future__ import annotations

from typing import Any

from packages.agents.config import OPENAI_COMPATIBLE_PROVIDERS, ModelSpec
from packages.agents.openai_compatible import (
    OpenAICompatibleInvestigationModel,
    parse_arguments,
    render_messages,
)

OPENROUTER_BASE_URL = OPENAI_COMPATIBLE_PROVIDERS["openrouter"][0]

__all__ = [
    "OPENROUTER_BASE_URL",
    "OpenRouterInvestigationModel",
    "parse_arguments",
    "render_messages",
]


class OpenRouterInvestigationModel(OpenAICompatibleInvestigationModel):
    def __init__(self, spec: ModelSpec, *, api_key: str, client: Any = None) -> None:
        super().__init__(
            spec,
            api_key=api_key,
            base_url=spec.base_url or OPENROUTER_BASE_URL,
            client=client,
            extra_headers={"X-Title": "Incident Intelligence"},
        )
