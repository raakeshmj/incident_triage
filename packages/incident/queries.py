"""incident-core's read model for the operations console (Phase 8).

Read-only queries over incident-core's own tables, shaped for the
dashboard API. No decisions are made here: statuses, verdicts and policy
outcomes are read as persisted, never recomputed. The lifecycle timeline is
a merge, by timestamp, of records that already exist (alerts, incident
status changes, investigation steps, RCA, remediation timeline,
verification observations and verdicts).

Metrics are computed only from recorded timestamps; a metric with no
underlying records is returned with `count: 0` and no values.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from packages.domain.events import EVENT_TYPE_INCIDENT_STATUS_CHANGED
from packages.incident.db.models import (
    AlertRow,
    EvidenceRefRow,
    IncidentRow,
    InvestigationRow,
    OutboxEventRow,
    RemediationRow,
    VerificationRow,
)
from packages.incident.investigations import InvestigationCoreService
from packages.incident.remediations import RemediationCoreService
from packages.incident.verifications import VerificationCoreService

ACTIVE_STATUSES = (
    "TRIAGING",
    "INVESTIGATING",
    "RCA_READY",
    "AWAITING_APPROVAL",
    "REMEDIATION_IN_PROGRESS",
    "VERIFYING",
    "VERIFICATION_FAILED",
)
FINAL_STATUSES = ("RESOLVED", "CANCELLED", "ESCALATED", "CLOSED", "SUPPRESSED")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class IncidentQueryService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        investigations: InvestigationCoreService,
        remediations: RemediationCoreService,
        verifications: VerificationCoreService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._investigations = investigations
        self._remediations = remediations
        self._verifications = verifications
        self._clock = clock

    # --- list ----------------------------------------------------------------------

    def list_incidents(
        self,
        *,
        statuses: list[str] | None = None,
        severities: list[str] | None = None,
        service: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            stmt = select(IncidentRow)
            if statuses:
                stmt = stmt.where(IncidentRow.status.in_(statuses))
            if severities:
                stmt = stmt.where(IncidentRow.severity.in_(severities))
            if service:
                stmt = stmt.where(IncidentRow.service == service)
            if since:
                stmt = stmt.where(IncidentRow.created_at >= since)
            if until:
                stmt = stmt.where(IncidentRow.created_at <= until)
            total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
            rows = list(
                session.execute(
                    stmt.order_by(IncidentRow.created_at.desc()).limit(limit).offset(offset)
                ).scalars()
            )
            ids = [r.id for r in rows]
            ends = self._final_times(session, ids)
            alerts = self._alert_stats(session, ids)
            latest_inv = self._latest(
                session, InvestigationRow, ids, InvestigationRow.attempt_number
            )
            latest_rem = self._latest(session, RemediationRow, ids, RemediationRow.created_at)
            latest_ver = self._latest(session, VerificationRow, ids, VerificationRow.created_at)
            services = sorted(session.execute(select(IncidentRow.service).distinct()).scalars())
            now = self._clock()
            items = []
            for r in rows:
                end = ends.get(r.id) if r.status in FINAL_STATUSES else None
                stats = alerts.get(r.id, {"count": 0, "firing": 0, "services": [r.service]})
                items.append(
                    {
                        "id": str(r.id),
                        "severity": r.severity,
                        "service": r.service,
                        "environment": r.environment,
                        "status": r.status,
                        "created_at": _iso(r.created_at),
                        "updated_at": _iso(r.updated_at),
                        "ended_at": _iso(end),
                        "duration_seconds": round(((end or now) - r.created_at).total_seconds(), 1),
                        "alert_count": stats["count"],
                        "firing_alert_count": stats["firing"],
                        "affected_services": stats["services"],
                        "affected_service_count": len(stats["services"]),
                        "attempt_count": r.attempt_count,
                        "investigation_status": latest_inv.get(r.id),
                        "remediation_status": latest_rem.get(r.id),
                        "verification_status": latest_ver.get(r.id),
                    }
                )
            return {"total": total, "items": items, "services": services}

    @staticmethod
    def _latest(
        session: Session, model: Any, ids: list[uuid.UUID], order: Any
    ) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        latest: dict[uuid.UUID, str] = {}
        for row in session.execute(
            select(model.incident_id, model.status)
            .where(model.incident_id.in_(ids))
            .order_by(order)
        ):
            latest[row[0]] = row[1]  # later rows overwrite: the last one wins
        return latest

    @staticmethod
    def _alert_stats(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
        stats: dict[uuid.UUID, dict[str, Any]] = {}
        if not ids:
            return stats
        for alert in session.execute(
            select(AlertRow).where(AlertRow.incident_id.in_(ids))
        ).scalars():
            if alert.incident_id is None:
                continue
            entry = stats.setdefault(
                alert.incident_id, {"count": 0, "firing": 0, "services": set()}
            )
            entry["count"] += 1
            entry["firing"] += 1 if alert.status == "firing" else 0
            if alert.labels.get("service"):
                entry["services"].add(alert.labels["service"])
        for entry in stats.values():
            entry["services"] = sorted(entry["services"])
        return stats

    @staticmethod
    def _final_times(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
        """When each incident reached its current (final) status, from its
        status-change events."""
        found: dict[uuid.UUID, datetime] = {}
        if not ids:
            return found
        for row in session.execute(
            select(OutboxEventRow.aggregate_id, OutboxEventRow.occurred_at, OutboxEventRow.payload)
            .where(
                OutboxEventRow.event_type == EVENT_TYPE_INCIDENT_STATUS_CHANGED,
                OutboxEventRow.aggregate_id.in_(ids),
            )
            .order_by(OutboxEventRow.sequence)
        ):
            if row[2].get("to_status") in FINAL_STATUSES:
                found[row[0]] = row[1]
        return found

    # --- detail --------------------------------------------------------------------

    def incident_detail(self, incident_id: uuid.UUID) -> dict[str, Any] | None:
        with self._session_factory() as session:
            incident = session.get(IncidentRow, incident_id)
            if incident is None:
                return None
            alerts = list(
                session.execute(
                    select(AlertRow)
                    .where(AlertRow.incident_id == incident_id)
                    .order_by(AlertRow.received_at)
                ).scalars()
            )
            investigation_ids = list(
                session.execute(
                    select(InvestigationRow.id)
                    .where(InvestigationRow.incident_id == incident_id)
                    .order_by(InvestigationRow.attempt_number)
                ).scalars()
            )
            refs = list(
                session.execute(
                    select(EvidenceRefRow)
                    .where(EvidenceRefRow.incident_id == incident_id)
                    .order_by(EvidenceRefRow.collected_at)
                ).scalars()
            )
            transitions = [
                {
                    "at": _iso(e.occurred_at),
                    "from": e.payload.get("from_status"),
                    "to": e.payload.get("to_status"),
                    "reason": e.payload.get("reason"),
                }
                for e in session.execute(
                    select(OutboxEventRow)
                    .where(
                        OutboxEventRow.aggregate_id == incident_id,
                        OutboxEventRow.event_type == EVENT_TYPE_INCIDENT_STATUS_CHANGED,
                    )
                    .order_by(OutboxEventRow.sequence)
                ).scalars()
            ]
        investigations = [self._investigation(i) for i in investigation_ids]
        remediations = [
            {
                "remediation": r.model_dump(mode="json"),
                "policy_decisions": self._remediations.policy_decisions(r.id),
                "executions": self._remediations.executions(r.id),
                "timeline": self._remediations.timeline(r.id),
                "baseline": self._remediations.baseline(r.id),
            }
            for r in self._remediations.list_for_incident(incident_id)
        ]
        verifications = [
            {
                "verification": v.model_dump(mode="json"),
                "observations": self._verifications.observations(v.id),
                "evidence": self._verifications.evidence_links(v.id),
            }
            for v in self._verifications.for_incident(incident_id)
        ]
        now = self._clock()
        final_at = (
            next(
                (t["at"] for t in reversed(transitions) if t["to"] == incident.status),
                None,
            )
            if incident.status in FINAL_STATUSES
            else None
        )
        end = datetime.fromisoformat(final_at) if final_at else now
        detail = {
            "incident": {
                "id": str(incident.id),
                "status": incident.status,
                "severity": incident.severity,
                "service": incident.service,
                "environment": incident.environment,
                "correlation_key": incident.correlation_key,
                "attempt_count": incident.attempt_count,
                "version": incident.version,
                "created_at": _iso(incident.created_at),
                "updated_at": _iso(incident.updated_at),
                "ended_at": final_at,
                "duration_seconds": round((end - incident.created_at).total_seconds(), 1),
            },
            "alerts": [
                {
                    "id": str(a.id),
                    "alertname": a.labels.get("alertname"),
                    "service": a.labels.get("service"),
                    "severity": a.severity,
                    "status": a.status,
                    "source": a.source,
                    "summary": a.annotations.get("summary"),
                    "received_at": _iso(a.received_at),
                    "resolved_at": _iso(a.resolved_at),
                }
                for a in alerts
            ],
            "transitions": transitions,
            "investigations": investigations,
            "remediations": remediations,
            "verifications": verifications,
            "evidence": [
                {
                    "id": str(r.id),
                    "evidence_type": r.evidence_type,
                    "source_system": r.source_system,
                    "investigation_id": str(r.investigation_id) if r.investigation_id else None,
                    "content_hash": r.content_hash,
                    "collected_at": _iso(r.collected_at),
                }
                for r in refs
            ],
        }
        detail["timeline"] = self._timeline(detail)
        return detail

    def _investigation(self, investigation_id: uuid.UUID) -> dict[str, Any]:
        trace = self._investigations.get_trace(investigation_id)
        steps = trace["steps"]
        tool_calls = [
            {
                "sequence": s["sequence"],
                "iteration": s["iteration"],
                "at": s["created_at"],
                "tool": s["payload"].get("tool"),
                "arguments": s["payload"].get("arguments", {}),
                "ok": s["payload"].get("ok"),
                "error_code": s["payload"].get("error_code"),
                "summary": s["payload"].get("summary"),
                "evidence_ids": s["payload"].get("evidence_ids", []),
            }
            for s in steps
            if s["kind"] == "tool_call"
        ]
        return {
            "investigation": trace["investigation"],
            "hypotheses": trace["hypotheses"],
            "tool_calls": tool_calls,
            "hypothesis_updates": [
                {"at": s["created_at"], "iteration": s["iteration"], **s["payload"]}
                for s in steps
                if s["kind"] == "hypothesis_update"
            ],
            "conclusion_rejections": [
                {"at": s["created_at"], "unmet": s["payload"].get("unmet_criteria", [])}
                for s in steps
                if s["kind"] == "conclusion_rejected"
            ],
            "rca": trace["rca_report"],
        }

    @staticmethod
    def _timeline(detail: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        def add(at: str | None, kind: str, title: str, **extra: Any) -> None:
            if at:
                events.append({"at": at, "kind": kind, "title": title, **extra})

        for a in detail["alerts"]:
            add(a["received_at"], "alert", f"{a['alertname']} fired on {a['service']}", ref=a["id"])
            add(a["resolved_at"], "alert", f"{a['alertname']} resolved", ref=a["id"])
        for t in detail["transitions"]:
            add(t["at"], "state", f"{t['from']} → {t['to']}", detail=t["reason"])
        for inv in detail["investigations"]:
            i = inv["investigation"]
            add(
                i["started_at"],
                "investigation",
                f"Investigation attempt {i['attempt_number']} started",
                detail=i["model_name"],
            )
            for call in inv["tool_calls"]:
                if call["ok"] and call["evidence_ids"]:
                    add(
                        call["at"],
                        "evidence",
                        f"{call['tool']}: {call['summary'] or ''}"[:160],
                        evidence_ids=call["evidence_ids"],
                    )
            for update in inv["hypothesis_updates"]:
                for applied in update.get("applied", []):
                    add(
                        update["at"],
                        "hypothesis",
                        f"{applied['key']}: {applied.get('from_status') or 'new'} "
                        f"→ {applied['to_status']}",
                    )
            if inv["rca"]:
                root = inv["rca"]["report"].get("root_cause_hypothesis", {})
                add(
                    i["completed_at"],
                    "rca",
                    f"RCA: {root.get('cause_category')} in {root.get('component')}",
                    ref=inv["rca"]["id"],
                )
            elif i["completed_at"]:
                add(
                    i["completed_at"],
                    "investigation",
                    f"Investigation ended {i['status']}",
                    detail=i.get("escalation_reason") or i.get("failure_reason"),
                )
        for rem in detail["remediations"]:
            for entry in rem["timeline"]:
                add(
                    entry["occurred_at"],
                    "remediation",
                    f"{entry['action_id']}: {entry['event'].replace('_', ' ')}",
                    detail=entry["actor"],
                    ref=rem["remediation"]["id"],
                )
        for ver in detail["verifications"]:
            v = ver["verification"]
            for o in ver["observations"]:
                add(
                    o["observed_at"],
                    "verification",
                    f"Observation {o['sequence']}: " + _verdict(o),
                    evidence_ids=o["evidence_ids"],
                )
            add(
                v["completed_at"],
                "verification",
                f"Verification {v['status']}",
                detail=v["failure_reason"],
                ref=v["id"],
            )
        events.sort(key=lambda e: e["at"])
        return events

    # --- overview -------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        with self._session_factory() as session:
            by_status = dict(
                session.execute(
                    select(IncidentRow.status, func.count()).group_by(IncidentRow.status)
                ).all()
            )
            investigations = session.execute(
                select(func.count())
                .select_from(InvestigationRow)
                .where(InvestigationRow.status.in_(["CREATED", "INVESTIGATING"]))
            ).scalar_one()
            remediation_counts = dict(
                session.execute(
                    select(RemediationRow.status, func.count()).group_by(RemediationRow.status)
                ).all()
            )
            verification_counts = dict(
                session.execute(
                    select(VerificationRow.status, func.count()).group_by(VerificationRow.status)
                ).all()
            )
            escalations = [
                {
                    "incident_id": str(e.aggregate_id),
                    "at": _iso(e.occurred_at),
                    "reason": e.payload.get("reason"),
                }
                for e in session.execute(
                    select(OutboxEventRow)
                    .where(OutboxEventRow.event_type == EVENT_TYPE_INCIDENT_STATUS_CHANGED)
                    .where(text("payload->>'to_status' = 'ESCALATED'"))
                    .order_by(OutboxEventRow.sequence.desc())
                    .limit(10)
                ).scalars()
            ]
            failed_events = session.execute(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.published_at.is_(None), OutboxEventRow.publish_attempts > 0)
            ).scalar_one()
            unpublished = session.execute(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.published_at.is_(None))
            ).scalar_one()
        return {
            "incidents_by_status": by_status,
            "active_incidents": sum(v for k, v in by_status.items() if k in ACTIVE_STATUSES),
            "investigations_running": investigations,
            "awaiting_approval": remediation_counts.get("AWAITING_APPROVAL", 0),
            "remediations_executing": remediation_counts.get("EXECUTING", 0)
            + remediation_counts.get("APPROVED", 0),
            "verifications_running": verification_counts.get("RUNNING", 0)
            + verification_counts.get("PENDING", 0),
            "recent_escalations": escalations,
            "outbox_failed_events": failed_events,
            "outbox_unpublished": unpublished,
        }

    # --- metrics ------------------------------------------------------------------

    _DURATIONS = {
        "time_to_incident_creation": """
            SELECT EXTRACT(EPOCH FROM (i.created_at - a.first_received))
            FROM incident_core.incidents i
            JOIN (SELECT incident_id, min(received_at) AS first_received
                  FROM incident_core.alerts GROUP BY incident_id) a ON a.incident_id = i.id""",
        "time_to_investigation_start": """
            SELECT EXTRACT(EPOCH FROM (min(v.started_at) - i.created_at))
            FROM incident_core.incidents i
            JOIN incident_core.investigations v ON v.incident_id = i.id
            WHERE v.started_at IS NOT NULL GROUP BY i.id, i.created_at""",
        "investigation_duration": """
            SELECT EXTRACT(EPOCH FROM (completed_at - started_at)) FROM incident_core.investigations
            WHERE completed_at IS NOT NULL AND started_at IS NOT NULL""",
        "time_to_rca": """
            SELECT EXTRACT(EPOCH FROM (min(r.generated_at) - i.created_at))
            FROM incident_core.incidents i JOIN incident_core.rca_reports r ON r.incident_id = i.id
            GROUP BY i.id, i.created_at""",
        "approval_latency": """
            SELECT EXTRACT(EPOCH FROM (a.decided_at - t.occurred_at))
            FROM incident_core.remediation_approvals a
            JOIN incident_core.remediation_timeline t
              ON t.remediation_id = a.remediation_id AND t.event = 'approval_requested'
            WHERE a.decision IN ('approved', 'rejected')""",
        "remediation_execution_latency": """
            SELECT EXTRACT(EPOCH FROM (completed_at - started_at))
            FROM incident_core.remediation_executions
            WHERE status = 'SUCCEEDED' AND completed_at IS NOT NULL""",
        "verification_latency": """
            SELECT EXTRACT(EPOCH FROM (completed_at - started_at)) FROM incident_core.verifications
            WHERE completed_at IS NOT NULL AND started_at IS NOT NULL""",
        "incident_duration": """
            SELECT EXTRACT(EPOCH FROM (max(e.occurred_at) - i.created_at))
            FROM incident_core.incidents i
            JOIN incident_core.outbox_events e ON e.aggregate_id = i.id
            WHERE e.event_type = 'IncidentStatusChanged'
              AND e.payload->>'to_status' IN ('RESOLVED', 'CANCELLED', 'ESCALATED', 'CLOSED')
              AND i.status IN ('RESOLVED', 'CANCELLED', 'ESCALATED', 'CLOSED')
            GROUP BY i.id, i.created_at""",
    }

    def metrics(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with self._session_factory() as session:
            for name, query in self._DURATIONS.items():
                values = sorted(
                    float(v)  # type: ignore[arg-type]
                    for (v,) in session.execute(text(query))
                    if v is not None
                )
                out[name] = _stats(values)
            completed: dict[str, int] = dict(
                session.execute(
                    text(
                        "SELECT status, count(*) FROM incident_core.verifications "
                        "WHERE status IN ('PASSED', 'FAILED', 'TIMED_OUT') GROUP BY status"
                    )
                ).all()
            )
            finished: dict[str, int] = dict(
                session.execute(
                    text(
                        "SELECT status, count(*) FROM incident_core.incidents "
                        "WHERE status IN ('RESOLVED', 'CANCELLED', 'ESCALATED', 'CLOSED') "
                        "GROUP BY status"
                    )
                ).all()
            )
        verified = sum(completed.values())
        done = sum(finished.values())
        out["successful_remediation_rate"] = _rate(completed.get("PASSED", 0), verified)
        out["verification_failure_rate"] = _rate(
            completed.get("FAILED", 0) + completed.get("TIMED_OUT", 0), verified
        )
        out["escalation_rate"] = _rate(finished.get("ESCALATED", 0), done)
        return out


def _verdict(observation: dict[str, Any]) -> str:
    if observation["passed"]:
        return "pass"
    return "fail" if observation["conclusive"] else "inconclusive"


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "unit": "seconds", "p50": None, "p90": None, "mean": None, "max": None}

    def pct(p: float) -> float:
        index = min(len(values) - 1, max(0, round(p * (len(values) - 1))))
        return round(values[index], 3)

    return {
        "count": len(values),
        "unit": "seconds",
        "p50": pct(0.5),
        "p90": pct(0.9),
        "mean": round(sum(values) / len(values), 3),
        "max": round(values[-1], 3),
    }


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "count": denominator,
        "numerator": numerator,
        "rate": round(numerator / denominator, 3) if denominator else None,
    }
