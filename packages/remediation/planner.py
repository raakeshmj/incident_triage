"""`RemediationPlanner`: deterministic RCA -> catalog proposal.

Reads the accepted RCA (selected cause category + component) and the
evidence records it cites, and maps them to at most one catalog action with
concrete parameters taken from that evidence -- never invented:

    deployment       -> rollback_deployment (running version -> previous version,
                        from the cited deployment record)
    configuration    -> revert_configuration (or disable_feature_flag for a
                        `feature.*` key), from the cited config-change record
    resource_memory  -> restart_service
    resource_cpu,
    traffic          -> scale_service (+2)
    anything else    -> no proposal (dependency/database/infrastructure causes
                        are not fixed by acting on the incident's own service)

The output is only a proposal; policy and a human decide what happens.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from packages.domain.remediation import RemediationProposal


class EvidenceReader(Protocol):
    def get_incident_evidence(self, incident_id: uuid.UUID) -> list[Any]: ...


class RemediationPlanner:
    def __init__(self, evidence: EvidenceReader) -> None:
        self._evidence = evidence

    def plan(
        self, incident_id: uuid.UUID, investigation_id: uuid.UUID, rca_report: dict[str, Any]
    ) -> tuple[RemediationProposal | None, str]:
        """(proposal, why). `why` explains a missing proposal too."""
        root = rca_report.get("root_cause_hypothesis") or {}
        category, component = root.get("cause_category"), root.get("component")
        if not category or not component:
            return None, "the RCA has no structured root cause"
        supporting = set(rca_report.get("supporting_evidence") or [])
        records = [
            r
            for r in self._evidence.get_incident_evidence(incident_id)
            if str(r.evidence_id) in supporting and r.subject_service == component
        ]
        root_text = (rca_report.get("root_cause") or {}).get("text", "")
        reason = f"RCA of investigation {investigation_id}: {category} in {component}. {root_text}"

        def proposal(action: str, params: dict[str, Any], effect: str) -> RemediationProposal:
            return RemediationProposal(
                incident_id=incident_id,
                investigation_id=investigation_id,
                action_id=action,
                parameters={"service": component, **params},
                reason=reason[:1000],
                expected_effect=effect,
                source="planner",
                proposed_by="remediation-planner",
            )

        if category == "deployment":
            deployment = next((r for r in records if r.evidence_type.value == "deployment"), None)
            current = (deployment.normalized_payload.get("current") or {}) if deployment else {}
            running, previous = current.get("version"), current.get("previous_version")
            if not running or not previous:
                return None, "no cited deployment record names the running and previous versions"
            return (
                proposal(
                    "rollback_deployment",
                    {"from_version": running, "to_version": previous},
                    f"{component} returns to {previous}; its error rate returns below 5%",
                ),
                "rollback to the previously deployed version",
            )
        if category == "configuration":
            config = next((r for r in records if r.evidence_type.value == "configuration"), None)
            changes = (config.normalized_payload.get("changes_in_window") or []) if config else []
            if not changes:
                return None, "no cited configuration record contains the change"
            change = changes[0]  # newest first
            if str(change["key"]).startswith("feature."):
                return (
                    proposal(
                        "disable_feature_flag",
                        {"flag": change["key"]},
                        f"{change['key']} off; {component} error rate returns below 5%",
                    ),
                    "disable the feature flag that changed",
                )
            if change.get("old_value") is None:
                return None, "the configuration change has no previous value to revert to"
            return (
                proposal(
                    "revert_configuration",
                    {
                        "key": change["key"],
                        "from_value": change["new_value"],
                        "to_value": change["old_value"],
                    },
                    f"{change['key']} back to {change['old_value']!r}; errors return below 5%",
                ),
                "revert the configuration change",
            )
        if category == "resource_memory":
            return (
                proposal("restart_service", {}, f"{component} memory back to baseline"),
                "restart to release leaked memory",
            )
        if category in ("resource_cpu", "traffic"):
            return (
                proposal("scale_service", {"increase_by": 2}, f"{component} p95 latency below 1s"),
                "add capacity",
            )
        return None, f"no catalog action remediates a {category} cause on {component}"
