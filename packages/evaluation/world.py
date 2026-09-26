"""A scenario's deterministic world, served through the real evidence path.

    investigation tool -> ToolExecutor -> EvidenceService -> real adapter
        -> (this module) canned backend: Prometheus / Loki / Tempo over an
           in-process httpx transport; change registries and Git as fixture
           subclasses of the real adapters

Only the backends are simulated. PromQL/LogQL rendering, scope checks,
bounds, normalization, hashing, persistence and evidence-ref registration
are all the production code, so a scenario exercises exactly what a live
investigation does -- without Prometheus, Loki, Tempo, Redis or Git, and
without a network. All times are relative to `anchor` (the moment the
incident opens); the same scenario + anchor always yields the same data.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
from sqlalchemy.orm import Session

from packages.evaluation.scenario import HEALTHY_DEFAULTS, Scenario, Series
from packages.evidence.adapters._http import iso
from packages.evidence.adapters.changes import (
    CONFIG_KEY,
    DEPLOYMENTS_KEY,
    ChangeRegistryAdapter,
)
from packages.evidence.adapters.git import GitAdapter
from packages.evidence.adapters.loki import LokiAdapter
from packages.evidence.adapters.prometheus import METRICS, PrometheusAdapter, render
from packages.evidence.adapters.tempo import TempoAdapter
from packages.evidence.errors import BackendUnavailableError
from packages.evidence.scope import IncidentGateway, ServiceCatalog
from packages.evidence.service import EvidenceService

_SERVICE = re.compile(r'service="([^"]+)"')
_SEVERITIES = re.compile(r'severity=~"([^"]+)"')
_LINE_FILTER = re.compile(r'\|= "((?:[^"\\]|\\.)*)"')
_COUNT = re.compile(r"^sum\(count_over_time\((.*) \[(\d+)s\]\)\)$", re.S)


class ScenarioWorld:
    def __init__(self, scenario: Scenario, catalog: ServiceCatalog, anchor: datetime) -> None:
        self.scenario = scenario
        self.catalog = catalog
        self.anchor = anchor
        self._queries = self._query_index()
        # Mutable world state (Phase 8 lifecycle evaluation): the change
        # records the registries serve, the runtime control plane, and when a
        # remediation took effect. `ScenarioExecutor` changes these; the
        # evidence path then observes the result, exactly like verification
        # observes the live simulator.
        self.records = self.change_records()
        self.runtime: dict[str, dict[str, Any]] = {}
        self.remediated_at: datetime | None = None
        lifecycle = scenario.lifecycle
        self.remediation_effective = lifecycle.remediation_effective if lifecycle else True

    def at(self, minutes: float) -> datetime:
        return self.anchor + timedelta(minutes=minutes)

    # --- metrics -------------------------------------------------------------

    def _query_index(self) -> dict[str, tuple[str, str]]:
        env = self.scenario.environment
        return {
            render(metric, service, env): (metric, service)
            for service in self.catalog.services
            for metric in METRICS
        }

    def _series(self, service: str, metric: str, dependency: str | None) -> Series:
        spec = self.scenario.world.metrics.get(service, {}).get(metric)
        if spec is not None:
            if dependency is not None and dependency in spec.by_dependency:
                return spec.by_dependency[dependency]
            if spec.series is not None and dependency is None:
                return spec.series
        return Series()

    def metric_value(
        self, service: str, metric: str, at: datetime, dependency: str | None = None
    ) -> float:
        series = self._series(service, metric, dependency)
        baseline = series.baseline if series.baseline is not None else HEALTHY_DEFAULTS[metric]
        incident = series.incident if series.incident is not None else baseline
        if self.remediation_effective and self.remediated_at and at >= self.remediated_at:
            return baseline  # the fix took: every signal returns to its baseline
        return incident if at >= self.at(series.change_at_min) else baseline

    def _metric_series_labels(self, metric: str, service: str) -> list[dict[str, str]]:
        if metric != "dependency_error_rate":
            return [{}]
        entry = self.catalog.services.get(service)
        return [{"dependency": d} for d in (entry.dependencies if entry else ())]

    def _prometheus(self, request: httpx.Request) -> httpx.Response:
        if "prometheus" in self.scenario.world.unavailable:
            return httpx.Response(503, json={"status": "error"})
        params = request.url.params
        found = self._queries.get(params["query"])
        if found is None:
            return httpx.Response(400, json={"status": "error", "error": "unknown query"})
        metric, service = found
        results: list[dict[str, Any]] = []
        if request.url.path.endswith("query_range"):
            start, end, step = float(params["start"]), float(params["end"]), float(params["step"])
            count = int(math.floor((end - start) / step)) + 1
            for labels in self._metric_series_labels(metric, service):
                values = []
                for i in range(count):
                    ts = start + i * step
                    at = datetime.fromtimestamp(ts, tz=self.anchor.tzinfo)
                    value = self.metric_value(service, metric, at, labels.get("dependency"))
                    values.append([ts, repr(value)])
                results.append({"metric": labels, "values": values})
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "matrix", "result": results}}
            )
        ts = float(params["time"])
        at = datetime.fromtimestamp(ts, tz=self.anchor.tzinfo)
        for labels in self._metric_series_labels(metric, service):
            value = self.metric_value(service, metric, at, labels.get("dependency"))
            results.append({"metric": labels, "value": [ts, repr(value)]})
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": results}}
        )

    # --- logs ----------------------------------------------------------------

    def log_lines(self, service: str) -> list[tuple[datetime, str]]:
        lines: list[tuple[datetime, str]] = []
        for spec in self.scenario.world.logs.get(service, []):
            for i in range(spec.count):
                at = self.at(spec.at_min) + timedelta(seconds=i * 2)
                body = {
                    "timestamp": iso(at),
                    "severity": spec.severity,
                    "message": spec.message,
                    "service": service,
                    **spec.fields,
                }
                lines.append((at, json.dumps(body, sort_keys=True)))
        lines.sort(key=lambda item: item[0], reverse=True)
        return lines

    def _matching(self, query: str, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
        service_match = _SERVICE.search(query)
        if service_match is None:
            return []
        severities_match = _SEVERITIES.search(query)
        severities = set(severities_match.group(1).split("|")) if severities_match else None
        needles = [unquote(n).replace('\\"', '"') for n in _LINE_FILTER.findall(query)]
        out = []
        for at, line in self.log_lines(service_match.group(1)):
            if not start <= at <= end:
                continue
            if severities is not None and json.loads(line)["severity"] not in severities:
                continue
            if all(n in line for n in needles):
                out.append((at, line))
        return out

    def _loki(self, request: httpx.Request) -> httpx.Response:
        if "loki" in self.scenario.world.unavailable:
            return httpx.Response(503, json={"status": "error"})
        params = request.url.params
        tz = self.anchor.tzinfo
        if request.url.path.endswith("query_range"):
            start = datetime.fromtimestamp(int(params["start"]) / 1e9, tz=tz)
            end = datetime.fromtimestamp(int(params["end"]) / 1e9, tz=tz)
            lines = self._matching(params["query"], start, end)[: int(params["limit"])]
            service = (_SERVICE.search(params["query"]) or [None, "unknown"])[1]
            values = [[str(int(at.timestamp() * 1e9)), line] for at, line in lines]
            streams = [{"stream": {"service": service}, "values": values}] if values else []
            return httpx.Response(200, json={"status": "success", "data": {"result": streams}})
        count = _COUNT.match(params["query"])
        if count is None:
            return httpx.Response(400, json={"status": "error"})
        end = datetime.fromtimestamp(float(params["time"]), tz=tz)
        start = end - timedelta(seconds=int(count.group(2)))
        total = len(self._matching(count.group(1), start, end))
        result: list[dict[str, Any]] = [{"value": [end.timestamp(), str(total)]}] if total else []
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    # --- traces --------------------------------------------------------------

    def _tempo(self, request: httpx.Request) -> httpx.Response:
        if "tempo" in self.scenario.world.unavailable:
            return httpx.Response(503, json={})
        if request.url.path.startswith("/api/traces/"):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"traces": []})

    # --- change records --------------------------------------------------------

    def change_records(self) -> dict[str, list[dict[str, Any]]]:
        env = self.scenario.environment
        records: dict[str, list[dict[str, Any]]] = {}
        # every service has a long-standing deployment on record
        for service in self.catalog.services:
            previous = next(
                (
                    d.previous_version
                    for d in sorted(self.scenario.world.deployments, key=lambda d: d.at_min)
                    if d.service == service
                ),
                "1.0.0",
            )
            records.setdefault(DEPLOYMENTS_KEY.format(service=service), []).append(
                _deployment(service, env, self.at(-3 * 24 * 60), previous, None, "baseline")
            )
        for d in sorted(self.scenario.world.deployments, key=lambda d: d.at_min):
            records[DEPLOYMENTS_KEY.format(service=d.service)].append(
                _deployment(
                    d.service,
                    env,
                    self.at(d.at_min),
                    d.version,
                    d.previous_version,
                    d.change_type,
                    d.commit_sha,
                )
            )
        for c in sorted(self.scenario.world.config_changes, key=lambda c: c.at_min):
            records.setdefault(CONFIG_KEY.format(service=c.service), []).append(
                {
                    "change_id": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{c.service}/{c.key}/{c.at_min}")
                    ),
                    "service": c.service,
                    "environment": env,
                    "key": c.key,
                    "old_value": c.old_value,
                    "new_value": c.new_value,
                    "changed_at": iso(self.at(c.at_min)),
                    "changed_by": "config-bot",
                }
            )
        return records

    def commits(self) -> list[dict[str, Any]]:
        out = []
        for c in sorted(self.scenario.world.commits, key=lambda c: c.at_min, reverse=True):
            at = iso(self.at(c.at_min))
            out.append(
                {
                    "sha": c.sha,
                    "author": c.author,
                    "authored_at": at,
                    "committed_at": at,
                    "subject": c.subject,
                    "files": [{"path": f, "additions": 12, "deletions": 3} for f in c.files],
                }
            )
        return out

    # --- the evidence service ----------------------------------------------------

    def evidence_service(
        self, session_factory: Callable[[], Session], gateway: IncidentGateway
    ) -> EvidenceService:
        def client(handler: Callable[[httpx.Request], httpx.Response], name: str) -> httpx.Client:
            return httpx.Client(
                base_url=f"http://{name}.scenario", transport=httpx.MockTransport(handler)
            )

        return EvidenceService(
            session_factory=session_factory,
            gateway=gateway,
            catalog=self.catalog,
            prometheus=PrometheusAdapter(client(self._prometheus, "prometheus")),
            loki=LokiAdapter(client(self._loki, "loki")),
            tempo=TempoAdapter(client(self._tempo, "tempo")),
            changes=self.change_registry(),
            git=self.git(),
        )

    def change_registry(self) -> FixtureChangeRegistry:
        return FixtureChangeRegistry(
            self.records,
            available="changes" not in self.scenario.world.unavailable,
            runtime=self.runtime,
        )

    def git(self) -> FixtureGit:
        return FixtureGit(self.commits(), available="git" not in self.scenario.world.unavailable)


def _deployment(
    service: str,
    environment: str,
    at: datetime,
    version: str,
    previous: str | None,
    change_type: str,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    return {
        "deployment_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{service}/{version}/{iso(at)}")),
        "service": service,
        "environment": environment,
        "version": version,
        "previous_version": previous,
        "commit_sha": commit_sha,
        "deployed_at": iso(at),
        "deployed_by": "ci",
        "change_type": change_type,
    }


class FixtureChangeRegistry(ChangeRegistryAdapter):
    """The real adapter's windowing/normalization over scenario records
    instead of Redis lists."""

    def __init__(
        self,
        records: dict[str, list[dict[str, Any]]],
        *,
        available: bool,
        runtime: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._records = records
        self._available = available
        self._runtime_state = runtime if runtime is not None else {}

    def _read(self, key: str) -> list[dict[str, Any]]:
        if not self._available:
            raise BackendUnavailableError("change registry unreachable")
        return list(self._records.get(key, []))

    def _runtime(self, service: str) -> tuple[int, dict[str, str]]:
        if not self._available:
            raise BackendUnavailableError("runtime registry unreachable")
        state = self._runtime_state.get(service, {})
        return int(state.get("replicas", 1)), dict(state.get("flags", {}))


class FixtureGit(GitAdapter):
    """The real adapter's output shaping over scenario commits instead of a
    `git log` subprocess."""

    def __init__(self, commits: list[dict[str, Any]], *, available: bool) -> None:
        super().__init__(".")
        self._commits = commits
        self._available = available

    def _log(self, *args: str, paths: tuple[str, ...]) -> list[dict[str, Any]]:
        if not self._available:
            raise BackendUnavailableError("git is not available")
        options = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a}
        positional = [a for a in args if not a.startswith("-")]
        since = options.get("--since")
        until = options.get("--until")
        limit = int(options.get("--max-count", "20"))
        selected = []
        for commit in self._commits:
            if positional and not commit["sha"].startswith(positional[0]):
                continue
            if not any(f["path"].startswith(p) for f in commit["files"] for p in paths):
                continue
            at = datetime.fromisoformat(commit["committed_at"])
            if since and at < datetime.fromisoformat(since):
                continue
            if until and at > datetime.fromisoformat(until):
                continue
            selected.append(commit)
        return selected[:limit]
