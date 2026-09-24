"""alert-ingestion: POST /api/v1/alerts.

This module has NO database access and no database credentials -- it
never imports packages.incident.db. It validates the inbound payload,
derives an idempotency key, builds an AlertReceivedCommand, and forwards
it to incident-core via the `IncidentCoreService` abstraction (Phase 1
requirement 6). See docs/architecture/02-component-boundaries.md.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError

from apps.api.dependencies import get_incident_core_service
from packages.domain.commands import AlertReceivedCommand
from packages.domain.enums import AlertSeverity, AlertSource, AlertStatus
from packages.domain.idempotency import derive_alert_idempotency_key
from packages.incident.service import IncidentCoreService
from packages.telemetry.context import bind_context
from packages.telemetry.logging import get_logger

router = APIRouter(prefix="/api/v1", tags=["alerts"])
log = get_logger(__name__)


class IncomingAlertRequest(BaseModel):
    source: AlertSource
    external_id: str | None = None
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    severity: AlertSeverity
    status: AlertStatus = AlertStatus.FIRING
    idempotency_key: str | None = None


class AlertAcceptedResponse(BaseModel):
    alert_id: uuid.UUID
    incident_id: uuid.UUID
    incident_created: bool


@router.post("/alerts", status_code=202, response_model=AlertAcceptedResponse)
def receive_alert(
    request: IncomingAlertRequest,
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    core: IncidentCoreService = Depends(get_incident_core_service),
) -> AlertAcceptedResponse:
    request_id = x_request_id or str(uuid.uuid4())
    normalized_payload = request.model_dump(mode="json")

    idempotency_key = request.idempotency_key or derive_alert_idempotency_key(
        source=request.source,
        external_id=request.external_id,
        normalized_payload=normalized_payload,
    )

    with bind_context(request_id=request_id, idempotency_key=idempotency_key):
        try:
            command = AlertReceivedCommand(
                idempotency_key=idempotency_key,
                source=request.source,
                external_id=request.external_id,
                labels=request.labels,
                annotations=request.annotations,
                severity=request.severity,
                status=request.status,
                raw_payload=normalized_payload,
            )
        except ValidationError as exc:
            # exc.errors() embeds the raw exception object in `ctx` for
            # value_error cases, which isn't JSON-serializable -- use the
            # JSON-safe serialization pydantic already provides instead.
            errors = json.loads(exc.json())
            log.warning("alert_received.validation_failed", errors=errors)
            raise HTTPException(status_code=422, detail=errors) from exc

        log.info("alert_received.accepted", source=request.source.value)
        result = core.handle_alert_received(command)

    return AlertAcceptedResponse(
        alert_id=result.alert_id,
        incident_id=result.incident_id,
        incident_created=result.incident_created,
    )
