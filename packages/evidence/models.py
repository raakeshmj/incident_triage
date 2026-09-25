"""Evidence data shapes.

- `Observation`: what an adapter hands back -- the raw (bounded) backend
  response, its normalized compact form, and exactly how it was obtained.
  Adapters never persist anything and never see an incident id.
- `EvidenceRecord`: the immutable, persisted record (evidence schema). An
  Observation plus identity, incident association, `content_hash`, and
  collection provenance.
- `EvidenceItem`: the compact view returned to callers (and, later, into a
  model's context): ids + hash + summary + normalized payload, never the
  raw response.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from packages.evidence.types import EvidenceType, SourceSystem


@dataclass(frozen=True)
class Observation:
    evidence_type: EvidenceType
    source_system: SourceSystem
    operation: str
    subject_service: str
    # The semantic operation + validated parameters + the exact backend
    # query that ran (rendered PromQL/LogQL/TraceQL, git argv, registry key).
    query_spec: dict[str, Any]
    # How to find the same object at the source again (endpoint, trace id,
    # commit sha, registry key + index...).
    source_reference: dict[str, Any]
    raw_response: Any
    raw_truncated: bool
    normalized_payload: dict[str, Any]
    summary: str
    result_count: int
    # Source-side time: the newest sample/line/span/change the observation
    # covers (or the window end if the source returned nothing).
    observed_at: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None


class Provenance(BaseModel):
    """Answers, for one record: where did this come from, when was it
    observed, when was it collected, what operation/query produced it,
    which incident asked, what source object identifies it, and what exact
    content came back (`content_hash` over the stored raw response)."""

    model_config = ConfigDict(frozen=True)

    source_system: SourceSystem
    operation: str
    query_spec: dict[str, Any]
    source_reference: dict[str, Any]
    observed_at: datetime
    window_start: datetime | None
    window_end: datetime | None
    collected_at: datetime
    incident_id: uuid.UUID
    investigation_id: uuid.UUID | None
    requested_by: str
    content_hash: str


class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: uuid.UUID
    incident_id: uuid.UUID
    evidence_type: EvidenceType
    source_system: SourceSystem
    operation: str
    subject_service: str
    observed_at: datetime
    collected_at: datetime
    content_hash: str
    summary: str
    result_count: int
    data: dict[str, Any]


class EvidenceRecord(BaseModel):
    """The full persisted record, raw response included -- for humans, the
    eval harness, and replay; never handed to a model as-is."""

    model_config = ConfigDict(frozen=True)

    evidence_id: uuid.UUID
    sequence: int
    incident_id: uuid.UUID
    investigation_id: uuid.UUID | None
    evidence_type: EvidenceType
    source_system: SourceSystem
    operation: str
    subject_service: str
    query_spec: dict[str, Any]
    source_reference: dict[str, Any]
    observed_at: datetime
    window_start: datetime | None
    window_end: datetime | None
    collected_at: datetime
    expires_at: datetime | None
    requested_by: str
    content_hash: str
    raw_response: Any
    raw_truncated: bool
    normalized_payload: dict[str, Any]
    summary: str
    result_count: int

    @property
    def provenance(self) -> Provenance:
        return Provenance(
            source_system=self.source_system,
            operation=self.operation,
            query_spec=self.query_spec,
            source_reference=self.source_reference,
            observed_at=self.observed_at,
            window_start=self.window_start,
            window_end=self.window_end,
            collected_at=self.collected_at,
            incident_id=self.incident_id,
            investigation_id=self.investigation_id,
            requested_by=self.requested_by,
            content_hash=self.content_hash,
        )

    def to_item(self) -> EvidenceItem:
        return EvidenceItem(
            evidence_id=self.evidence_id,
            incident_id=self.incident_id,
            evidence_type=self.evidence_type,
            source_system=self.source_system,
            operation=self.operation,
            subject_service=self.subject_service,
            observed_at=self.observed_at,
            collected_at=self.collected_at,
            content_hash=self.content_hash,
            summary=self.summary,
            result_count=self.result_count,
            data=self.normalized_payload,
        )
