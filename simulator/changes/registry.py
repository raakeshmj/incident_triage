"""Redis-backed change registry (writer side).

Contract, shared with packages/evidence/adapters/changes.py (reader side):

- `changes:deployments:{service}` -- Redis list, append-only (RPUSH), one
  JSON object per deployment:
  {deployment_id, service, environment, version, previous_version,
   commit_sha, deployed_at, deployed_by, change_type: deploy|rollback}
- `changes:config:{service}` -- Redis list, append-only, one JSON object per
  config change:
  {change_id, service, environment, key, old_value, new_value,
   changed_at, changed_by}
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis

DEPLOYMENTS_KEY = "changes:deployments:{service}"
CONFIG_KEY = "changes:config:{service}"

# What `docker compose up` actually deploys (docker-compose.yml's
# SERVICE_VERSION / SERVICE_PREVIOUS_VERSION / ENVIRONMENT per service).
SEED_SERVICES = {
    "checkout-service": {"version": "1.0.0", "previous_version": "0.9.0"},
    "payment-service": {"version": "1.0.0", "previous_version": "0.9.0"},
    "inventory-service": {"version": "1.0.0", "previous_version": "0.9.0"},
}
SEED_ENVIRONMENT = "production"
CONFIG_KEY_NAME = "request_pipeline_config_version"
SEED_CONFIG_VALUE = "v1"
DEPLOYER = "ci-pipeline"
CONFIG_WRITER = "config-service"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def service_commit(service: str) -> str | None:
    """The latest commit touching the service's code -- what a deploy of
    that service at this moment would actually ship."""
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(_REPO_ROOT),
                "log",
                "-1",
                "--format=%H",
                "--",
                f"simulator/services/{service}/",
                "simulator/services/common/",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


class ChangeRegistry:
    def __init__(self, client: redis.Redis) -> None:
        self._redis = client

    def _latest(self, key: str) -> dict[str, Any] | None:
        raw = self._redis.lindex(key, -1)
        return json.loads(raw) if raw else None

    def current_deployment(self, service: str) -> dict[str, Any] | None:
        return self._latest(DEPLOYMENTS_KEY.format(service=service))

    def find_deployment(self, service: str, version: str) -> dict[str, Any] | None:
        """Most recent deployment of `version` (what a rollback returns to)."""
        for raw in reversed(self._redis.lrange(DEPLOYMENTS_KEY.format(service=service), 0, -1)):
            record = json.loads(raw)
            if record["version"] == version:
                return record
        return None

    def current_config(self, service: str) -> dict[str, Any] | None:
        return self._latest(CONFIG_KEY.format(service=service))

    def record_deployment(
        self,
        *,
        service: str,
        version: str,
        environment: str = SEED_ENVIRONMENT,
        commit_sha: str | None = None,
        change_type: str = "deploy",
        previous_version: str | None = None,
    ) -> dict[str, Any]:
        current = self.current_deployment(service)
        record = {
            "deployment_id": str(uuid.uuid4()),
            "service": service,
            "environment": environment,
            "version": version,
            "previous_version": previous_version
            if previous_version is not None
            else (current or {}).get("version"),
            "commit_sha": commit_sha,
            "deployed_at": _now(),
            "deployed_by": DEPLOYER,
            "change_type": change_type,
        }
        self._redis.rpush(DEPLOYMENTS_KEY.format(service=service), json.dumps(record))
        return record

    def record_config_change(
        self, *, service: str, new_value: str, environment: str = SEED_ENVIRONMENT
    ) -> dict[str, Any]:
        current = self.current_config(service)
        record = {
            "change_id": str(uuid.uuid4()),
            "service": service,
            "environment": environment,
            "key": CONFIG_KEY_NAME,
            "old_value": (current or {}).get("new_value"),
            "new_value": new_value,
            "changed_at": _now(),
            "changed_by": CONFIG_WRITER,
        }
        self._redis.rpush(CONFIG_KEY.format(service=service), json.dumps(record))
        return record

    def seed(self) -> list[str]:
        """Record the initial deployment + config of every simulated service,
        once. Idempotent: a service that already has history is left alone."""
        seeded = []
        for service, spec in SEED_SERVICES.items():
            if self.current_deployment(service) is None:
                self.record_deployment(
                    service=service,
                    version=spec["version"],
                    previous_version=spec["previous_version"],
                    commit_sha=service_commit(service),
                )
                seeded.append(f"deployment:{service}")
            if self.current_config(service) is None:
                self.record_config_change(service=service, new_value=SEED_CONFIG_VALUE)
                seeded.append(f"config:{service}")
        return seeded
