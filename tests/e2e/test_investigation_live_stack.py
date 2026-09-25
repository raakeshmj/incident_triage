"""Phase 5 on the real stack (`make infra-up-full`), model scripted:

a real bad-deployment chaos incident -> Prometheus -> Alertmanager -> API
-> incident -> investigation -> tool calls through the *actual* evidence
service against live Prometheus / the deployment registry / Git ->
evidence-backed RCA -> RCA_READY.

The scripted model reads the real tool results and cites only the evidence
ids that came back; it's told nothing else. No model API is called (the
real-model run is scripts/manual_investigation.py, run by hand).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from apps.worker.investigation_main import build_runtime
from packages.agents.config import InvestigationSettings
from packages.agents.fake import FakeInvestigationModel
from packages.domain.investigation import InvestigationStatus
from packages.incident.db.models import IncidentRow, RcaReportRow
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


def test_live_incident_is_investigated_through_the_real_evidence_service(
    live_stack, api_server, session_factory
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

        model = FakeInvestigationModel(support.happy_script())
        runtime = build_runtime(
            database_url=os.environ["INCIDENT_CORE_DATABASE_URL"],
            settings=InvestigationSettings(_env_file=None),  # type: ignore[call-arg]
            model_factory=lambda spec: model,
            owner="live-e2e",
        )
        investigation_id = runtime.start(incident.id)
        assert runtime.engine.run(investigation_id) == InvestigationStatus.COMPLETED

        trace = runtime.investigations.get_trace(investigation_id)
        tool_calls = [s for s in trace["steps"] if s["kind"] == "tool_call"]
        assert {s["payload"]["tool"] for s in tool_calls} == {
            "get_metric_window",
            "get_recent_deployments",
            "get_service_health",
        }
        assert all(s["payload"]["ok"] for s in tool_calls)
        deploys = next(s for s in tool_calls if s["payload"]["tool"] == "get_recent_deployments")
        assert "1.1.0-bad" in deploys["payload"]["observation"]  # the real registry, not a script

        with session_factory() as session:
            assert session.get(IncidentRow, incident.id).status == "RCA_READY"
            rca = session.execute(
                select(RcaReportRow).where(RcaReportRow.investigation_id == investigation_id)
            ).scalar_one()
        verified = [runtime.engine._evidence.verify(e) for e in _uuids(rca.report)]
        assert verified and all(v.ok for v in verified)
    finally:
        chaos.main(["stop", "--service", SERVICE])


def _uuids(report):
    import uuid

    return [uuid.UUID(e) for e in report["supporting_evidence"]]
