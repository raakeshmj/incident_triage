"""Phase 8: verification, pre-remediation baselines, and RESOLVED closing
an incident for correlation.

- `remediation_baselines`: what was observed just before a remediation
  executed (immutable) -- the reference verification compares against.
- `verifications`: one per executed remediation (unique), with the
  resolved spec and baseline (immutable), status, streak, deadline and a
  fenced lease (`lease_owner` + `claim_attempt`).
- `verification_observations`: every poll's check results (immutable).
- `verification_evidence`: the normalized, FK-enforced link from a
  verification to every evidence ref it used (baseline and polls) --
  06-database-design.md's `verification_evidence` (immutable).
- `incidents_open_correlation_key` now also excludes RESOLVED: a new alert
  after a verified recovery opens a new incident instead of being absorbed
  into the resolved one.

Revision ID: 0007_verification
Revises: 0006_remediation
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_verification"
down_revision = "0006_remediation"
branch_labels = None
depends_on = None

SCHEMA = "incident_core"
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TS = sa.DateTime(timezone=True)
FROZEN_COLUMNS = (
    "incident_id",
    "remediation_id",
    "verification_type",
    "policy_version",
    "spec",
    "baseline",
    "correlation_id",
    "created_at",
)


def upgrade() -> None:
    op.create_table(
        "remediation_baselines",
        sa.Column(
            "remediation_id", UUID, sa.ForeignKey(f"{SCHEMA}.remediations.id"), primary_key=True
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("values", JSONB, nullable=False),
        sa.Column("evidence_ids", JSONB, nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("captured_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('captured', 'unavailable')", name="ck_baseline_status"),
        schema=SCHEMA,
    )
    op.create_table(
        "verifications",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("incident_id", UUID, sa.ForeignKey(f"{SCHEMA}.incidents.id"), nullable=False),
        sa.Column(
            "remediation_id",
            UUID,
            sa.ForeignKey(f"{SCHEMA}.remediations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("verification_type", sa.String(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column("baseline", JSONB, nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("consecutive_successes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conclusive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.String(), nullable=True),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("next_action", sa.String(), nullable=True),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("grace_until", TS, nullable=True),
        sa.Column("deadline_at", TS, nullable=True),
        sa.Column("next_poll_at", TS, nullable=True),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", TS, nullable=True),
        sa.Column("claim_attempt", sa.Integer(), nullable=False, server_default="0"),
        # alerts already linked when verification started: any other firing
        # alert is new (clock-free; alert timestamps come from the DB clock)
        sa.Column("known_alert_ids", JSONB, nullable=False, server_default="[]"),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'PASSED', 'FAILED', 'TIMED_OUT')",
            name="ck_verification_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_verifications_status_poll", "verifications", ["status", "next_poll_at"], schema=SCHEMA
    )
    op.create_table(
        "verification_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "verification_id", UUID, sa.ForeignKey(f"{SCHEMA}.verifications.id"), nullable=False
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("claim_attempt", sa.Integer(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("conclusive", sa.Boolean(), nullable=False),
        sa.Column("checks", JSONB, nullable=False),
        sa.Column("errors", JSONB, nullable=False),
        sa.Column("evidence_ids", JSONB, nullable=False),
        sa.UniqueConstraint("verification_id", "sequence", name="uq_verification_observation"),
        schema=SCHEMA,
    )
    op.create_table(
        "verification_evidence",
        sa.Column(
            "verification_id", UUID, sa.ForeignKey(f"{SCHEMA}.verifications.id"), nullable=False
        ),
        sa.Column("evidence_id", UUID, sa.ForeignKey(f"{SCHEMA}.evidence_refs.id"), nullable=False),
        sa.Column("poll_sequence", sa.Integer(), nullable=False),  # 0 = baseline
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("collected_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("verification_id", "evidence_id"),
        sa.CheckConstraint(
            "role IN ('baseline', 'observation')", name="ck_verification_evidence_role"
        ),
        schema=SCHEMA,
    )
    for table in ("remediation_baselines", "verification_observations", "verification_evidence"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
                BEFORE UPDATE OR DELETE ON {SCHEMA}.{table}
                FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_row_mutation();
            """
        )
    changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in FROZEN_COLUMNS)
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_verification_spec_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'verifications rows cannot be deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF {changed} THEN
                RAISE EXCEPTION 'a verification spec and baseline are immutable'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER verifications_spec_immutable
            BEFORE UPDATE OR DELETE ON {SCHEMA}.verifications
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_verification_spec_mutation();
        """
    )
    op.drop_index("incidents_open_correlation_key", table_name="incidents", schema=SCHEMA)
    op.create_index(
        "incidents_open_correlation_key",
        "incidents",
        ["correlation_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status NOT IN ('CLOSED', 'CANCELLED', 'SUPPRESSED', 'RESOLVED')"),
    )


def downgrade() -> None:
    op.drop_index("incidents_open_correlation_key", table_name="incidents", schema=SCHEMA)
    op.create_index(
        "incidents_open_correlation_key",
        "incidents",
        ["correlation_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status NOT IN ('CLOSED', 'CANCELLED', 'SUPPRESSED')"),
    )
    op.execute(f"DROP TRIGGER IF EXISTS verifications_spec_immutable ON {SCHEMA}.verifications")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_verification_spec_mutation()")
    for table in ("remediation_baselines", "verification_observations", "verification_evidence"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {SCHEMA}.{table}")
    op.drop_table("verification_evidence", schema=SCHEMA)
    op.drop_table("verification_observations", schema=SCHEMA)
    op.drop_index("ix_verifications_status_poll", table_name="verifications", schema=SCHEMA)
    op.drop_table("verifications", schema=SCHEMA)
    op.drop_table("remediation_baselines", schema=SCHEMA)
