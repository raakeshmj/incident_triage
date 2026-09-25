"""alert-ingestion: POST /api/v1/alerts and POST /api/v1/alerts/alertmanager.

This module has NO database access and no database credentials -- it
never imports packages.incident.db. It validates the inbound payload,
derives an idempotency key, builds an AlertReceivedCommand, and forwards
it to incident-core via the `IncidentCoreService` abstraction (Phase 1
requirement 6). See docs/architecture/02-component-boundaries.md.

The Alertmanager route is the same alert-ingestion component, not a
parallel service or pipeline (docs/architecture/13-security-boundaries.md
places "Alertmanager, PagerDuty, generic webhooks" together at this exact
trust boundary) -- it exists because Alertmanager's own webhook payload
shape is fixed by Alertmanager itself and cannot be made to match
`IncomingAlertRequest` directly. Both routes converge on the same
`_accept_alert` helper -> the same `AlertReceivedCommand` ->
`core.handle_alert_received`. See
docs/architecture/14-observability-and-chaos.md ("Alertmanager payload
mapping") for the exact field-by-field translation.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from apps.api.config import get_settings
from apps.api.dependencies import get_incident_core_service
from packages.domain.commands import AlertReceivedCommand
from packages.domain.enums import AlertSeverity, AlertSource, AlertStatus
from packages.domain.errors import ConcurrentModificationError
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
    # None only for a resolved notification whose firing episode was never
    # seen -- recorded, but it opens no incident (Phase 4).
    incident_id: uuid.UUID | None
    incident_created: bool
    incident_status: str | None = None


def _accept_alert(
    core: IncidentCoreService,
    *,
    source: AlertSource,
    external_id: str | None,
    labels: dict[str, str],
    annotations: dict[str, str],
    severity: str,
    status: str,
    raw_payload: dict,
    idempotency_key: str | None = None,
) -> AlertAcceptedResponse:
    """Shared by both routes: build+validate the command, hand it to
    incident-core, return the response shape both endpoints use.

    Raises HTTPException(422) on a schema violation -- e.g. a Prometheus
    alert rule missing the `service`/`environment` label, or an
    unrecognized `severity` value -- exactly as the direct-POST path
    always has.
    """
    try:
        status_value = AlertStatus(status)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"unrecognized alert status {status!r}"
        ) from exc

    key = idempotency_key or derive_alert_idempotency_key(
        source=source,
        external_id=external_id,
        normalized_payload=raw_payload,
        status=status_value,
    )

    with bind_context(idempotency_key=key):
        try:
            command = AlertReceivedCommand(
                idempotency_key=key,
                source=source,
                external_id=external_id,
                labels=labels,
                annotations=annotations,
                severity=severity,  # type: ignore[arg-type]
                status=status_value,
                raw_payload=raw_payload,
            )
        except ValidationError as exc:
            # exc.errors() embeds the raw exception object in `ctx` for
            # value_error cases, which isn't JSON-serializable -- use the
            # JSON-safe serialization pydantic already provides instead.
            errors = json.loads(exc.json())
            log.warning("alert_received.validation_failed", errors=errors)
            raise HTTPException(status_code=422, detail=errors) from exc

        log.info("alert_received.accepted", source=source.value, status=status_value.value)
        try:
            result = core.handle_alert_received(command)
        except ConcurrentModificationError as exc:
            # Retryable by construction (commands are idempotent): tell the
            # sender to redeliver rather than reporting a server fault.
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return AlertAcceptedResponse(
        alert_id=result.alert_id,
        incident_id=result.incident_id,
        incident_created=result.incident_created,
        incident_status=result.incident_status,
    )


@router.post("/alerts", status_code=202, response_model=AlertAcceptedResponse)
def receive_alert(
    request: IncomingAlertRequest,
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    core: IncidentCoreService = Depends(get_incident_core_service),
) -> AlertAcceptedResponse:
    request_id = x_request_id or str(uuid.uuid4())
    normalized_payload = request.model_dump(mode="json")

    with bind_context(request_id=request_id):
        return _accept_alert(
            core,
            source=request.source,
            external_id=request.external_id,
            labels=request.labels,
            annotations=request.annotations,
            severity=request.severity,
            status=request.status,
            raw_payload=normalized_payload,
            idempotency_key=request.idempotency_key,
        )


# --- Alertmanager webhook adapter -------------------------------------------
#
# Native Alertmanager webhook_configs payload shape (fixed by Alertmanager
# itself -- https://prometheus.io/docs/alerting/latest/configuration/#webhook_config):
# a single POST batching every alert in the notification group under
# `alerts[]`. Each alert becomes one call to `_accept_alert` above, so
# idempotency/correlation/persistence are byte-for-byte the same path the
# direct-POST endpoint uses.


class AlertmanagerAlert(BaseModel):
    status: str
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    generatorURL: str | None = None
    fingerprint: str | None = None


class AlertmanagerWebhookPayload(BaseModel):
    version: str | None = None
    groupKey: str | None = None
    status: str | None = None
    receiver: str | None = None
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str | None = None
    alerts: list[AlertmanagerAlert]


class AlertmanagerWebhookResponse(BaseModel):
    accepted: list[AlertAcceptedResponse]


def _alertmanager_episode_id(alert: AlertmanagerAlert) -> str | None:
    """Identity of one firing *episode* of an Alertmanager alert.

    Alertmanager's `fingerprint` hashes the label set only, so the same
    alert firing, resolving, and firing again an hour later has one
    fingerprint across both episodes -- using it alone (Phase 3) meant the
    second episode deduped against the first forever. `startsAt` is fixed
    for the life of an episode and repeated verbatim on its resolved
    notification, so `fingerprint:startsAt` is exactly "this episode":
    the firing and resolved notifications for one episode share it (so the
    resolution finds the alert it ends), and a new episode gets a new one.
    `startsAt` is normalized to UTC so formatting differences can't split
    one episode into two.
    """
    if not alert.fingerprint:
        return None
    if not alert.startsAt:
        return alert.fingerprint
    try:
        started = datetime.fromisoformat(alert.startsAt.replace("Z", "+00:00"))
    except ValueError:
        return f"{alert.fingerprint}:{alert.startsAt}"
    return f"{alert.fingerprint}:{started.astimezone(UTC).isoformat()}"


def _check_webhook_auth(authorization: str | None) -> None:
    expected = get_settings().alertmanager_webhook_token
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        log.warning("alertmanager_webhook.unauthorized")
        raise HTTPException(status_code=401, detail="invalid or missing webhook credentials")


@router.post("/alerts/alertmanager", status_code=202, response_model=AlertmanagerWebhookResponse)
def receive_alertmanager_webhook(
    payload: AlertmanagerWebhookPayload,
    request: Request,
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    authorization: str | None = Header(default=None),
    core: IncidentCoreService = Depends(get_incident_core_service),
) -> AlertmanagerWebhookResponse:
    _check_webhook_auth(authorization)
    request_id = x_request_id or str(uuid.uuid4())

    accepted: list[AlertAcceptedResponse] = []
    with bind_context(request_id=request_id):
        for alert in payload.alerts:
            # Mapping (docs/architecture/14-observability-and-chaos.md):
            #   source        -> always "prometheus" (Alertmanager's origin here)
            #   external_id   -> alert.fingerprint + ":" + startsAt -- one
            #                    *firing episode* of that label set (Phase 4;
            #                    see _alertmanager_episode_id)
            #   labels        -> alert.labels verbatim (already carries
            #                    service/environment/region/severity per
            #                    infrastructure/prometheus/alerts/*.yml)
            #   annotations   -> alert.annotations verbatim (summary/
            #                    description/runbook_url)
            #   severity      -> labels["severity"], validated against
            #                    AlertSeverity by AlertReceivedCommand
            #   status        -> alert.status ("firing"/"resolved" match
            #                    AlertStatus's own values exactly)
            accepted.append(
                _accept_alert(
                    core,
                    source=AlertSource.PROMETHEUS,
                    external_id=_alertmanager_episode_id(alert),
                    labels=alert.labels,
                    annotations=alert.annotations,
                    severity=alert.labels.get("severity", ""),
                    status=alert.status,
                    raw_payload=alert.model_dump(mode="json"),
                )
            )

    return AlertmanagerWebhookResponse(accepted=accepted)
