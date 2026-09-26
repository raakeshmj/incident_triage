"""Phase 7: remediation proposals, policy decisions, approvals, executions,
the immutable remediation timeline, and kill switches.

- `remediations`: one proposed catalog action against one target. The
  proposal columns are immutable (trigger); only lifecycle columns change.
- `remediation_policy_decisions`: every evaluation, with the policy
  version, the catalog version, every rule's result and the exact
  PolicyEvaluationContext -- immutable (replayable, ADR-0012).
- `remediation_approvals`: at most one human decision per remediation,
  bound to the proposal hash and the decision it approved -- immutable.
- `remediation_executions`: one row per attempt, unique idempotency key.
- `remediation_timeline`: append-only audit trail -- immutable.
- `kill_switches`: runtime-writable emergency stop (global / per service).

Revision ID: 0006_remediation
Revises: 0005_hypothesis_cause
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_remediation"
down_revision = "0005_hypothesis_cause"
branch_labels = None
depends_on = None

SCHEMA = "incident_core"
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TS = sa.DateTime(timezone=True)
STATUSES = (
    "PROPOSED",
    "POLICY_REJECTED",
    "AWAITING_APPROVAL",
    "APPROVED",
    "EXECUTING",
    "EXECUTED",
    "FAILED",
    "CANCELLED",
)
PROPOSAL_COLUMNS = (
    "incident_id",
    "investigation_id",
    "idempotency_key",
    "action_id",
    "catalog_version",
    "parameters",
    "target_service",
    "environment",
    "reason",
    "expected_effect",
    "blast_radius_tier",
    "proposal_hash",
    "source",
    "proposed_by",
    "correlation_id",
    "created_at",
)


def upgrade() -> None:
    op.create_table(
        "remediations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("incident_id", UUID, sa.ForeignKey(f"{SCHEMA}.incidents.id"), nullable=False),
        sa.Column(
            "investigation_id", UUID, sa.ForeignKey(f"{SCHEMA}.investigations.id"), nullable=True
        ),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("action_id", sa.String(), nullable=False),
        sa.Column("catalog_version", sa.String(), nullable=False),
        sa.Column("parameters", JSONB, nullable=False),
        sa.Column("target_service", sa.String(), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("expected_effect", sa.String(), nullable=False),
        sa.Column("blast_radius_tier", sa.Integer(), nullable=True),
        sa.Column("proposal_hash", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("proposed_by", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("policy_decision_id", UUID, nullable=True),
        sa.Column("approval_status", sa.String(), nullable=True),
        sa.Column("execution_status", sa.String(), nullable=True),
        sa.Column("execution_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("executor_result", JSONB, nullable=True),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("verification_ref", UUID, nullable=True),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", TS, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", TS, nullable=True),
        sa.UniqueConstraint("incident_id", "idempotency_key", name="uq_remediations_idempotency"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")",
            name="ck_remediations_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_remediations_status_lease",
        "remediations",
        ["status", "lease_expires_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_remediations_target_created",
        "remediations",
        ["target_service", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "remediation_policy_decisions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "remediation_id", UUID, sa.ForeignKey(f"{SCHEMA}.remediations.id"), nullable=False
        ),
        sa.Column("proposal_hash", sa.String(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("catalog_version", sa.String(), nullable=True),
        sa.Column("catalog_digest", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("blast_radius_tier", sa.Integer(), nullable=True),
        sa.Column("required_approver_roles", JSONB, nullable=False),
        sa.Column("rules", JSONB, nullable=False),
        sa.Column("reasons", JSONB, nullable=False),
        sa.Column("policy_context", JSONB, nullable=False),
        sa.Column("evaluated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "decision IN ('ALLOW', 'DENY', 'REQUIRE_APPROVAL')", name="ck_policy_decision"
        ),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_remediations_policy_decision",
        "remediations",
        "remediation_policy_decisions",
        ["policy_decision_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )

    op.create_table(
        "remediation_approvals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "remediation_id",
            UUID,
            sa.ForeignKey(f"{SCHEMA}.remediations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "policy_decision_id",
            UUID,
            sa.ForeignKey(f"{SCHEMA}.remediation_policy_decisions.id"),
            nullable=False,
        ),
        sa.Column("proposal_hash", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("approver", sa.String(), nullable=False),
        sa.Column("approver_role", sa.String(), nullable=True),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("decided_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'timed_out')", name="ck_approval_decision"
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "remediation_executions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "remediation_id", UUID, sa.ForeignKey(f"{SCHEMA}.remediations.id"), nullable=False
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False, unique=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("executor", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("started_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("deadline_at", TS, nullable=False),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.UniqueConstraint("remediation_id", "attempt_number", name="uq_execution_attempt"),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'UNKNOWN')",
            name="ck_execution_status",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "remediation_timeline",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "remediation_id", UUID, sa.ForeignKey(f"{SCHEMA}.remediations.id"), nullable=False
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("action_id", sa.String(), nullable=False),
        sa.Column("catalog_version", sa.String(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=True),
        sa.Column("details", JSONB, nullable=False),
        sa.Column("occurred_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("remediation_id", "sequence", name="uq_timeline_sequence"),
        schema=SCHEMA,
    )

    op.create_table(
        "kill_switches",
        sa.Column("scope", sa.String(), primary_key=True),
        sa.Column("engaged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("changed_by", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("changed_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "scope = 'global' OR scope LIKE 'service:%'", name="ck_kill_switch_scope"
        ),
        schema=SCHEMA,
    )

    for table in (
        "remediation_policy_decisions",
        "remediation_approvals",
        "remediation_timeline",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
                BEFORE UPDATE OR DELETE ON {SCHEMA}.{table}
                FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_row_mutation();
            """
        )
    changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in PROPOSAL_COLUMNS)
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_proposal_mutation() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'remediations rows cannot be deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF {changed} THEN
                RAISE EXCEPTION 'a remediation proposal is immutable; propose a new one'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER remediations_proposal_immutable
            BEFORE UPDATE OR DELETE ON {SCHEMA}.remediations
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_proposal_mutation();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS remediations_proposal_immutable ON {SCHEMA}.remediations")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_proposal_mutation()")
    for table in (
        "remediation_policy_decisions",
        "remediation_approvals",
        "remediation_timeline",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {SCHEMA}.{table}")
    op.drop_table("kill_switches", schema=SCHEMA)
    op.drop_table("remediation_timeline", schema=SCHEMA)
    op.drop_table("remediation_executions", schema=SCHEMA)
    op.drop_table("remediation_approvals", schema=SCHEMA)
    op.drop_constraint(
        "fk_remediations_policy_decision", "remediations", schema=SCHEMA, type_="foreignkey"
    )
    op.drop_table("remediation_policy_decisions", schema=SCHEMA)
    op.drop_index("ix_remediations_target_created", table_name="remediations", schema=SCHEMA)
    op.drop_index("ix_remediations_status_lease", table_name="remediations", schema=SCHEMA)
    op.drop_table("remediations", schema=SCHEMA)
