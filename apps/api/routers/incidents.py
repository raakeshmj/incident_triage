"""incident-core's query surface: GET /api/v1/incidents/{incident_id}."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from apps.api.dependencies import get_incident_core_service
from packages.domain.views import IncidentView
from packages.incident.service import IncidentCoreService

router = APIRouter(prefix="/api/v1", tags=["incidents"])


@router.get("/incidents/{incident_id}", response_model=IncidentView)
def get_incident(
    incident_id: uuid.UUID,
    core: IncidentCoreService = Depends(get_incident_core_service),
) -> IncidentView:
    view = core.get_incident_view(incident_id)
    if view is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return view
