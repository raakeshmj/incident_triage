"""Authorization scope for evidence queries.

Every query runs *for an incident*, and the incident -- not the caller --
decides what may be queried (docs/architecture/13-security-boundaries.md,
"Prompt-injection defense", point 4: parameters are validated against the
incident's own service/environment scope server-side, never trusted from
the caller's arguments):

- services: the incident's service plus its direct dependency neighbors
  from the service catalog (infrastructure/evidence/service-catalog.json);
- environment: the incident's own, always -- callers can't pick one;
- time: a bounded window no earlier than `MAX_LOOKBACK_BEFORE_INCIDENT`
  before the incident opened, never in the future.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from packages.domain.commands import RegisterEvidenceRefCommand
from packages.domain.results import EvidenceRefRegisteredResult
from packages.domain.views import EvidenceRefView, IncidentSummary, IncidentView
from packages.evidence import limits
from packages.evidence.errors import IncidentNotFoundError, InvalidQueryError, ScopeViolationError

# Label values are interpolated into PromQL/LogQL/TraceQL selectors, so the
# allowed alphabet is what makes that safe, on top of the catalog allow-list.
_LABEL_VALUE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class IncidentGateway(Protocol):
    """What evidence-service needs from incident-core -- its read API plus
    the one command it sends. `IncidentCoreService` satisfies this
    structurally; in a split deployment it becomes an HTTP client."""

    def get_incident_view(self, incident_id: uuid.UUID) -> IncidentView | None: ...

    def register_evidence_ref(
        self, command: RegisterEvidenceRefCommand
    ) -> EvidenceRefRegisteredResult: ...

    def list_incident_summaries(
        self,
        *,
        environment: str | None = None,
        exclude_incident_id: uuid.UUID | None = None,
        created_before: datetime | None = None,
        limit: int = 200,
    ) -> list[IncidentSummary]: ...

    def list_evidence_refs(self, incident_id: uuid.UUID) -> list[EvidenceRefView]: ...


@dataclass(frozen=True)
class ServiceEntry:
    name: str
    dependencies: tuple[str, ...]
    repo_paths: tuple[str, ...]


@dataclass(frozen=True)
class ServiceCatalog:
    services: dict[str, ServiceEntry]

    @classmethod
    def load(cls, path: str | Path) -> ServiceCatalog:
        data = json.loads(Path(path).read_text())
        entries = {
            name: ServiceEntry(
                name=name,
                dependencies=tuple(spec.get("dependencies", [])),
                repo_paths=tuple(spec.get("repo_paths", [])),
            )
            for name, spec in data["services"].items()
        }
        for entry in entries.values():
            if not _LABEL_VALUE.match(entry.name):
                raise ValueError(f"invalid service name in catalog: {entry.name!r}")
            for path in entry.repo_paths:
                if path.startswith("/") or ".." in Path(path).parts:
                    raise ValueError(f"repo path must be repo-relative: {path!r}")
        return cls(services=entries)

    def neighborhood(self, service: str) -> frozenset[str]:
        entry = self.services.get(service)
        allowed = {service}
        if entry is not None:
            allowed.update(entry.dependencies)
        allowed.update(name for name, e in self.services.items() if service in e.dependencies)
        return frozenset(allowed)

    def repo_paths(self, service: str) -> tuple[str, ...]:
        entry = self.services.get(service)
        return entry.repo_paths if entry is not None else ()


@dataclass(frozen=True)
class IncidentScope:
    incident_id: uuid.UUID
    service: str
    environment: str
    status: str
    created_at: datetime
    allowed_services: frozenset[str]
    regions: tuple[str, ...] = field(default=())
    alert_types: tuple[str, ...] = field(default=())

    @classmethod
    def from_incident(cls, incident: IncidentView, catalog: ServiceCatalog) -> IncidentScope:
        return cls(
            incident_id=incident.id,
            service=incident.service,
            environment=incident.environment,
            status=incident.status,
            created_at=incident.created_at,
            allowed_services=catalog.neighborhood(incident.service),
            regions=tuple(
                sorted({a.labels["region"] for a in incident.alerts if "region" in a.labels})
            ),
            alert_types=tuple(
                sorted({a.labels["alertname"] for a in incident.alerts if "alertname" in a.labels})
            ),
        )

    def service_or_default(self, service: str | None) -> str:
        """The service a query targets: the incident's own by default, or
        a dependency neighbor -- anything else is a scope violation."""
        target = service or self.service
        if not _LABEL_VALUE.match(target):
            raise InvalidQueryError(f"malformed service name {target!r}")
        if target not in self.allowed_services:
            raise ScopeViolationError(
                f"service {target!r} is outside incident {self.incident_id}'s scope "
                f"(allowed: {sorted(self.allowed_services)})"
            )
        return target

    def window(
        self,
        start: datetime | None,
        end: datetime | None,
        *,
        now: datetime,
        default: timedelta = limits.DEFAULT_WINDOW,
        max_length: timedelta = limits.MAX_WINDOW,
    ) -> tuple[datetime, datetime]:
        """Resolve and bound a query window. `end` defaults to now and is
        clamped to now; `start` defaults to `end - default`."""
        for value in (start, end):
            if value is not None and value.tzinfo is None:
                raise InvalidQueryError("window timestamps must be timezone-aware")
        resolved_end = min(end or now, now)
        resolved_start = start or max(
            resolved_end - default, self.created_at - limits.MAX_LOOKBACK_BEFORE_INCIDENT
        )
        if resolved_start >= resolved_end:
            raise InvalidQueryError("window start must be before its end (and not in the future)")
        length = resolved_end - resolved_start
        if length > max_length:
            raise InvalidQueryError(f"window {length} exceeds the maximum {max_length}")
        if length < limits.MIN_WINDOW:
            raise InvalidQueryError(f"window {length} is below the minimum {limits.MIN_WINDOW}")
        earliest = self.created_at - limits.MAX_LOOKBACK_BEFORE_INCIDENT
        if resolved_start < earliest:
            raise ScopeViolationError(
                f"window starts before {earliest.isoformat()} "
                f"({limits.MAX_LOOKBACK_BEFORE_INCIDENT} before the incident opened)"
            )
        return resolved_start.astimezone(UTC), resolved_end.astimezone(UTC)


def resolve_scope(
    gateway: IncidentGateway, catalog: ServiceCatalog, incident_id: uuid.UUID
) -> IncidentScope:
    incident = gateway.get_incident_view(incident_id)
    if incident is None:
        raise IncidentNotFoundError(f"incident {incident_id} not found")
    return IncidentScope.from_incident(incident, catalog)


def bounded_limit(requested: int | None, *, default: int, maximum: int) -> int:
    value = default if requested is None else requested
    if value < 1:
        raise InvalidQueryError("limit must be at least 1")
    return min(value, maximum)
