"""Phase 2: event transport + correlation-engine support.

- `outbox_events.producer`: which service emitted the event (canonical
  envelope field, see docs/architecture/05-event-model.md).
- `outbox_events.publish_attempts` / `last_publish_error`: relay retry
  bookkeeping/observability (ADR-0014). Diagnostic only.
- `consumed_events`: consumer-side idempotency ledger, keyed by the
  event's own `event_id`, not the Redis message id (ADR-0014).
- `ix_incidents_service_environment_status`: supports the correlation
  engine's candidate-incident query
  (`repository.find_open_incident_candidates`).
- `ix_alerts_incident_id_received_at`: supports finding an incident's most
  recently received alert (used for temporal-proximity scoring).

Revision ID: 0002_events_correlation
Revises: 0001_initial_schema
Create Date: 2026-09-24

Note: kept short (<=32 chars) deliberately -- Alembic's default
`alembic_version.version_num` column is `VARCHAR(32)`, and the first
draft of this migration used the full descriptive slug as its revision id
and failed on `alembic upgrade` with a truncation error. Short, still
readable revision ids from here on.

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_events_correlation"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "incident_core"


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("producer", sa.String(), nullable=False, server_default="incident-core"),
        schema=SCHEMA,
    )
    op.add_column(
        "outbox_events",
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
        schema=SCHEMA,
    )
    op.add_column(
        "outbox_events",
        sa.Column("last_publish_error", sa.String(), nullable=True),
        schema=SCHEMA,
    )

    op.create_table(
        "consumed_events",
        sa.Column("consumer_name", sa.String(), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA,
    )

    op.create_index(
        "ix_incidents_service_environment_status",
        "incidents",
        ["service", "environment", "status"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_alerts_incident_id_received_at",
        "alerts",
        ["incident_id", sa.text("received_at DESC")],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_alerts_incident_id_received_at", table_name="alerts", schema=SCHEMA)
    op.drop_index("ix_incidents_service_environment_status", table_name="incidents", schema=SCHEMA)
    op.drop_table("consumed_events", schema=SCHEMA)
    op.drop_column("outbox_events", "last_publish_error", schema=SCHEMA)
    op.drop_column("outbox_events", "publish_attempts", schema=SCHEMA)
    op.drop_column("outbox_events", "producer", schema=SCHEMA)
