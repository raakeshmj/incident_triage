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

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, func
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
