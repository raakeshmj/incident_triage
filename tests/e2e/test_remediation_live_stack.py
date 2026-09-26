"""Phase 7 on the real stack (`make infra-up-full`):

a real bad-deployment chaos incident -> Prometheus -> Alertmanager -> API
-> incident -> investigation through the real evidence service (scripted
model) -> RCA_READY -> planner proposes a rollback -> policy requires
approval -> a simulated human approves -> the runner rolls back through the
simulator control plane -> the bad build stops running and the rollback is
on record -> everything persisted.

No model API is called.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import httpx
import pytest
import redis
from sqlalchemy import select

from apps.worker.investigation_main import build_runtime as build_investigation_runtime
from apps.worker.remediation_main import build_runtime as build_remediation_runtime
from packages.agents.config import InvestigationSettings
from packages.agents.fake import FakeInvestigationModel
from packages.domain.investigation import InvestigationStatus
from packages.domain.remediation import RemediationStatus
from packages.incident.db.models import IncidentRow, RemediationRow
from simulator.changes.registry import ChangeRegistry
from simulator.chaos import cli as chaos
from tests import investigation_support as support

pytestmark = pytest.mark.stack
SERVICE = "checkout-service"


@pytest.fixture
def live_stack(stack_urls):
    for url in ("http://localhost:9093/-/ready", "http://localhost:8001/health"):
        try:
            httpx.get(url, timeout=3).raise_for_status()
        except httpx.HTTPError:
            pytest.skip(f"{url} not reachable; run `make infra-up-full`")
    return stack_urls


def test_live_bad_deployment_is_rolled_back_after_approval(
    live_stack, quiet_checkout, api_server, session_factory
):
    live_redis = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    started = datetime.now(UTC)
    chaos.main(["stop", "--service", SERVICE])
    chaos.main(
        [
            "start",
            "bad-deployment",
            "--service",
            SERVICE,
            "--duration",
            "300",
            "--params",
            '{"error_rate": 0.6}',
        ]
    )
    try:
        deadline = time.monotonic() + 180
        incident = None
        while time.monotonic() < deadline and incident is None:
            with session_factory() as session:
                incident = session.execute(
                    select(IncidentRow).where(
                        IncidentRow.service == SERVICE, IncidentRow.created_at >= started
                    )
                ).scalar_one_or_none()
            if incident is None:
                time.sleep(3)
        assert incident is not None, "no incident from the real alert within 180s"

        investigation = build_investigation_runtime(
            database_url=os.environ["INCIDENT_CORE_DATABASE_URL"],
            settings=InvestigationSettings(_env_file=None),  # type: ignore[call-arg]
            model_factory=lambda spec: FakeInvestigationModel(support.happy_script()),
            owner="live-e2e",
        )
        investigation_id = investigation.start(incident.id)
        assert investigation.engine.run(investigation_id) == InvestigationStatus.COMPLETED

        remediation = build_remediation_runtime(
            database_url=os.environ["INCIDENT_CORE_DATABASE_URL"], owner="live-e2e-runner"
        )
        remediation_id = remediation.plan(investigation_id)
        assert remediation_id is not None
        proposed = remediation.remediations.get(remediation_id)
        assert proposed.status == RemediationStatus.AWAITING_APPROVAL
        assert proposed.action_id == "rollback_deployment"
        assert (
            proposed.parameters["from_version"] == "1.1.0-bad"
        )  # from the real registry, via evidence

        remediation.remediations.decide_approval(  # the simulated human
            remediation_id,
            approver="alice",
            approver_roles=["service_owner"],
            approve=True,
            proposal_hash=proposed.proposal_hash,
            policy_decision_id=proposed.policy_decision_id,  # type: ignore[arg-type]
        )
        remediation.execute(remediation_id)

        assert live_redis.get(f"chaos:{SERVICE}") is None  # the bad build no longer runs
        current = ChangeRegistry(live_redis).current_deployment(SERVICE)
        assert (
            current["change_type"] == "rollback"
            and current["version"] == proposed.parameters["to_version"]
        )
        with session_factory() as session:  # persisted, not just in memory
            row = session.get(RemediationRow, remediation_id)
            assert row.status == RemediationStatus.EXECUTED.value
            assert (
                row.execution_attempts == 1
                and row.executor_result["to_version"] == current["version"]
            )
            assert session.get(IncidentRow, incident.id).status == "VERIFYING"
        events = [t["event"] for t in remediation.remediations.timeline(remediation_id)]
        assert events[-3:] == ["execution_started", "executed", "verification_requested"]
    finally:
        chaos.main(["stop", "--service", SERVICE])
