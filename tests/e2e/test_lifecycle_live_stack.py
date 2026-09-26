"""Phase 8 on the real stack (`make infra-up-full`): real chaos, real
Prometheus alerting through Alertmanager into the API, the real evidence
service against live telemetry, the real simulator control plane.

Scenario 1: bad deployment -> incident -> investigation -> RCA_READY ->
    rollback proposal -> policy -> approval -> rollback -> verification
    (live metrics) -> RESOLVED.
Scenario 2: the rollback executes but the system does not recover (a new
    fault) -> verification FAILED -> never RESOLVED -> re-investigation.
Scenario 3: the alert clears on its own while TRIAGING -> CANCELLED,
    never RESOLVED, nothing investigated or executed.

The investigation uses the scripted model (no model API); verification
windows run at a fraction of production length (VERIFICATION_TIME_SCALE).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import httpx
import pytest
import redis
from sqlalchemy import select, text

from apps.worker.investigation_main import build_runtime as build_investigation_runtime
from apps.worker.remediation_main import RemediationSettings
from apps.worker.remediation_main import build_runtime as build_remediation_runtime
from apps.worker.verification_main import VerificationSettings
from apps.worker.verification_main import build_engine as build_verification_engine
from packages.agents.config import InvestigationSettings
from packages.agents.fake import FakeInvestigationModel
from packages.domain.investigation import InvestigationStatus
from packages.domain.remediation import RemediationStatus
from packages.domain.verification import VerificationStatus
from packages.incident.db.models import IncidentRow
from simulator.chaos import cli as chaos
from tests import investigation_support as support
from tests.e2e.conftest import API_PORT

pytestmark = pytest.mark.stack
SERVICE = "checkout-service"
DB = os.environ["INCIDENT_CORE_DATABASE_URL"]


@pytest.fixture
def live_stack(stack_urls):
    for url in ("http://localhost:9093/-/ready", "http://localhost:8001/health"):
        try:
            httpx.get(url, timeout=3).raise_for_status()
        except httpx.HTTPError:
            pytest.skip(f"{url} not reachable; run `make infra-up-full`")
    return stack_urls


def _incident_from_real_alert(session_factory, started, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with session_factory() as session:
            incident = session.execute(
                select(IncidentRow).where(
                    IncidentRow.service == SERVICE, IncidentRow.created_at >= started
                )
            ).scalar_one_or_none()
        if incident is not None:
            return incident
        time.sleep(3)
    raise AssertionError(f"no incident from the real alert within {timeout}s")


def _status(session_factory, incident_id) -> str:
    with session_factory() as session:
        return session.get(IncidentRow, incident_id).status


def _to_rollback(session_factory, time_scale):
    """Real alert -> RCA_READY -> planned rollback -> approved -> executed."""
    started = datetime.now(UTC)
    chaos.main(["stop", "--service", SERVICE])
    chaos.main(
        [
            "start",
            "bad-deployment",
            "--service",
            SERVICE,
            "--duration",
            "900",
            "--params",
            '{"error_rate": 0.6}',
        ]
    )
    incident = _incident_from_real_alert(session_factory, started)
    investigation = build_investigation_runtime(
        database_url=DB,
        settings=InvestigationSettings(_env_file=None),  # type: ignore[call-arg]
        model_factory=lambda spec: FakeInvestigationModel(support.happy_script()),
        owner="live-lifecycle",
    )
    investigation_id = investigation.start(incident.id)
    assert investigation.engine.run(investigation_id) == InvestigationStatus.COMPLETED
    remediation = build_remediation_runtime(
        database_url=DB,
        owner="live-lifecycle-runner",
        settings=RemediationSettings(_env_file=None, verification_time_scale=time_scale),  # type: ignore[call-arg]
    )
    remediation_id = remediation.plan(investigation_id)
    proposal = remediation.remediations.get(remediation_id)  # type: ignore[arg-type]
    assert proposal.action_id == "rollback_deployment"
    remediation.remediations.decide_approval(
        proposal.id,
        approver="alice",
        approver_roles=["service_owner"],
        approve=True,
        proposal_hash=proposal.proposal_hash,
        policy_decision_id=proposal.policy_decision_id,  # type: ignore[arg-type]
    )
    remediation.execute(proposal.id)
    executed = remediation.remediations.get(proposal.id)
    assert executed.status == RemediationStatus.EXECUTED, executed.failure_reason
    return incident, investigation, executed


def test_scenario_1_bad_deployment_is_rolled_back_verified_and_resolved(
    live_stack, quiet_checkout, api_server, session_factory
):
    try:
        incident, _, executed = _to_rollback(session_factory, time_scale=0.5)
        live = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        assert live.get(f"chaos:{SERVICE}") is None  # the bad build is gone
        core, engine = build_verification_engine(
            database_url=DB,
            settings=VerificationSettings(_env_file=None),
            owner="live-verifier",  # type: ignore[call-arg]
        )
        verdict = engine.run_until_done(executed.verification_ref, poll_seconds=1.0)  # type: ignore[arg-type]
        assert verdict.status == VerificationStatus.PASSED, verdict.failure_reason
        assert verdict.baseline["deployment"]["version"] == executed.parameters["from_version"]  # type: ignore[index]
        observations = core.observations(verdict.id)
        assert observations[-1]["passed"] and all(o["evidence_ids"] for o in observations)
        assert _status(session_factory, incident.id) == "RESOLVED"
        detail = httpx.get(
            f"http://localhost:{API_PORT}/api/v1/incidents/{incident.id}/detail", timeout=10
        ).json()
        assert detail["incident"]["status"] == "RESOLVED"
        assert detail["verifications"][0]["verification"]["status"] == "PASSED"
        assert [t["to"] for t in detail["transitions"]][-4:] == [
            "AWAITING_APPROVAL",
            "REMEDIATION_IN_PROGRESS",
            "VERIFYING",
            "RESOLVED",
        ]
    finally:
        chaos.main(["stop", "--service", SERVICE])


def test_scenario_2_verification_failure_never_resolves_and_reinvestigates(
    live_stack, quiet_checkout, api_server, session_factory
):
    try:
        incident, investigation, executed = _to_rollback(session_factory, time_scale=0.3)
        # the rollback ran, but the service keeps failing for another reason
        chaos.main(
            [
                "start",
                "error-storm",
                "--service",
                SERVICE,
                "--duration",
                "600",
                "--params",
                '{"error_rate": 0.6}',
            ]
        )
        _, engine = build_verification_engine(
            database_url=DB,
            settings=VerificationSettings(_env_file=None),
            owner="live-verifier",  # type: ignore[call-arg]
        )
        verdict = engine.run_until_done(executed.verification_ref, poll_seconds=1.0)  # type: ignore[arg-type]
        assert verdict.status == VerificationStatus.FAILED
        assert verdict.next_action == "reinvestigate"
        assert _status(session_factory, incident.id) == "VERIFICATION_FAILED"
        [again] = [
            i
            for i in investigation.investigations.due_for_investigation(debounce_seconds=0)
            if i == incident.id
        ]
        investigation.start(again)
        assert _status(session_factory, incident.id) == "INVESTIGATING"
        with session_factory() as session:
            transitions = [
                r[0]
                for r in session.execute(
                    text(
                        "SELECT payload->>'to_status' FROM incident_core.outbox_events "
                        "WHERE aggregate_id = :id AND event_type = 'IncidentStatusChanged' "
                        "ORDER BY sequence"
                    ),
                    {"id": incident.id},
                )
            ]
        assert "RESOLVED" not in transitions
    finally:
        chaos.main(["stop", "--service", SERVICE])


def test_scenario_3_natural_recovery_is_cancelled_never_resolved(
    live_stack, quiet_checkout, api_server, session_factory
):
    started = datetime.now(UTC)
    chaos.main(["stop", "--service", SERVICE])
    chaos.main(
        [
            "start",
            "bad-deployment",
            "--service",
            SERVICE,
            "--duration",
            "600",
            "--params",
            '{"error_rate": 0.6}',
        ]
    )
    try:
        incident = _incident_from_real_alert(session_factory, started)
        assert incident.status == "TRIAGING"
    finally:
        chaos.main(["stop", "--service", SERVICE])  # the problem goes away by itself
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and _status(session_factory, incident.id) == "TRIAGING":
        time.sleep(5)
    assert _status(session_factory, incident.id) == "CANCELLED"
    with session_factory() as session:
        counts = session.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM incident_core.investigations WHERE incident_id = :id), "
                "(SELECT count(*) FROM incident_core.remediations WHERE incident_id = :id), "
                "(SELECT count(*) FROM incident_core.verifications WHERE incident_id = :id)"
            ),
            {"id": incident.id},
        ).one()
    assert tuple(counts) == (0, 0, 0)
