"""Phase 5: investigations, hypotheses, the investigation trace, RCA reports.

- `investigations`: one attempt at explaining one incident; unique
  `(incident_id, attempt_number)` makes "start investigation" idempotent
  under duplicate delivery. Lease columns fence concurrent workers.
- `hypotheses` + `hypothesis_evidence_links`: evidence citations are rows
  with foreign keys to `evidence_refs` -- a hypothesis cannot reference an
  evidence id that was never registered.
- `investigation_steps`: the append-only, replayable trace (context, every
  model turn, every tool call, every hypothesis change, every rejection).
- `rca_reports`: the structured RCA, one per completed investigation.
- `evidence_refs.investigation_id` gets the FK ADR-0018 deferred to this phase.

Revision ID: 0004_investigations
Revises: 0003_resolution_evidence
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_investigations"
down_revision: str | None = "0003_resolution_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "incident_core"
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("incident_id", UUID, sa.ForeignKey(f"{SCHEMA}.incidents.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("model_provider", sa.String(), nullable=False),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.Column("model_config", JSONB, nullable=False),
        sa.Column("budget", JSONB, nullable=False),
        sa.Column("iteration_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_creation_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_action", sa.String(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("inconclusive_reason", sa.Text(), nullable=True),
        sa.Column("selected_hypothesis_id", UUID, nullable=True),
        sa.Column("final_result", JSONB, nullable=True),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("incident_id", "attempt_number", name="uq_investigations_attempt"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_investigations_status_lease",
        "investigations",
        ["status", "lease_expires_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "hypotheses",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "investigation_id", UUID, sa.ForeignKey(f"{SCHEMA}.investigations.id"), nullable=False
        ),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("missing_evidence", JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("investigation_id", "key", name="uq_hypotheses_key"),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_investigations_selected_hypothesis",
        "investigations",
        "hypotheses",
        ["selected_hypothesis_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )

    op.create_table(
        "hypothesis_evidence_links",
        sa.Column(
            "hypothesis_id", UUID, sa.ForeignKey(f"{SCHEMA}.hypotheses.id"), primary_key=True
        ),
        sa.Column(
            "evidence_id", UUID, sa.ForeignKey(f"{SCHEMA}.evidence_refs.id"), primary_key=True
        ),
        sa.Column("relation", sa.String(), nullable=False),
        sa.Column("linked_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "relation IN ('supports', 'contradicts')", name="ck_hypothesis_evidence_relation"
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "investigation_steps",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "investigation_id", UUID, sa.ForeignKey(f"{SCHEMA}.investigations.id"), nullable=False
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("call_id", sa.String(), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("investigation_id", "sequence", name="uq_investigation_steps_seq"),
        schema=SCHEMA,
    )

    op.create_table(
        "rca_reports",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("incident_id", UUID, sa.ForeignKey(f"{SCHEMA}.incidents.id"), nullable=False),
        sa.Column(
            "investigation_id",
            UUID,
            sa.ForeignKey(f"{SCHEMA}.investigations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "root_cause_hypothesis_id",
            UUID,
            sa.ForeignKey(f"{SCHEMA}.hypotheses.id"),
            nullable=False,
        ),
        sa.Column("report", JSONB, nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("generated_at", TS, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )

    op.create_foreign_key(
        "fk_evidence_refs_investigation",
        "evidence_refs",
        "investigations",
        ["investigation_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_row_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable (% attempted)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ("investigation_steps", "rca_reports", "hypothesis_evidence_links"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
                BEFORE UPDATE OR DELETE ON {SCHEMA}.{table}
                FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_row_mutation();
            """
        )


def downgrade() -> None:
    for table in ("investigation_steps", "rca_reports", "hypothesis_evidence_links"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {SCHEMA}.{table}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_row_mutation()")
    op.drop_constraint(
        "fk_evidence_refs_investigation", "evidence_refs", schema=SCHEMA, type_="foreignkey"
    )
    op.drop_table("rca_reports", schema=SCHEMA)
    op.drop_table("investigation_steps", schema=SCHEMA)
    op.drop_table("hypothesis_evidence_links", schema=SCHEMA)
    op.drop_constraint(
        "fk_investigations_selected_hypothesis", "investigations", schema=SCHEMA, type_="foreignkey"
    )
    op.drop_table("hypotheses", schema=SCHEMA)
    op.drop_index("ix_investigations_status_lease", table_name="investigations", schema=SCHEMA)
    op.drop_table("investigations", schema=SCHEMA)
