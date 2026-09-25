"""Result types returned by incident-core's command handlers.

These are also what gets stored verbatim in `processed_commands.result`
(JSONB), so a replayed duplicate command returns exactly what the original
call returned. See docs/architecture/06-database-design.md.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class AlertReceivedResult(BaseModel):
    """`incident_id` is None only for a resolved notification whose firing
    episode was never seen (or a late firing notification for an episode
    that already resolved without one) -- such an alert is recorded but
    never opens an incident. See docs/architecture/04-incident-state-machine.md,
    "Alert resolution (Phase 4)".

    `incident_status` is the incident's status *after* this command; it's
    optional only so ledger rows written before Phase 4 still validate.
    """

    model_config = ConfigDict(frozen=True)

    alert_id: uuid.UUID
    incident_id: uuid.UUID | None
    incident_created: bool
    incident_status: str | None = None


class EvidenceRefRegisteredResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: uuid.UUID
    incident_id: uuid.UUID
    newly_registered: bool
