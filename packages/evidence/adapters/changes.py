"""Deployment and config-change adapter (read-only).

Reads the simulated environment's change registries -- the Redis lists the
simulated CI/CD and config systems append to (simulator/changes/registry.py
documents the key/record contract). This adapter never writes and never
synthesizes a record: if the registry holds nothing for a service/window,
the evidence says exactly that.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

import redis

from packages.evidence.adapters._http import iso
from packages.evidence.errors import BackendTimeoutError, BackendUnavailableError
from packages.evidence.models import Observation
from packages.evidence.sanitize import clean_text
from packages.evidence.types import EvidenceType, SourceSystem

DEPLOYMENTS_KEY = "changes:deployments:{service}"
CONFIG_KEY = "changes:config:{service}"
# Bounded read: only the tail of each append-only list is ever scanned.
MAX_RECORDS_SCANNED = 500


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class ChangeRegistryAdapter:
    def __init__(self, client: redis.Redis) -> None:
        self._redis = client

    def _read(self, key: str) -> list[dict[str, Any]]:
        try:
            raw = cast(list[str], self._redis.lrange(key, -MAX_RECORDS_SCANNED, -1))
        except redis.TimeoutError as exc:
            raise BackendTimeoutError("change registry timed out") from exc
        except redis.RedisError as exc:
            raise BackendUnavailableError("change registry unreachable") from exc
        records = []
        for item in raw:
            try:
                record = json.loads(item)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def deployments(
        self, *, service: str, environment: str, start: datetime, end: datetime, limit: int
    ) -> Observation:
        key = DEPLOYMENTS_KEY.format(service=service)
        records = [r for r in self._read(key) if r.get("environment") == environment]
        as_of_end = [r for r in records if _parse_ts(r["deployed_at"]) <= end]
        in_window = [r for r in as_of_end if _parse_ts(r["deployed_at"]) >= start]
        in_window.sort(key=lambda r: r["deployed_at"], reverse=True)
        current = as_of_end[-1] if as_of_end else None
        shown = in_window[:limit]

        normalized = {
            "service": service,
            "environment": environment,
            "window": {"start": iso(start), "end": iso(end)},
            "current": _deployment_view(current) if current else None,
            "deployments_in_window": [_deployment_view(r) for r in shown],
            "deployments_in_window_total": len(in_window),
        }
        if current is None:
            summary = f"no deployment on record for {service} in {environment}"
        else:
            summary = (
                f"{service} running {current['version']} (from {current.get('previous_version')}, "
                f"deployed {current['deployed_at']}); {len(in_window)} deployment(s) in window"
            )
        return Observation(
            evidence_type=EvidenceType.DEPLOYMENT,
            source_system=SourceSystem.DEPLOYMENT_REGISTRY,
            operation="recent_deployments",
            subject_service=service,
            query_spec={
                "template": "recent_deployments",
                "params": {"service": service, "environment": environment, "limit": limit},
            },
            source_reference={
                "registry": "redis",
                "key": key,
                "deployment_ids": [r["deployment_id"] for r in shown],
            },
            raw_response={"current": current, "in_window": shown},
            raw_truncated=len(in_window) > limit,
            normalized_payload=normalized,
            summary=summary,
            result_count=len(shown),
            observed_at=_parse_ts(current["deployed_at"]) if current else end,
            window_start=start,
            window_end=end,
        )

    def config_changes(
        self, *, service: str, environment: str, start: datetime, end: datetime, limit: int
    ) -> Observation:
        key = CONFIG_KEY.format(service=service)
        records = [r for r in self._read(key) if r.get("environment") == environment]
        as_of_end = [r for r in records if _parse_ts(r["changed_at"]) <= end]
        in_window = [r for r in as_of_end if _parse_ts(r["changed_at"]) >= start]
        in_window.sort(key=lambda r: r["changed_at"], reverse=True)
        effective: dict[str, Any] = {}
        for record in as_of_end:
            effective[clean_text(str(record["key"]), 120)] = record["new_value"]
        shown = in_window[:limit]

        normalized = {
            "service": service,
            "environment": environment,
            "window": {"start": iso(start), "end": iso(end)},
            "effective_config": effective,
            "changes_in_window": [_config_view(r) for r in shown],
            "changes_in_window_total": len(in_window),
        }
        return Observation(
            evidence_type=EvidenceType.CONFIGURATION,
            source_system=SourceSystem.CONFIG_REGISTRY,
            operation="config_changes",
            subject_service=service,
            query_spec={
                "template": "config_changes",
                "params": {"service": service, "environment": environment, "limit": limit},
            },
            source_reference={
                "registry": "redis",
                "key": key,
                "change_ids": [r["change_id"] for r in shown],
            },
            raw_response={"effective_from": as_of_end[-limit:], "in_window": shown},
            raw_truncated=len(in_window) > limit,
            normalized_payload=normalized,
            summary=(
                f"{len(in_window)} config change(s) for {service} in window; "
                f"effective: {effective or 'none on record'}"
            ),
            result_count=len(shown),
            observed_at=_parse_ts(as_of_end[-1]["changed_at"]) if as_of_end else end,
            window_start=start,
            window_end=end,
        )


def _deployment_view(record: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "deployment_id",
        "version",
        "previous_version",
        "commit_sha",
        "deployed_at",
        "deployed_by",
        "change_type",
    )
    return {k: record.get(k) for k in keys}


def _config_view(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("change_id", "key", "old_value", "new_value", "changed_at", "changed_by")
    return {k: record.get(k) for k in keys}
