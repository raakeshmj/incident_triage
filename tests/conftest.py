from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
# Load .env if present, then fall back to .env.example so the suite works
# out of the box against the documented docker-compose defaults.
load_dotenv(_ROOT / ".env", override=False)
load_dotenv(_ROOT / ".env.example", override=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        path = str(item.fspath)
        if "/tests/integration/" in path:
            item.add_marker(pytest.mark.integration)
        elif "/tests/e2e/" in path:
            item.add_marker(pytest.mark.e2e)


STACK_BACKENDS = {
    "prometheus": "PROMETHEUS_URL",
    "loki": "LOKI_URL",
    "tempo": "TEMPO_URL",
}
# Functional probes: single-binary Loki/Tempo serve queries while their ring-based
# /ready endpoints still report 503, so /ready is not a usable signal here.
_READY_PATHS = {"prometheus": "/-/ready", "loki": "/loki/api/v1/labels", "tempo": "/api/echo"}


@pytest.fixture(scope="session")
def stack_urls() -> dict[str, str]:
    """Base URLs of the live telemetry backends; skips unless all are ready."""
    import os

    import httpx

    urls = {name: os.environ.get(var, "") for name, var in STACK_BACKENDS.items()}
    for name, url in urls.items():
        try:
            ready = httpx.get(url + _READY_PATHS[name], timeout=3.0).status_code == 200
        except (httpx.HTTPError, ValueError):
            ready = False
        if not ready:
            pytest.skip(f"{name} not ready at {url!r}; run `make infra-up-full`")
    return urls
