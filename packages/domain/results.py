"""Result types returned by incident-core's command handlers.

These are also what gets stored verbatim in `processed_commands.result`
(JSONB), so a replayed duplicate command returns exactly what the original
call returned. See docs/architecture/06-database-design.md.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class AlertReceivedResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    alert_id: uuid.UUID
    incident_id: uuid.UUID
    incident_created: bool
