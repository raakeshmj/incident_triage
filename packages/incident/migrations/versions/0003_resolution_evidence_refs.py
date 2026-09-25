"""Phase 4: alert resolution + evidence references.

- `alerts.resolved_at`: when a linked alert's firing episode ended.
  `alerts.status` flips firing -> resolved exactly once; nothing else on an
  alert row is ever updated.
- `evidence_refs`: incident-core's reference to each evidence record
  evidence-service persisted (docs/architecture/06-database-design.md,
  08-evidence-model.md). Metadata + `content_hash` only -- the payload
  lives in the `evidence` schema, which this role cannot read.
  Deviation from the doc's sketch, deliberately: keyed to `incident_id`
  (FK) with a *nullable* `investigation_id` and no FK on it yet, because
  `investigations` doesn't exist until the investigation agent does
  (Phase 5). That FK is added when the table it points at is.
- A trigger rejects UPDATE/DELETE on `evidence_refs`: evidence references
  are immutable at the database level, not just by convention.

Revision ID: 0003_resolution_evidence
Revises: 0002_events_correlation
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_resolution_evidence"
down_revision: str | None = "0002_events_correlation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "incident_core"


def upgrade() -> None:
    op.add_column(
        "alerts",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_alerts_incident_id_status", "alerts", ["incident_id", "status"], schema=SCHEMA
    )

    op.create_table(
        "evidence_refs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.incidents.id"),
            nullable=False,
        ),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_type", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("source_system", sa.String(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_evidence_refs_incident_id_collected_at",
        "evidence_refs",
        ["incident_id", "collected_at"],
        schema=SCHEMA,
    )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_evidence_ref_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'evidence_refs rows are immutable (% attempted)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER evidence_refs_immutable
            BEFORE UPDATE OR DELETE ON {SCHEMA}.evidence_refs
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_evidence_ref_mutation();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS evidence_refs_immutable ON {SCHEMA}.evidence_refs")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_evidence_ref_mutation()")
    op.drop_index(
        "ix_evidence_refs_incident_id_collected_at", table_name="evidence_refs", schema=SCHEMA
    )
    op.drop_table("evidence_refs", schema=SCHEMA)
    op.drop_index("ix_alerts_incident_id_status", table_name="alerts", schema=SCHEMA)
    op.drop_column("alerts", "resolved_at", schema=SCHEMA)
