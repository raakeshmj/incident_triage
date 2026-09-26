from __future__ import annotations

from fastapi import FastAPI

from apps.api.config import get_settings
from apps.api.routers import alerts, incidents, operations, remediations
from packages.telemetry.logging import configure_logging

configure_logging(get_settings().log_level)

app = FastAPI(title="Incident Intelligence API", version="0.1.0")
app.include_router(alerts.router)
app.include_router(incidents.router)
app.include_router(remediations.router)
app.include_router(operations.router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
