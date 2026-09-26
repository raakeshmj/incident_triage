"""Phase 4 end to end, on the real stack (`make infra-up-full`), nothing faked:

 1. trigger a real chaos incident (bad-deployment on checkout-service);
 2. Prometheus fires, Alertmanager delivers, the running API opens an incident;
 3-7. metrics, logs, traces, deployments and Git changes are queried through
      the tool layer -> evidence service -> adapters -> real backends;
 8-9. every observation is an immutable EvidenceRecord with a registered
      EvidenceRef, and every returned evidence id resolves and re-verifies;
10. recovery (rollback) resolves the alert, and the incident follows the
    state machine's own edge: TRIAGING -> CANCELLED.

Takes ~3-4 minutes: alert `for:` windows and Alertmanager grouping are real.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from apps.evidence.dependencies import get_evidence_service
from packages.incident.db.models import IncidentRow
from packages.incident.service import IncidentCoreService
from packages.tools.contracts import ToolContext
from packages.tools.executor import ToolExecutor
from simulator.chaos import cli as chaos

pytestmark = pytest.mark.stack

SERVICE = "checkout-service"


def _wait_for(predicate, *, timeout: float, interval: float = 3.0, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")


@pytest.fixture
def live_stack(stack_urls):
    for name, url in {
        "alertmanager": "http://localhost:9093/-/ready",
        SERVICE: "http://localhost:8001/health",
    }.items():
        try:
            httpx.get(url, timeout=3).raise_for_status()
        except httpx.HTTPError:
            pytest.skip(f"{name} not reachable; run `make infra-up-full`")
    return stack_urls


def test_live_incident_evidence_and_resolution(
    live_stack, quiet_checkout, api_server, session_factory
):
    started = datetime.now(UTC)
    chaos.main(["stop", "--service", SERVICE])  # clean slate
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
        # 1-2: real alert -> real incident
        def opened() -> IncidentRow | None:
            with session_factory() as session:
                return session.execute(
                    select(IncidentRow).where(
                        IncidentRow.service == SERVICE, IncidentRow.created_at >= started
                    )
                ).scalar_one_or_none()

        incident = _wait_for(opened, timeout=180, what="an incident from the real alert")
        assert incident.status == "TRIAGING"

        # 3-7: evidence through the tool layer only
        tools = ToolExecutor(get_evidence_service(), ToolContext(incident_id=incident.id))
        calls = {
            "metrics": ("get_metrics", {"metric": "error_rate"}),
            "health": ("get_service_health", {}),
            "logs": ("get_logs", {"severities": ["ERROR"]}),
            "error_traces": ("get_traces", {"mode": "errors", "limit": 5}),
            "deploys": ("get_deploys", {}),
            "config": ("get_config_history", {}),
            "code": ("get_git_diff", {}),
            "commits": ("get_recent_commits", {"limit": 5}),
            "history": ("search_historical_incidents", {}),
        }
        results = {key: tools.execute(name, args) for key, (name, args) in calls.items()}
        failed = {k: r.error for k, r in results.items() if not r.ok}
        assert not failed, failed

        assert results["metrics"].data["current_value"] > 0.1
        assert results["health"].data["status"] == "degraded"
        assert results["logs"].data["error_lines_total"] > 0
        assert results["error_traces"].data["trace_count"] > 0
        assert results["deploys"].data["current"]["version"] == "1.1.0-bad"
        assert results["commits"].data["commits"]

        trace_id = results["error_traces"].data["traces"][0]["trace_id"]
        trace = tools.execute("get_trace", {"trace_id": trace_id})
        assert trace.ok and trace.data["error_span_count"] >= 1
        trace_logs = tools.execute("get_logs", {"trace_id": trace_id})
        assert trace_logs.ok and trace_logs.data["matching_lines_total"] >= 1

        # 8-9: everything is a persisted, referenced, verifiable record
        returned = [r.evidence_id for r in (*results.values(), trace, trace_logs)]
        service = get_evidence_service()
        replay = service.get_incident_evidence(incident.id)
        assert [r.evidence_id for r in replay] == returned
        refs = {r.id for r in IncidentCoreService(session_factory).list_evidence_refs(incident.id)}
        assert refs == set(returned)
        for evidence_id in returned:
            assert service.verify(evidence_id).ok
        assert service.get_evidence(uuid.UUID(str(returned[0]))).incident_id == incident.id
    finally:
        # 10: recovery = rollback; the alert resolves and the incident follows
        chaos.main(["stop", "--service", SERVICE])

    def closed() -> str | None:
        with session_factory() as session:
            status = session.get(IncidentRow, incident.id).status
        return status if status != "TRIAGING" else None

    assert _wait_for(closed, timeout=240, interval=5, what="alert resolution") == "CANCELLED"
    after = tools.execute("get_deploys", {})
    assert after.ok and after.data["current"]["change_type"] == "rollback"
