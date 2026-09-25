"""Evidence records: immutable, content-hashed observations.

Revision ID: 0001_evidence_records
Revises:
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_evidence_records"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "evidence"


def upgrade() -> None:
    op.create_table(
        "evidence_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_type", sa.String(), nullable=False),
        sa.Column("source_system", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("subject_service", sa.String(), nullable=False),
        sa.Column("query_spec", postgresql.JSONB(), nullable=False),
        sa.Column("source_reference", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("raw_response", postgresql.JSONB(), nullable=False),
        sa.Column("raw_truncated", sa.Boolean(), nullable=False),
        sa.Column("normalized_payload", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("sequence", name="uq_evidence_records_sequence"),
        sa.CheckConstraint(
            "content_hash ~ '^sha256:[0-9a-f]{64}$'", name="ck_evidence_records_content_hash"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_evidence_records_incident_order",
        "evidence_records",
        ["incident_id", "collected_at", "sequence"],
        schema=SCHEMA,
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_evidence_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'evidence records are immutable (% attempted)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER evidence_records_immutable
            BEFORE UPDATE OR DELETE ON {SCHEMA}.evidence_records
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_evidence_mutation();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS evidence_records_immutable ON {SCHEMA}.evidence_records")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_evidence_mutation()")
    op.drop_index(
        "ix_evidence_records_incident_order", table_name="evidence_records", schema=SCHEMA
    )
    op.drop_table("evidence_records", schema=SCHEMA)
