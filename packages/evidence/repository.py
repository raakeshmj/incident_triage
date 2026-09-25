"""Persistence for evidence records: insert and read. There is no update or
delete function here, and the table's trigger would reject one anyway."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from packages.evidence.db.models import EvidenceRecordRow
from packages.evidence.models import EvidenceRecord
from packages.evidence.types import EvidenceType, SourceSystem


def insert_record(session: Session, record: EvidenceRecord) -> EvidenceRecord:
    """Insert a new record; returns it with its DB-assigned `sequence`."""
    stmt = (
        pg_insert(EvidenceRecordRow)
        .values(
            id=record.evidence_id,
            incident_id=record.incident_id,
            investigation_id=record.investigation_id,
            evidence_type=record.evidence_type.value,
            source_system=record.source_system.value,
            operation=record.operation,
            subject_service=record.subject_service,
            query_spec=record.query_spec,
            source_reference=record.source_reference,
            observed_at=record.observed_at,
            window_start=record.window_start,
            window_end=record.window_end,
            collected_at=record.collected_at,
            expires_at=record.expires_at,
            requested_by=record.requested_by,
            content_hash=record.content_hash,
            raw_response=record.raw_response,
            raw_truncated=record.raw_truncated,
            normalized_payload=record.normalized_payload,
            summary=record.summary,
            result_count=record.result_count,
        )
        .returning(EvidenceRecordRow.sequence)
    )
    sequence = session.execute(stmt).scalar_one()
    return record.model_copy(update={"sequence": sequence})


def get_record(session: Session, evidence_id: uuid.UUID) -> EvidenceRecord | None:
    row = session.get(EvidenceRecordRow, evidence_id)
    return _to_record(row) if row is not None else None


def list_incident_records(session: Session, incident_id: uuid.UUID) -> list[EvidenceRecord]:
    """Deterministic replay order: collection time, then insertion order."""
    stmt = (
        select(EvidenceRecordRow)
        .where(EvidenceRecordRow.incident_id == incident_id)
        .order_by(EvidenceRecordRow.collected_at, EvidenceRecordRow.sequence)
    )
    return [_to_record(row) for row in session.execute(stmt).scalars()]


def _to_record(row: EvidenceRecordRow) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=row.id,
        sequence=row.sequence,
        incident_id=row.incident_id,
        investigation_id=row.investigation_id,
        evidence_type=EvidenceType(row.evidence_type),
        source_system=SourceSystem(row.source_system),
        operation=row.operation,
        subject_service=row.subject_service,
        query_spec=row.query_spec,
        source_reference=row.source_reference,
        observed_at=row.observed_at,
        window_start=row.window_start,
        window_end=row.window_end,
        collected_at=row.collected_at,
        expires_at=row.expires_at,
        requested_by=row.requested_by,
        content_hash=row.content_hash,
        raw_response=row.raw_response,
        raw_truncated=row.raw_truncated,
        normalized_payload=row.normalized_payload,
        summary=row.summary,
        result_count=row.result_count,
    )
