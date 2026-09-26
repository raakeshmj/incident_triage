from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from packages.incident.db.base import make_engine, make_session_factory


@pytest.fixture(scope="session")
def engine() -> Engine:
    eng = make_engine(os.environ["INCIDENT_CORE_DATABASE_URL"])
    try:
        with eng.connect():
            pass
    except OperationalError as exc:
        pytest.skip(
            f"Postgres not reachable at INCIDENT_CORE_DATABASE_URL ({exc}); "
            "run `make infra-up && make migrate` first"
        )
    return eng


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    def _truncate() -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE TABLE "
                    "incident_core.verification_evidence, "
                    "incident_core.verification_observations, "
                    "incident_core.verifications, "
                    "incident_core.remediation_baselines, "
                    "incident_core.remediation_timeline, "
                    "incident_core.remediation_executions, "
                    "incident_core.remediation_approvals, "
                    "incident_core.remediation_policy_decisions, "
                    "incident_core.remediations, "
                    "incident_core.kill_switches, "
                    "incident_core.rca_reports, "
                    "incident_core.hypothesis_evidence_links, "
                    "incident_core.hypotheses, "
                    "incident_core.investigation_steps, "
                    "incident_core.investigations, "
                    "incident_core.evidence_refs, "
                    "incident_core.outbox_events, "
                    "incident_core.processed_commands, "
                    "incident_core.consumed_events, "
                    "incident_core.alerts, "
                    "incident_core.incidents "
                    "RESTART IDENTITY CASCADE"
                )
            )

    _truncate()
    yield
    _truncate()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    from apps.api.main import app

    with TestClient(app) as test_client:
        yield test_client


# The port Alertmanager delivers to: infrastructure/alertmanager/alertmanager.yml's
# default, or whatever ALERTMANAGER_WEBHOOK_URL / API_PORT point it at on this host.
API_PORT = int(os.environ.get("API_PORT", "8000"))


@pytest.fixture(scope="module")
def api_server(engine: Engine) -> Iterator[None]:
    """A real uvicorn server for the real app on API_PORT -- the port
    Alertmanager delivers to (8000 unless this host remaps it)."""
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", API_PORT)) == 0:
            pytest.skip(f"port {API_PORT} is already in use (is `make run-api` running?)")

    from apps.api.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=API_PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://localhost:{API_PORT}/healthz", timeout=1.0)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        pytest.fail("API server did not start within 10s")
    yield
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def quiet_checkout() -> None:
    """Live chaos tests share one stack: wait until no alert is active for
    checkout-service, so this test's fault starts a *new* firing episode
    (Alertmanager doesn't re-notify an episode that is already firing)."""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            alerts = httpx.get("http://localhost:9093/api/v2/alerts", timeout=3).json()
        except (httpx.HTTPError, ValueError):
            return  # no Alertmanager: the test's own readiness check will skip
        if not any(a["labels"].get("service") == "checkout-service" for a in alerts):
            return
        time.sleep(5)
    pytest.fail("checkout-service alerts still active after 300s; the stack isn't quiet")
