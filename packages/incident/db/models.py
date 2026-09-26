"""SQLAlchemy ORM models for the `incident_core` schema.

Table shapes mirror docs/architecture/06-database-design.md exactly,
scoped to what Phase 1 needs: incidents, alerts, outbox_events,
processed_commands. Everything else in that document (investigations,
hypotheses, evidence_refs, remediation_proposals, policy_decisions, ...)
is out of scope until its own phase.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.incident.db.base import SCHEMA, Base


class IncidentRow(Base):
    __tablename__ = "incidents"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    service: Mapped[str] = mapped_column(String, nullable=False)
    environment: Mapped[str] = mapped_column(String, nullable=False)
    correlation_key: Mapped[str] = mapped_column(String, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AlertRow(Base):
    __tablename__ = "alerts"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False)
    annotations: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.incidents.id"), nullable=True
    )
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Phase 4: set once, when the alert's firing episode ends.
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"
    __table_args__ = {"schema": SCHEMA}

    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    aggregate_type: Mapped[str] = mapped_column(String, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    producer: Mapped[str] = mapped_column(String, nullable=False, default="incident-core")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 2: relay retry bookkeeping -- see docs/architecture/05-event-model.md,
    # "Delivery semantics" and ADR-0014. Purely observational/diagnostic:
    # never used to decide correctness, only to explain "why hasn't this
    # published yet" without grepping logs.
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_publish_error: Mapped[str | None] = mapped_column(String, nullable=True)


class ProcessedCommandRow(Base):
    __tablename__ = "processed_commands"
    __table_args__ = {"schema": SCHEMA}

    command_type: Mapped[str] = mapped_column(String, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConsumedEventRow(Base):
    """Consumer-side idempotency ledger (Phase 2).

    Redis Streams consumer groups already prevent the *same consumer
    group* from handing one message to two consumers concurrently, but
    they do not prevent redelivery after a crash-before-ack, and they
    provide nothing at all if a consumer is ever restarted against a
    stream position it has already fully processed. This table is the
    actual duplicate-processing guard: "Do not rely only on Redis to
    prevent duplicate processing." One row per (consumer, event), keyed by
    the event's own `event_id` -- not the Redis stream message id, which
    is transport-specific and meaningless across a redelivery.
    """

    __tablename__ = "consumed_events"
    __table_args__ = {"schema": SCHEMA}

    consumer_name: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    processed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvidenceRefRow(Base):
    """incident-core's reference to an evidence record (Phase 4).

    Immutable: migration 0003 installs a trigger rejecting UPDATE/DELETE.
    The payload itself lives in evidence-service's `evidence` schema, which
    incident-core's role has no grant on -- this row holds exactly what's
    needed to validate a citation (`id` exists, belongs to this incident,
    `content_hash` matches). See docs/architecture/08-evidence-model.md.
    """

    __tablename__ = "evidence_refs"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.incidents.id"), nullable=False
    )
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    evidence_type: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    collected_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# --- Phase 5: investigations (ADR-0020) ---------------------------------------


class InvestigationRow(Base):
    """One attempt at explaining one incident. Written only by incident-core;
    the investigation worker changes it exclusively through commands."""

    __tablename__ = "investigations"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.incidents.id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    model_provider: Mapped[str] = mapped_column(String, nullable=False)
    model_name: Mapped[str] = mapped_column(String, nullable=False)
    model_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    budget: Mapped[dict] = mapped_column(JSONB, nullable=False)
    iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_action: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    inconclusive_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    final_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HypothesisRow(Base):
    __tablename__ = "hypotheses"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.investigations.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    missing_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    cause_category: Mapped[str | None] = mapped_column(String, nullable=True)
    component: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HypothesisEvidenceLinkRow(Base):
    """FK-enforced citation: `evidence_id` must be a registered evidence_ref."""

    __tablename__ = "hypothesis_evidence_links"
    __table_args__ = {"schema": SCHEMA}

    hypothesis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.hypotheses.id"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.evidence_refs.id"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(String, nullable=False)  # supports | contradicts
    linked_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InvestigationStepRow(Base):
    """The append-only investigation trace (immutable via trigger)."""

    __tablename__ = "investigation_steps"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.investigations.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RcaReportRow(Base):
    __tablename__ = "rca_reports"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.incidents.id"), nullable=False
    )
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.investigations.id"), nullable=False
    )
    root_cause_hypothesis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.hypotheses.id"), nullable=False
    )
    report: Mapped[dict] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    generated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# --- Phase 7: remediation (migration 0006_remediation) ------------------------------


def _ts(nullable: bool = False, default: bool = False) -> Mapped:
    if default:
        return mapped_column(DateTime(timezone=True), nullable=nullable, server_default=func.now())
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class RemediationRow(Base):
    """A proposed catalog action. Proposal columns are immutable (trigger)."""

    __tablename__ = "remediations"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.incidents.id"), nullable=False
    )
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.investigations.id"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    action_id: Mapped[str] = mapped_column(String, nullable=False)
    catalog_version: Mapped[str] = mapped_column(String, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target_service: Mapped[str] = mapped_column(String, nullable=False)
    environment: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    expected_effect: Mapped[str] = mapped_column(String, nullable=False)
    blast_radius_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposal_hash: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    proposed_by: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    policy_decision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approval_status: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_status: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    executor_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    verification_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = _ts(nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime.datetime] = _ts(default=True)
    updated_at: Mapped[datetime.datetime] = _ts(default=True)
    completed_at: Mapped[datetime.datetime | None] = _ts(nullable=True)


class RemediationPolicyDecisionRow(Base):
    __tablename__ = "remediation_policy_decisions"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    remediation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.remediations.id"), nullable=False
    )
    proposal_hash: Mapped[str] = mapped_column(String, nullable=False)
    policy_version: Mapped[str] = mapped_column(String, nullable=False)
    catalog_version: Mapped[str | None] = mapped_column(String, nullable=True)
    catalog_digest: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    blast_radius_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_approver_roles: Mapped[list] = mapped_column(JSONB, nullable=False)
    rules: Mapped[list] = mapped_column(JSONB, nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False)
    policy_context: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evaluated_at: Mapped[datetime.datetime] = _ts(default=True)


class RemediationApprovalRow(Base):
    __tablename__ = "remediation_approvals"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    remediation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.remediations.id"), nullable=False, unique=True
    )
    policy_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.remediation_policy_decisions.id"),
        nullable=False,
    )
    proposal_hash: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    approver: Mapped[str] = mapped_column(String, nullable=False)
    approver_role: Mapped[str | None] = mapped_column(String, nullable=True)
    comment: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime.datetime] = _ts(default=True)


class RemediationExecutionRow(Base):
    __tablename__ = "remediation_executions"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    remediation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.remediations.id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    executor: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime.datetime] = _ts(default=True)
    deadline_at: Mapped[datetime.datetime] = _ts()
    completed_at: Mapped[datetime.datetime | None] = _ts(nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)


class RemediationTimelineRow(Base):
    __tablename__ = "remediation_timeline"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    remediation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.remediations.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String, nullable=True)
    to_status: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action_id: Mapped[str] = mapped_column(String, nullable=False)
    catalog_version: Mapped[str] = mapped_column(String, nullable=False)
    policy_version: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime.datetime] = _ts(default=True)


class KillSwitchRow(Base):
    __tablename__ = "kill_switches"
    __table_args__ = {"schema": SCHEMA}

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    engaged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    changed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    changed_at: Mapped[datetime.datetime] = _ts(default=True)


# --- Phase 8: verification (migration 0007_verification) -----------------------------


class RemediationBaselineRow(Base):
    __tablename__ = "remediation_baselines"
    __table_args__ = {"schema": SCHEMA}

    remediation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.remediations.id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    captured_at: Mapped[datetime.datetime] = _ts(default=True)


class VerificationRow(Base):
    """Spec, baseline and identity columns are immutable (trigger)."""

    __tablename__ = "verifications"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.incidents.id"), nullable=False
    )
    remediation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.remediations.id"), nullable=False, unique=True
    )
    verification_type: Mapped[str] = mapped_column(String, nullable=False)
    policy_version: Mapped[str] = mapped_column(String, nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    baseline: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    consecutive_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conclusive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    next_action: Mapped[str | None] = mapped_column(String, nullable=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    grace_until: Mapped[datetime.datetime | None] = _ts(nullable=True)
    deadline_at: Mapped[datetime.datetime | None] = _ts(nullable=True)
    next_poll_at: Mapped[datetime.datetime | None] = _ts(nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = _ts(nullable=True)
    claim_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    known_alert_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    started_at: Mapped[datetime.datetime | None] = _ts(nullable=True)
    completed_at: Mapped[datetime.datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime.datetime] = _ts(default=True)
    updated_at: Mapped[datetime.datetime] = _ts(default=True)


class VerificationObservationRow(Base):
    __tablename__ = "verification_observations"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    verification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.verifications.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime.datetime] = _ts()
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    conclusive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checks: Mapped[list] = mapped_column(JSONB, nullable=False)
    errors: Mapped[list] = mapped_column(JSONB, nullable=False)
    evidence_ids: Mapped[list] = mapped_column(JSONB, nullable=False)


class VerificationEvidenceRow(Base):
    __tablename__ = "verification_evidence"
    __table_args__ = {"schema": SCHEMA}

    verification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.verifications.id"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.evidence_refs.id"), primary_key=True
    )
    poll_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    collected_at: Mapped[datetime.datetime] = _ts()
