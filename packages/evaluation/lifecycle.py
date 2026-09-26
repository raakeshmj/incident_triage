"""The rest of the loop for evaluation runs (Phase 8): after the
investigation, the same planner, policy engine, approval binding, runner,
baseline capture, verification engine and incident-core commands as
production -- with one substitution: `ScenarioExecutor` applies the
catalog action to the scenario world instead of the simulator's Redis.

    RCA -> planner -> propose (policy) -> simulated human approval
        -> runner (baseline via evidence) -> ScenarioExecutor -> world changes
        -> verification engine (evidence only) -> incident verdict

Duplicate deliveries are replayed on purpose (proposal, approval-triggered
execution, verification start) so "no duplicate side effects" is graded
from what actually happened, not assumed. Nothing here touches real
infrastructure.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from packages.evaluation.recording import EvidenceStoreReader
from packages.evaluation.scenario import Scenario
from packages.evaluation.world import ScenarioWorld, _deployment
from packages.evidence.adapters._http import iso
from packages.evidence.adapters.changes import CONFIG_KEY, DEPLOYMENTS_KEY
from packages.evidence.service import EvidenceService
from packages.incident.investigations import InvestigationCoreService
from packages.incident.remediations import RemediationCoreService
from packages.incident.verifications import VerificationCoreService
from packages.remediation.catalog import CATALOG_VERSION, get_entry
from packages.remediation.executor import ExecutionRequest, ExecutorResult
from packages.remediation.planner import RemediationPlanner
from packages.remediation.runner import RemediationRunner
from packages.verification.engine import BaselineCollector, VerificationEngine
from packages.verification.observer import EvidenceObserver

EVAL_APPROVER = "eval-approver"


class ScenarioExecutor:
    """Catalog actions applied to a scenario world; idempotent by key, with
    every real side effect counted."""

    name = "scenario"

    def __init__(
        self, world: ScenarioWorld, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._world = world
        self._clock = clock
        self._results: dict[str, ExecutorResult] = {}
        self.side_effects: list[dict[str, Any]] = []

    def inspect(self, idempotency_key: str) -> ExecutorResult | None:
        return self._results.get(idempotency_key)

    def execute(self, request: ExecutionRequest) -> ExecutorResult:
        if request.idempotency_key in self._results:
            return self._results[request.idempotency_key]
        entry = get_entry(request.action_id)
        if entry is None or request.catalog_version != CATALOG_VERSION:
            return ExecutorResult(False, error="not a catalog action")
        params, problems = entry.validate(request.parameters)
        if params is None:
            return ExecutorResult(False, error="; ".join(problems))
        now = self._clock()
        service = params.service
        env = self._world.scenario.environment
        runtime = self._world.runtime.setdefault(service, {"replicas": 1, "flags": {}})
        detail: dict[str, Any] = {}
        if request.action_id == "rollback_deployment":
            records = self._world.records.setdefault(DEPLOYMENTS_KEY.format(service=service), [])
            running = records[-1]["version"] if records else None
            if running not in (params.from_version, params.to_version):  # type: ignore[attr-defined]
                return ExecutorResult(False, {"running": running}, error="target changed")
            records.append(_deployment(service, env, now, params.to_version, running, "rollback"))  # type: ignore[attr-defined]
            detail = {"to_version": params.to_version}  # type: ignore[attr-defined]
        elif request.action_id == "revert_configuration":
            records = self._world.records.setdefault(CONFIG_KEY.format(service=service), [])
            records.append(
                {
                    "change_id": str(uuid.uuid4()),
                    "service": service,
                    "environment": env,
                    "key": params.key,  # type: ignore[attr-defined]
                    "old_value": params.from_value,  # type: ignore[attr-defined]
                    "new_value": params.to_value,  # type: ignore[attr-defined]
                    "changed_at": iso(now),
                    "changed_by": "remediation-executor",
                }
            )
            detail = {"key": params.key}  # type: ignore[attr-defined]
        elif request.action_id == "disable_feature_flag":
            runtime["flags"][params.flag] = "false"  # type: ignore[attr-defined]
        elif request.action_id == "scale_service":
            runtime["replicas"] = int(runtime["replicas"]) + params.increase_by  # type: ignore[attr-defined]
            detail = {"replicas": runtime["replicas"]}
        self._world.remediated_at = now
        self.side_effects.append({"action": request.action_id, "key": request.idempotency_key})
        result = ExecutorResult(True, detail)
        self._results[request.idempotency_key] = result
        return result


def run_lifecycle(
    scenario: Scenario,
    *,
    world: ScenarioWorld,
    evidence: EvidenceService,
    evidence_reader: EvidenceStoreReader,
    investigations: InvestigationCoreService,
    remediations: RemediationCoreService,
    verifications: VerificationCoreService,
    investigation_id: uuid.UUID,
    owner: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    lifecycle = scenario.lifecycle
    summary: dict[str, Any] = {"proposal": None, "why": None, "executor_side_effects": 0}
    trace = investigations.get_trace(investigation_id)
    if not trace.get("rca_report") or lifecycle is None:
        summary["why"] = (
            "no accepted RCA" if not trace.get("rca_report") else "no lifecycle expectations"
        )
        return summary
    incident_id = uuid.UUID(trace["investigation"]["incident_id"])
    proposal, why = RemediationPlanner(evidence_reader).plan(
        incident_id, investigation_id, trace["rca_report"]["report"]
    )
    summary["why"] = why
    if proposal is None:
        return summary
    key = f"planner:{investigation_id}"
    view = remediations.propose(proposal, idempotency_key=key)
    remediations.propose(proposal, idempotency_key=key)  # redelivered InvestigationCompleted
    summary["proposal"] = str(view.id)
    if view.policy_decision != "REQUIRE_APPROVAL" or not lifecycle.approve:
        return summary
    remediations.decide_approval(
        view.id,
        approver=EVAL_APPROVER,
        approver_roles=["service_owner", "on_call_engineer"],
        approve=True,
        proposal_hash=view.proposal_hash,
        policy_decision_id=view.policy_decision_id,  # type: ignore[arg-type]
    )
    executor = ScenarioExecutor(world)
    observer = EvidenceObserver(evidence)
    runner = RemediationRunner(
        remediations, executor, owner=owner, baseline=BaselineCollector(remediations, observer)
    )
    done = runner.run(view.id)
    runner.run(view.id)  # redelivered RemediationApproved
    summary["executor_side_effects"] = len(executor.side_effects)
    if done.verification_ref is None:
        return summary
    engine = VerificationEngine(verifications, observer, owner=owner)
    engine.start(done.verification_ref)
    engine.start(done.verification_ref)  # redelivered VerificationRequested
    engine.run_until_done(done.verification_ref, sleep=sleep, poll_seconds=0.02)
    return summary
