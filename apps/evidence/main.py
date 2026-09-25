"""evidence-service's internal HTTP surface (Phase 4).

Internal only: served on its own port (`make run-evidence`, default 8010),
never behind the public edge that alert-ingestion sits on. Two kinds of
caller:

- the (future) investigation agent, through the tool endpoints -- context
  binding, validation, retries and clean errors come from
  packages/tools, exactly as they will for an in-process agent;
- humans / the eval harness, through the read endpoints (incident evidence
  replay, a single full record, integrity verification).

There is deliberately no endpoint that takes a raw PromQL/LogQL/TraceQL
query, a git command, or a database query.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from apps.evidence.config import get_evidence_settings
from apps.evidence.dependencies import get_evidence_service
from packages.evidence.errors import EvidenceNotFoundError
from packages.evidence.models import EvidenceItem, EvidenceRecord, Provenance
from packages.evidence.service import EvidenceService
from packages.telemetry.logging import configure_logging
from packages.tools.contracts import ToolContext, ToolResult
from packages.tools.executor import ToolExecutor
from packages.tools.registry import tool_definitions

configure_logging(get_evidence_settings().log_level)

app = FastAPI(title="Incident Intelligence evidence-service (internal)", version="0.1.0")


class ToolCall(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    investigation_id: uuid.UUID | None = None


class ReplayEntry(BaseModel):
    sequence: int
    evidence: EvidenceItem
    provenance: Provenance


class IntegrityResponse(BaseModel):
    evidence_id: uuid.UUID
    ok: bool
    content_hash_matches: bool
    ref_registered: bool
    ref_matches: bool


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/internal/v1/tools")
def list_tools() -> list[dict[str, Any]]:
    return tool_definitions()


@app.post("/internal/v1/incidents/{incident_id}/tools/{tool_name}")
def call_tool(
    incident_id: uuid.UUID,
    tool_name: str,
    call: ToolCall,
    service: EvidenceService = Depends(get_evidence_service),
) -> ToolResult:
    # One executor per request: budgets are per-investigation state that the
    # agent loop will own; over HTTP each call is independent.
    context = ToolContext(incident_id=incident_id, investigation_id=call.investigation_id)
    return ToolExecutor(service, context).execute(tool_name, call.arguments)


@app.get("/internal/v1/incidents/{incident_id}/evidence")
def incident_evidence(
    incident_id: uuid.UUID, service: EvidenceService = Depends(get_evidence_service)
) -> list[ReplayEntry]:
    return [
        ReplayEntry(sequence=r.sequence, evidence=r.to_item(), provenance=r.provenance)
        for r in service.get_incident_evidence(incident_id)
    ]


@app.get("/internal/v1/evidence/{evidence_id}")
def get_evidence(
    evidence_id: uuid.UUID, service: EvidenceService = Depends(get_evidence_service)
) -> EvidenceRecord:
    try:
        return service.get_evidence(evidence_id)
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="evidence not found") from exc


@app.get("/internal/v1/evidence/{evidence_id}/verify")
def verify_evidence(
    evidence_id: uuid.UUID, service: EvidenceService = Depends(get_evidence_service)
) -> IntegrityResponse:
    try:
        report = service.verify(evidence_id)
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="evidence not found") from exc
    return IntegrityResponse(
        evidence_id=report.evidence_id,
        ok=report.ok,
        content_hash_matches=report.content_hash_matches,
        ref_registered=report.ref_registered,
        ref_matches=report.ref_matches,
    )
