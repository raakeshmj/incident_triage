"""The `evidence.evidence_records` table.

Insert-only: migration 0001 installs a trigger that rejects UPDATE and
DELETE, so immutability is a database guarantee, not a convention. A retry
of the same query produces a *new* record with a new `collected_at`
(docs/architecture/08-evidence-model.md, "How that's enforced", point 4).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Identity, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.evidence.db.base import SCHEMA, Base


class EvidenceRecordRow(Base):
    __tablename__ = "evidence_records"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Insertion order: the deterministic tiebreak for replay ordering.
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    # No FK: incidents live in another schema this role has no grant on.
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    evidence_type: Mapped[str] = mapped_column(String, nullable=False)
    source_system: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)
    subject_service: Mapped[str] = mapped_column(String, nullable=False)
    query_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_reference: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    window_end: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    collected_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    raw_response: Mapped[Any] = mapped_column(JSONB, nullable=False)
    raw_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
