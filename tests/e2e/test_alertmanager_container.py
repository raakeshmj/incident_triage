"""Real Alertmanager container -> real running API: the one hop Phase 3
explicitly requires not to fake ("Do not fake the Alertmanager -> API
integration in the e2e test. Use the actual local containers.").

This starts the actual `prom/alertmanager` image with the actual
`infrastructure/alertmanager/alertmanager.yml` (the same file
docker-compose.yml wires up for local dev -- not a test-specific fork), a
real uvicorn server for the real FastAPI app bound to the same host port
that config already targets (`host.docker.internal:8000`), fires a
synthetic alert into Alertmanager's own v2 API, and polls the real
Postgres database for the Alert row Alertmanager's webhook should have
produced through POST /api/v1/alerts/alertmanager.

Requires Docker and a reachable image registry; skips (like the
Postgres/Redis fixtures elsewhere in this suite) with a clear message if
either isn't available, rather than failing opaquely.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import select

from packages.incident.db.models import AlertRow

REPO_ROOT = Path(__file__).resolve().parents[2]
ALERTMANAGER_CONFIG = REPO_ROOT / "infrastructure" / "alertmanager" / "alertmanager.yml"
API_PORT = 8000
ALERTMANAGER_PORT = 9093
CONTAINER_NAME = "ii-e2e-alertmanager-test"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


@pytest.fixture(scope="module")
def api_server(engine) -> Iterator[None]:
    """A real uvicorn server for the real app, on the port alertmanager.yml targets."""
    from apps.api.main import app

    config = uvicorn.Config(app, host="0.0.0.0", port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)
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


@pytest.fixture(scope="module")
def alertmanager_container() -> Iterator[None]:
    if not _docker_available():
        pytest.skip("Docker is not available in this environment")

    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--add-host=host.docker.internal:host-gateway",
            "-p",
            f"{ALERTMANAGER_PORT}:9093",
            "-v",
            f"{ALERTMANAGER_CONFIG}:/etc/alertmanager/alertmanager.yml:ro",
            "prom/alertmanager:v0.27.0",
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"could not start the alertmanager container: {run.stderr.strip()}")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://localhost:{ALERTMANAGER_PORT}/-/ready", timeout=1.0)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    else:
        subprocess.run(["docker", "logs", CONTAINER_NAME], capture_output=True)
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
        pytest.skip("alertmanager container did not become ready within 20s")

    yield

    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)


def test_real_alertmanager_delivers_alert_to_running_api(
    api_server, alertmanager_container, session_factory
):
    marker_service = f"e2e-container-{uuid.uuid4().hex[:8]}"
    alert = {
        "labels": {
            "alertname": "HighErrorRate",
            "service": marker_service,
            "environment": "production",
            "region": "us-east-1",
            "severity": "critical",
            "alert_type": "availability",
        },
        "annotations": {
            "summary": f"{marker_service} error rate is high",
            "description": "synthetic alert injected by test_alertmanager_container.py",
            "runbook_url": "https://runbooks.example.com/incident-intelligence/high-error-rate",
        },
        "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    post_response = httpx.post(
        f"http://localhost:{ALERTMANAGER_PORT}/api/v2/alerts", json=[alert], timeout=10.0
    )
    assert post_response.status_code == 200

    # Alertmanager's group_wait (5s, infrastructure/alertmanager/alertmanager.yml) delays
    # the first notification; poll the real database for the row its webhook produces.
    deadline = time.monotonic() + 30
    alert_row: AlertRow | None = None
    while time.monotonic() < deadline:
        with session_factory() as session:
            alert_row = session.execute(
                select(AlertRow).where(AlertRow.labels["service"].astext == marker_service)
            ).scalar_one_or_none()
        if alert_row is not None:
            break
        time.sleep(1.0)

    assert alert_row is not None, (
        "no Alert row appeared for the synthetic service within 30s -- "
        "the real Alertmanager container never reached the real running API"
    )
    assert alert_row.source == "prometheus"
    assert alert_row.severity == "critical"
    assert alert_row.labels["region"] == "us-east-1"
