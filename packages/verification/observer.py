"""`EvidenceObserver`: one observation of a service, through evidence-service.

Every value a verification (or a baseline) uses is an evidence record:
persisted, hashed, registered with incident-core, and linked to the
verification. Nothing is read from the executor's own report. A source
that fails is recorded as an error and makes the sample inconclusive --
never a pass.
"""

from __future__ import annotations

import uuid
from typing import Any

from packages.domain.verification import Sample
from packages.evidence.errors import EvidenceError
from packages.evidence.service import EvidenceService


class EvidenceObserver:
    def __init__(self, evidence: EvidenceService) -> None:
        self._evidence = evidence

    def observe(
        self,
        incident_id: uuid.UUID,
        *,
        service: str,
        needs: set[str],
        requested_by: str,
    ) -> tuple[Sample, list[uuid.UUID]]:
        values: dict[str, Any] = {}
        errors: list[str] = []
        evidence_ids: list[uuid.UUID] = []

        def read(name: str, call: Any) -> dict[str, Any] | None:
            try:
                item = call()
            except EvidenceError as exc:
                errors.append(f"{name}: {exc.code}")
                return None
            evidence_ids.append(item.evidence_id)
            return dict(item.data)

        if "health" in needs:
            data = read(
                "health",
                lambda: self._evidence.get_service_health(
                    incident_id, service=service, requested_by=requested_by
                ),
            )
            if data is not None:
                values["health"] = {
                    "status": data.get("status"),
                    "signals": {
                        k: (v or {}).get("value") for k, v in (data.get("signals") or {}).items()
                    },
                    "violations": data.get("violations", []),
                }
        if "deployment" in needs:
            data = read(
                "deployment",
                lambda: self._evidence.get_recent_deployments(
                    incident_id, service=service, requested_by=requested_by
                ),
            )
            if data is not None:
                current = data.get("current") or {}
                values["deployment"] = {
                    "version": current.get("version"),
                    "previous_version": current.get("previous_version"),
                    "deployed_at": current.get("deployed_at"),
                }
        if "config" in needs:
            data = read(
                "config",
                lambda: self._evidence.get_config_changes(
                    incident_id, service=service, requested_by=requested_by
                ),
            )
            if data is not None:
                values["config"] = {"effective": data.get("effective_config") or {}}
        if "runtime" in needs:
            data = read(
                "runtime",
                lambda: self._evidence.get_runtime_state(
                    incident_id, service=service, requested_by=requested_by
                ),
            )
            if data is not None:
                values["runtime"] = {
                    "replicas": data.get("replicas"),
                    "flags": data.get("flags") or {},
                }
        return Sample(**values, errors=errors), evidence_ids
