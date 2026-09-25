"""Phase 6: structured cause on hypotheses (category + component).

Revision ID: 0005_hypothesis_cause
Revises: 0004_investigations
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_hypothesis_cause"
down_revision = "0004_investigations"
branch_labels = None
depends_on = None

SCHEMA = "incident_core"


def upgrade() -> None:
    op.add_column(
        "hypotheses", sa.Column("cause_category", sa.String(), nullable=True), schema=SCHEMA
    )
    op.add_column("hypotheses", sa.Column("component", sa.String(), nullable=True), schema=SCHEMA)
    op.create_check_constraint(
        "ck_hypotheses_cause_category",
        "hypotheses",
        "cause_category IS NULL OR cause_category IN ('deployment', 'configuration', "
        "'code_change', 'dependency', 'database', 'resource_cpu', 'resource_memory', "
        "'traffic', 'infrastructure', 'other')",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint("ck_hypotheses_cause_category", "hypotheses", schema=SCHEMA)
    op.drop_column("hypotheses", "component", schema=SCHEMA)
    op.drop_column("hypotheses", "cause_category", schema=SCHEMA)
