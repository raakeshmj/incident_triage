"""The operations console's read API (Phase 8): incident list and detail,
operations overview, lifecycle metrics, evidence records.

Read-only. Everything is read as persisted by incident-core and
evidence-service; nothing here decides or recomputes a status. Mutations
stay on the operator-token endpoints in routers/remediations.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import redis
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from apps.api.config import get_settings
from apps.api.dependencies import get_evidence_reader, get_query_service, get_redis
from packages.evaluation.recording import EvidenceStoreReader
from packages.events.streams import dead_letter_stream_name
from packages.incident.queries import IncidentQueryService
from packages.telemetry.heartbeat import read_heartbeats

router = APIRouter(prefix="/api/v1", tags=["operations"])


class IncidentSummary(BaseModel):
    id: str
    severity: str
    service: str
    environment: str
    status: str
    created_at: str
    updated_at: str
    ended_at: str | None
    duration_seconds: float
    alert_count: int
    firing_alert_count: int
    affected_services: list[str]
    affected_service_count: int
    attempt_count: int
    investigation_status: str | None
    remediation_status: str | None
    verification_status: str | None


class IncidentList(BaseModel):
    total: int
    items: list[IncidentSummary]
    services: list[str]


class IncidentDetail(BaseModel):
    incident: dict[str, Any]
    alerts: list[dict[str, Any]]
    transitions: list[dict[str, Any]]
    investigations: list[dict[str, Any]]
    remediations: list[dict[str, Any]]
    verifications: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    timeline: list[dict[str, Any]]


class WorkerHeartbeat(BaseModel):
    role: str
    key: str
    age_seconds: float
    started: float | None = None
    details: dict[str, Any] = {}


class Overview(BaseModel):
    incidents_by_status: dict[str, int]
    active_incidents: int
    investigations_running: int
    awaiting_approval: int
    remediations_executing: int
    verifications_running: int
    recent_escalations: list[dict[str, Any]]
    outbox_failed_events: int
    outbox_unpublished: int
    dead_letter_count: int | None
    workers: list[WorkerHeartbeat]
    redis_available: bool


class EvidenceDetail(BaseModel):
    evidence_id: str
    incident_id: str
    investigation_id: str | None
    evidence_type: str
    source_system: str
    operation: str
    subject_service: str
    observed_at: str
    window_start: str | None
    window_end: str | None
    collected_at: str
    requested_by: str
    content_hash: str
    summary: str
    query_spec: dict[str, Any]
    normalized_payload: dict[str, Any]


@router.get("/incidents", response_model=IncidentList)
def list_incidents(
    status: list[str] = Query(default=[]),
    severity: list[str] = Query(default=[]),
    service: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    queries: IncidentQueryService = Depends(get_query_service),
) -> dict[str, Any]:
    return queries.list_incidents(
        statuses=status or None,
        severities=severity or None,
        service=service,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


@router.get("/incidents/{incident_id}/detail", response_model=IncidentDetail)
def incident_detail(
    incident_id: uuid.UUID, queries: IncidentQueryService = Depends(get_query_service)
) -> dict[str, Any]:
    detail = queries.incident_detail(incident_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return detail


@router.get("/overview", response_model=Overview)
def overview(
    queries: IncidentQueryService = Depends(get_query_service),
    client: redis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    result = queries.overview()
    try:
        dlq = int(
            client.xlen(dead_letter_stream_name(prefix=get_settings().outbox_stream_prefix))  # type: ignore[arg-type]
        )
        beats = read_heartbeats(client)
        available = True
    except redis.RedisError:
        dlq, beats, available = None, [], False
    workers = [
        {
            "role": b.get("role", "?"),
            "key": b["key"],
            "age_seconds": b["age_seconds"],
            "started": b.get("started"),
            "details": {
                k: v
                for k, v in b.items()
                if k not in ("role", "key", "age_seconds", "started", "at")
            },
        }
        for b in beats
    ]
    return {**result, "dead_letter_count": dlq, "workers": workers, "redis_available": available}


@router.get("/metrics")
def lifecycle_metrics(queries: IncidentQueryService = Depends(get_query_service)) -> dict[str, Any]:
    return queries.metrics()


@router.get("/evidence/{evidence_id}", response_model=EvidenceDetail)
def evidence_detail(
    evidence_id: uuid.UUID, reader: EvidenceStoreReader = Depends(get_evidence_reader)
) -> dict[str, Any]:
    record = reader.get_record(evidence_id)
    if record is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    return {
        "evidence_id": str(record.evidence_id),
        "incident_id": str(record.incident_id),
        "investigation_id": str(record.investigation_id) if record.investigation_id else None,
        "evidence_type": record.evidence_type.value,
        "source_system": record.source_system.value,
        "operation": record.operation,
        "subject_service": record.subject_service,
        "observed_at": record.observed_at.isoformat(),
        "window_start": record.window_start.isoformat() if record.window_start else None,
        "window_end": record.window_end.isoformat() if record.window_end else None,
        "collected_at": record.collected_at.isoformat(),
        "requested_by": record.requested_by,
        "content_hash": record.content_hash,
        "summary": record.summary,
        "query_spec": record.query_spec,
        "normalized_payload": record.normalized_payload,
    }
