"""Initial incident_core schema: incidents, alerts, outbox_events, processed_commands.

Phase 1 scope only -- see docs/architecture/06-database-design.md.
Every constraint here is load-bearing, not incidental:

- `incidents_open_correlation_key`: partial unique index, at most one open
  incident per correlation_key (docs/architecture/04-incident-state-machine.md).
- `alerts_source_external_id`: partial unique index, alert dedup for
  sources with a stable external_id (06-database-design.md, "Alert
  deduplication and retries").
- `processed_commands` composite primary key `(command_type,
  idempotency_key)`: scoped idempotency per ADR-0011.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "incident_core"


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("service", sa.String(), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("correlation_key", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "incidents_open_correlation_key",
        "incidents",
        ["correlation_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status NOT IN ('CLOSED', 'CANCELLED', 'SUPPRESSED')"),
    )

    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("labels", postgresql.JSONB(), nullable=False),
        sa.Column("annotations", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.incidents.id"),
            nullable=True,
        ),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_alerts_fingerprint", "alerts", ["fingerprint"], schema=SCHEMA)
    op.create_index("ix_alerts_incident_id", "alerts", ["incident_id"], schema=SCHEMA)
    op.create_index(
        "alerts_source_external_id",
        "alerts",
        ["source", "external_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )

    op.create_table(
        "outbox_events",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("aggregate_type", sa.String(), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outbox_events_aggregate_id_sequence",
        "outbox_events",
        ["aggregate_id", "sequence"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outbox_events_unpublished",
        "outbox_events",
        ["published_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "processed_commands",
        sa.Column("command_type", sa.String(), primary_key=True),
        sa.Column("idempotency_key", sa.String(), primary_key=True),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("processed_commands", schema=SCHEMA)
    op.drop_table("outbox_events", schema=SCHEMA)
    op.drop_table("alerts", schema=SCHEMA)
    op.drop_table("incidents", schema=SCHEMA)
