"""Build an `InvestigationModel` from a `ModelSpec`.

Provider adapters are imported lazily, so nothing that doesn't actually
construct a Claude model ever imports the Anthropic SDK -- the engine, the
worker's control plane, and the test suite included.
"""

from __future__ import annotations

from collections.abc import Callable

from packages.agents.config import ModelConfigError, ModelSpec
from packages.agents.model import InvestigationModel

ModelFactory = Callable[[ModelSpec], InvestigationModel]


def build_investigation_model(spec: ModelSpec) -> InvestigationModel:
    if spec.provider == "anthropic":
        from packages.agents.claude import ClaudeInvestigationModel

        return ClaudeInvestigationModel(spec)
    raise ModelConfigError(f"unknown investigation model provider {spec.provider!r}")
