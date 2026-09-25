"""The initial incident context: compact, scoped, and built by the
application -- never by the model.

Only what bears on this incident: its metadata, its alerts and their
timeline, its service's dependency neighborhood, evidence already recorded
for it, the tools, and the budget. No raw telemetry: the model pulls
evidence through tools. Alert annotations are untrusted text and are
sanitized and bounded (docs/architecture/13-security-boundaries.md).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from packages.domain.investigation import InvestigationBudget
from packages.domain.views import EvidenceRefView, IncidentView
from packages.evidence.sanitize import clean_text
from packages.evidence.scope import ServiceCatalog


def build_context(
    incident: IncidentView,
    evidence_refs: list[EvidenceRefView],
    catalog: ServiceCatalog,
    budget: InvestigationBudget,
    now: datetime,
) -> dict[str, Any]:
    alerts = sorted(incident.alerts, key=lambda a: (a.received_at, str(a.id)))
    timeline: list[dict[str, Any]] = []
    for alert in alerts:
        name = alert.labels.get("alertname", "alert")
        timeline.append({"at": alert.received_at.isoformat(), "event": f"{name} fired"})
        if alert.resolved_at:
            timeline.append({"at": alert.resolved_at.isoformat(), "event": f"{name} resolved"})
    timeline.sort(key=lambda t: t["at"])

    entry = catalog.services.get(incident.service)
    dependencies = list(entry.dependencies) if entry else []
    dependents = sorted(
        name for name, entry in catalog.services.items() if incident.service in entry.dependencies
    )
    return {
        "incident": {
            "service": incident.service,
            "environment": incident.environment,
            "regions": sorted({a.labels["region"] for a in alerts if "region" in a.labels}),
            "severity": incident.severity,
            "status": incident.status,
            "opened_at": incident.created_at.isoformat(),
            "now": now.isoformat(),
        },
        "alerts": [
            {
                "alertname": a.labels.get("alertname"),
                "severity": a.severity,
                "status": a.status,
                "alert_type": a.labels.get("alert_type"),
                "fired_at": a.received_at.isoformat(),
                "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
                "summary": clean_text(a.annotations.get("summary", ""), 300),
                "description": clean_text(a.annotations.get("description", ""), 500),
            }
            for a in alerts
        ],
        "alert_timeline": timeline,
        "service_topology": {
            "service": incident.service,
            "calls": dependencies,
            "called_by": dependents,
        },
        "existing_evidence": [
            {
                "evidence_id": str(r.id),
                "evidence_type": r.evidence_type,
                "source": r.source_system,
                "collected_at": r.collected_at.isoformat(),
            }
            for r in evidence_refs
        ],
        "budget": {
            "max_turns": budget.max_iterations,
            "max_evidence_tool_calls": budget.max_tool_calls,
            "max_evidence_items": budget.max_evidence_items,
        },
    }


def render_context(context: dict[str, Any]) -> str:
    return "Investigate this incident. Incident context (JSON):\n" + json.dumps(
        context, indent=1, sort_keys=True
    )
