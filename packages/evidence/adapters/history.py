"""Historical incident search: deterministic, structured, explainable.

Candidates come from incident-core's read API (past incidents opened before
this one), and each is scored on structured fields only -- same service,
same environment, region overlap, alert-type overlap (Jaccard), recency --
with the per-signal breakdown returned alongside the score, the same
explainability contract as the correlation engine (ADR-0015). No embeddings,
no vector store: whether semantic retrieval adds anything is a question for
the eval harness once there's data to answer it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from packages.domain.views import IncidentSummary
from packages.evidence import limits
from packages.evidence.adapters._http import iso
from packages.evidence.models import Observation
from packages.evidence.types import EvidenceType, SourceSystem

WEIGHTS = {
    "same_service": 0.40,
    "alert_type_overlap": 0.25,
    "same_environment": 0.15,
    "region_overlap": 0.10,
    "recency": 0.10,
}
RECENCY_HORIZON_DAYS = 30.0
MIN_SCORE = 0.2


def score_candidate(
    *,
    service: str,
    environment: str,
    regions: tuple[str, ...],
    alert_types: tuple[str, ...],
    reference_time: datetime,
    candidate: IncidentSummary,
) -> tuple[float, dict[str, float]]:
    types_a, types_b = set(alert_types), set(candidate.alert_types)
    union = types_a | types_b
    age_days = max(0.0, (reference_time - candidate.created_at).total_seconds() / 86_400)
    signals = {
        "same_service": 1.0 if candidate.service == service else 0.0,
        "alert_type_overlap": len(types_a & types_b) / len(union) if union else 0.0,
        "same_environment": 1.0 if candidate.environment == environment else 0.0,
        "region_overlap": 1.0 if set(regions) & set(candidate.regions) else 0.0,
        "recency": max(0.0, 1.0 - age_days / RECENCY_HORIZON_DAYS),
    }
    contributions = {name: round(WEIGHTS[name] * value, 4) for name, value in signals.items()}
    return round(sum(contributions.values()), 4), contributions


class IncidentHistoryAdapter:
    def __init__(self, list_summaries: Callable[..., list[IncidentSummary]]) -> None:
        self._list = list_summaries

    def search_similar(
        self,
        *,
        incident_id: Any,
        service: str,
        environment: str,
        regions: tuple[str, ...],
        alert_types: tuple[str, ...],
        reference_time: datetime,
        limit: int,
    ) -> Observation:
        pool = self._list(
            exclude_incident_id=incident_id,
            created_before=reference_time,
            limit=limits.HISTORY_CANDIDATE_POOL,
        )
        scored = []
        for candidate in pool:
            score, contributions = score_candidate(
                service=service,
                environment=environment,
                regions=regions,
                alert_types=alert_types,
                reference_time=reference_time,
                candidate=candidate,
            )
            if score >= MIN_SCORE:
                scored.append((score, candidate, contributions))
        scored.sort(key=lambda s: (-s[0], -s[1].created_at.timestamp(), str(s[1].id)))
        matches = [
            {
                "incident_id": str(c.id),
                "score": score,
                "signals": contributions,
                "service": c.service,
                "environment": c.environment,
                "regions": list(c.regions),
                "alert_types": list(c.alert_types),
                "status": c.status,
                "severity": c.severity,
                "created_at": iso(c.created_at),
                "closed_at": iso(c.closed_at) if c.closed_at else None,
                "duration_seconds": int((c.closed_at - c.created_at).total_seconds())
                if c.closed_at
                else None,
                # Unknown until RCA reports exist (Phase 5+); never guessed.
                "root_cause_category": c.root_cause_category,
            }
            for score, c, contributions in scored[:limit]
        ]
        normalized = {
            "query": {
                "service": service,
                "environment": environment,
                "regions": list(regions),
                "alert_types": list(alert_types),
                "before": iso(reference_time),
            },
            "weights": WEIGHTS,
            "min_score": MIN_SCORE,
            "candidates_considered": len(pool),
            "matches": matches,
        }
        top = f"; best {matches[0]['incident_id']} ({matches[0]['score']})" if matches else ""
        return Observation(
            evidence_type=EvidenceType.INCIDENT_HISTORY,
            source_system=SourceSystem.INCIDENT_CORE,
            operation="similar_incidents",
            subject_service=service,
            query_spec={
                "template": "similar_incidents",
                "params": {
                    "service": service,
                    "environment": environment,
                    "alert_types": list(alert_types),
                    "limit": limit,
                },
                "scoring": {"weights": WEIGHTS, "min_score": MIN_SCORE},
            },
            source_reference={
                "api": "incident-core.list_incident_summaries",
                "matched_incident_ids": [m["incident_id"] for m in matches],
            },
            raw_response={"candidates": [c.model_dump(mode="json") for c in pool]},
            raw_truncated=len(pool) >= limits.HISTORY_CANDIDATE_POOL,
            normalized_payload=normalized,
            summary=f"{len(matches)} similar past incident(s) of {len(pool)} considered{top}",
            result_count=len(matches),
            observed_at=reference_time,
        )
