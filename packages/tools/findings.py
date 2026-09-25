"""`submit_findings` -- the terminal tool's schema
(docs/architecture/07-agent-tool-architecture.md).

Contract only in Phase 4: incident-core's validation of a submitted result
(evidence ids exist in `evidence_refs` for this investigation, action
catalog entries exist and are active) arrives with the agent. What *is*
enforced here, by schema, is the rule the state machine routes on:
exactly one of `selected_root_cause_index` / `inconclusive_reason`.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HypothesisOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[uuid.UUID] = Field(min_length=1)
    refuting_evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class RemediationProposalOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_catalog_id: str
    action_catalog_version: str
    parameters: dict[str, Any]


class InvestigationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[HypothesisOut] = Field(max_length=10)
    selected_root_cause_index: int | None = None
    remediation_proposal: RemediationProposalOut | None = None
    inconclusive_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> InvestigationResult:
        chose = self.selected_root_cause_index is not None
        gave_up = self.inconclusive_reason is not None
        if chose == gave_up:
            raise ValueError(
                "exactly one of selected_root_cause_index and inconclusive_reason must be set"
            )
        if chose and not 0 <= self.selected_root_cause_index < len(self.hypotheses):  # type: ignore[operator]
            raise ValueError("selected_root_cause_index must index into hypotheses")
        return self

    def cited_evidence_ids(self) -> set[uuid.UUID]:
        cited: set[uuid.UUID] = set()
        for hypothesis in self.hypotheses:
            cited.update(hypothesis.supporting_evidence_ids)
            cited.update(hypothesis.refuting_evidence_ids)
        return cited
