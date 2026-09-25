"""evidence-service: the controlled boundary between telemetry backends and
anything that investigates an incident.

Every public operation follows one path (docs/architecture/08-evidence-model.md):

    resolve the incident's scope (incident-core read API)
      -> validate/bound every parameter against that scope
      -> adapter queries the backend (allow-listed, bounded)
      -> persist an immutable, content-hashed EvidenceRecord (evidence schema)
      -> register the EvidenceRef with incident-core (incident_core schema)
      -> return a compact EvidenceItem (ids + hash + summary + normalized data)

Nothing is returned that wasn't persisted and referenced first, so anything
a caller (later: the investigation agent) can cite already exists as a
durable record. There is no operation that takes a raw backend query.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from packages.domain.commands import RegisterEvidenceRefCommand
from packages.evidence import limits, repository
from packages.evidence.adapters.changes import ChangeRegistryAdapter
from packages.evidence.adapters.git import GitAdapter
from packages.evidence.adapters.history import IncidentHistoryAdapter
from packages.evidence.adapters.loki import LokiAdapter
from packages.evidence.adapters.prometheus import LATENCY_METRICS, PrometheusAdapter
from packages.evidence.adapters.tempo import SEARCH_MODES, TempoAdapter
from packages.evidence.errors import (
    BackendTimeoutError,
    EvidenceError,
    EvidenceNotFoundError,
    InvalidQueryError,
    ScopeViolationError,
)
from packages.evidence.hashing import canonical_json, content_hash, verify_content_hash
from packages.evidence.models import EvidenceItem, EvidenceRecord, Observation
from packages.evidence.sanitize import strip_nul
from packages.evidence.scope import (
    IncidentGateway,
    IncidentScope,
    ServiceCatalog,
    bounded_limit,
    resolve_scope,
)
from packages.evidence.types import EVIDENCE_TTL_DAYS, EXPIRING_TYPES
from packages.telemetry.context import bind_context
from packages.telemetry.logging import get_logger
from packages.telemetry.metrics import get_metrics
from packages.telemetry.tracing import start_span

log = get_logger(__name__)
metrics = get_metrics()

Collector = Callable[[IncidentScope, datetime], Observation]


@dataclass(frozen=True)
class IntegrityReport:
    evidence_id: uuid.UUID
    content_hash_matches: bool
    ref_registered: bool
    ref_matches: bool

    @property
    def ok(self) -> bool:
        return self.content_hash_matches and self.ref_registered and self.ref_matches


class EvidenceService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        gateway: IncidentGateway,
        catalog: ServiceCatalog,
        prometheus: PrometheusAdapter,
        loki: LokiAdapter,
        tempo: TempoAdapter,
        changes: ChangeRegistryAdapter,
        git: GitAdapter,
        history: IncidentHistoryAdapter | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._catalog = catalog
        self._prometheus = prometheus
        self._loki = loki
        self._tempo = tempo
        self._changes = changes
        self._git = git
        self._history = history or IncidentHistoryAdapter(gateway.list_incident_summaries)
        self._clock = clock

    # --- metrics -------------------------------------------------------

    def get_metric_window(
        self,
        incident_id: uuid.UUID,
        *,
        metric: str,
        service: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        def collect(scope: IncidentScope, now: datetime) -> Observation:
            target = scope.service_or_default(service)
            window_start, window_end = scope.window(start, end, now=now)
            return self._prometheus.metric_window(
                metric=metric,
                service=target,
                environment=scope.environment,
                start=window_start,
                end=window_end,
            )

        return self._run(
            incident_id, f"metric_window:{metric}", requested_by, investigation_id, collect
        )

    def get_error_rate(self, incident_id: uuid.UUID, **kwargs: object) -> EvidenceItem:
        return self.get_metric_window(incident_id, metric="error_rate", **kwargs)  # type: ignore[arg-type]

    def get_request_rate(self, incident_id: uuid.UUID, **kwargs: object) -> EvidenceItem:
        return self.get_metric_window(incident_id, metric="request_rate", **kwargs)  # type: ignore[arg-type]

    def get_latency(
        self, incident_id: uuid.UUID, *, quantile: str = "0.95", **kwargs: object
    ) -> EvidenceItem:
        metric = LATENCY_METRICS.get(quantile)
        if metric is None:
            raise InvalidQueryError(f"quantile must be one of {sorted(LATENCY_METRICS)}")
        return self.get_metric_window(incident_id, metric=metric, **kwargs)  # type: ignore[arg-type]

    def get_service_health(
        self,
        incident_id: uuid.UUID,
        *,
        service: str | None = None,
        at: datetime | None = None,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        def collect(scope: IncidentScope, now: datetime) -> Observation:
            target = scope.service_or_default(service)
            _, instant = scope.window(None, at, now=now, default=limits.MIN_WINDOW)
            return self._prometheus.service_health(
                service=target, environment=scope.environment, at=instant
            )

        return self._run(incident_id, "service_health", requested_by, investigation_id, collect)

    # --- logs ----------------------------------------------------------

    def get_logs(
        self,
        incident_id: uuid.UUID,
        *,
        service: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        severities: tuple[str, ...] | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        limit: int | None = None,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        def collect(scope: IncidentScope, now: datetime) -> Observation:
            target = scope.service_or_default(service)
            window_start, window_end = scope.window(start, end, now=now)
            return self._loki.query_logs(
                service=target,
                start=window_start,
                end=window_end,
                severities=tuple(s.upper() for s in severities) if severities else None,
                trace_id=trace_id,
                request_id=request_id,
                limit=bounded_limit(
                    limit,
                    default=limits.MAX_LOG_LINES_RETURNED,
                    maximum=limits.MAX_LOG_LINES_RETURNED,
                ),
            )

        return self._run(incident_id, "logs", requested_by, investigation_id, collect)

    # --- traces --------------------------------------------------------

    def get_trace(
        self,
        incident_id: uuid.UUID,
        *,
        trace_id: str,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        def collect(scope: IncidentScope, now: datetime) -> Observation:
            del now
            observation = self._tempo.get_trace(trace_id=trace_id)
            services = set(observation.normalized_payload.get("services", []))
            if (
                observation.normalized_payload.get("found")
                and not services & scope.allowed_services
            ):
                # Checked on content, before anything is persisted: a trace
                # id is not itself a scope, the services in it are.
                raise ScopeViolationError(
                    f"trace {trace_id} involves only {sorted(services)}, outside incident scope"
                )
            return observation

        return self._run(incident_id, "trace_by_id", requested_by, investigation_id, collect)

    def get_traces(
        self,
        incident_id: uuid.UUID,
        *,
        service: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        mode: str = "recent",
        min_duration_ms: int | None = None,
        limit: int | None = None,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        if mode not in SEARCH_MODES:
            raise InvalidQueryError(f"mode must be one of {SEARCH_MODES}")

        def collect(scope: IncidentScope, now: datetime) -> Observation:
            target = scope.service_or_default(service)
            window_start, window_end = scope.window(start, end, now=now)
            return self._tempo.search(
                service=target,
                start=window_start,
                end=window_end,
                mode=mode,
                min_duration_ms=min_duration_ms,
                limit=bounded_limit(limit, default=10, maximum=limits.MAX_TRACES),
            )

        return self._run(
            incident_id, f"trace_search:{mode}", requested_by, investigation_id, collect
        )

    # --- changes -------------------------------------------------------

    def _change_window(
        self, scope: IncidentScope, start: datetime | None, end: datetime | None, now: datetime
    ) -> tuple[datetime, datetime]:
        return scope.window(
            start,
            end,
            now=now,
            default=limits.MAX_CHANGE_WINDOW,
            max_length=limits.MAX_CHANGE_WINDOW,
        )

    def get_recent_deployments(
        self,
        incident_id: uuid.UUID,
        *,
        service: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        def collect(scope: IncidentScope, now: datetime) -> Observation:
            window_start, window_end = self._change_window(scope, start, end, now)
            return self._changes.deployments(
                service=scope.service_or_default(service),
                environment=scope.environment,
                start=window_start,
                end=window_end,
                limit=bounded_limit(limit, default=10, maximum=limits.MAX_DEPLOYMENTS),
            )

        return self._run(incident_id, "recent_deployments", requested_by, investigation_id, collect)

    def get_config_changes(
        self,
        incident_id: uuid.UUID,
        *,
        service: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        def collect(scope: IncidentScope, now: datetime) -> Observation:
            window_start, window_end = self._change_window(scope, start, end, now)
            return self._changes.config_changes(
                service=scope.service_or_default(service),
                environment=scope.environment,
                start=window_start,
                end=window_end,
                limit=bounded_limit(limit, default=10, maximum=limits.MAX_CONFIG_CHANGES),
            )

        return self._run(incident_id, "config_changes", requested_by, investigation_id, collect)

    def get_code_changes(
        self,
        incident_id: uuid.UUID,
        *,
        service: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        sha: str | None = None,
        limit: int | None = None,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        def collect(scope: IncidentScope, now: datetime) -> Observation:
            target = scope.service_or_default(service)
            window_start, window_end = self._change_window(scope, start, end, now)
            return self._git.code_changes(
                service=target,
                paths=self._catalog.repo_paths(target),
                start=window_start,
                end=window_end,
                sha=sha,
                limit=bounded_limit(limit, default=10, maximum=limits.MAX_COMMITS),
            )

        return self._run(incident_id, "code_changes", requested_by, investigation_id, collect)

    def get_recent_commits(
        self,
        incident_id: uuid.UUID,
        *,
        service: str | None = None,
        limit: int | None = None,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        def collect(scope: IncidentScope, now: datetime) -> Observation:
            target = scope.service_or_default(service)
            return self._git.recent_commits(
                service=target,
                paths=self._catalog.repo_paths(target),
                until=now,
                reference_time=scope.created_at,
                limit=bounded_limit(limit, default=10, maximum=limits.MAX_COMMITS),
            )

        return self._run(incident_id, "recent_commits", requested_by, investigation_id, collect)

    # --- history -------------------------------------------------------

    def search_similar_incidents(
        self,
        incident_id: uuid.UUID,
        *,
        limit: int | None = None,
        requested_by: str = "api",
        investigation_id: uuid.UUID | None = None,
    ) -> EvidenceItem:
        def collect(scope: IncidentScope, now: datetime) -> Observation:
            del now
            return self._history.search_similar(
                incident_id=scope.incident_id,
                service=scope.service,
                environment=scope.environment,
                regions=scope.regions,
                alert_types=scope.alert_types,
                reference_time=scope.created_at,
                limit=bounded_limit(limit, default=5, maximum=limits.MAX_SIMILAR_INCIDENTS),
            )

        return self._run(incident_id, "similar_incidents", requested_by, investigation_id, collect)

    # --- replay / audit ------------------------------------------------

    def get_incident_evidence(self, incident_id: uuid.UUID) -> list[EvidenceRecord]:
        """Every evidence record for an incident, in deterministic order
        (collected_at, then insertion sequence) -- the replay input for a
        future investigation or eval run."""
        with self._session_factory() as session:
            return repository.list_incident_records(session, incident_id)

    def get_evidence(self, evidence_id: uuid.UUID) -> EvidenceRecord:
        with self._session_factory() as session:
            record = repository.get_record(session, evidence_id)
        if record is None:
            raise EvidenceNotFoundError(f"evidence {evidence_id} not found")
        return record

    def verify(self, evidence_id: uuid.UUID) -> IntegrityReport:
        """Recompute the content hash from the stored raw response, and
        check incident-core's reference agrees with the record."""
        record = self.get_evidence(evidence_id)
        refs = {r.id: r for r in self._gateway.list_evidence_refs(record.incident_id)}
        ref = refs.get(evidence_id)
        return IntegrityReport(
            evidence_id=evidence_id,
            content_hash_matches=verify_content_hash(record.raw_response, record.content_hash),
            ref_registered=ref is not None,
            ref_matches=ref is not None
            and ref.content_hash == record.content_hash
            and ref.incident_id == record.incident_id,
        )

    # --- the one path every query takes --------------------------------

    def _run(
        self,
        incident_id: uuid.UUID,
        operation: str,
        requested_by: str,
        investigation_id: uuid.UUID | None,
        collect: Collector,
    ) -> EvidenceItem:
        started = time.monotonic()
        source = "unknown"
        with (
            bind_context(incident_id=str(incident_id)),
            start_span("evidence.query", operation=operation),
        ):
            try:
                scope = resolve_scope(self._gateway, self._catalog, incident_id)
                observation = collect(scope, self._clock())
                source = observation.source_system.value
                record = self._persist(scope, observation, requested_by, investigation_id)
                self._register(record)
            except EvidenceError as exc:
                latency_ms = (time.monotonic() - started) * 1000
                metrics.increment("evidence.query_failed", operation=operation, code=exc.code)
                if isinstance(exc, BackendTimeoutError):
                    metrics.increment("evidence.backend_timeout", operation=operation)
                log.warning(
                    "evidence.query_failed",
                    operation=operation,
                    code=exc.code,
                    error=str(exc),
                    latency_ms=round(latency_ms, 1),
                )
                raise

            latency_ms = (time.monotonic() - started) * 1000
            metrics.observe(
                "evidence.query_latency_ms", latency_ms, operation=operation, source=source
            )
            metrics.increment("evidence.collected", operation=operation, source=source)
            # Identifiers and counts only -- never the payload.
            log.info(
                "evidence.collected",
                evidence_id=str(record.evidence_id),
                evidence_type=record.evidence_type.value,
                source=source,
                operation=operation,
                subject_service=record.subject_service,
                result_count=record.result_count,
                raw_truncated=record.raw_truncated,
                requested_by=requested_by,
                latency_ms=round(latency_ms, 1),
            )
            return record.to_item()

    def _persist(
        self,
        scope: IncidentScope,
        observation: Observation,
        requested_by: str,
        investigation_id: uuid.UUID | None,
    ) -> EvidenceRecord:
        # Round-trip through canonical JSON first: what gets hashed is
        # exactly what JSONB will hand back, so the hash re-verifies from
        # storage (tuples become lists, NUL is stripped, etc.).
        raw = json.loads(canonical_json(strip_nul(observation.raw_response)))
        normalized = json.loads(canonical_json(strip_nul(observation.normalized_payload)))
        collected_at = self._clock()
        record = EvidenceRecord(
            evidence_id=uuid.uuid4(),
            sequence=0,
            incident_id=scope.incident_id,
            investigation_id=investigation_id,
            evidence_type=observation.evidence_type,
            source_system=observation.source_system,
            operation=observation.operation,
            subject_service=observation.subject_service,
            query_spec=json.loads(canonical_json(observation.query_spec)),
            source_reference=json.loads(canonical_json(observation.source_reference)),
            observed_at=observation.observed_at,
            window_start=observation.window_start,
            window_end=observation.window_end,
            collected_at=collected_at,
            expires_at=collected_at + timedelta(days=EVIDENCE_TTL_DAYS)
            if observation.evidence_type in EXPIRING_TYPES
            else None,
            requested_by=requested_by,
            content_hash=content_hash(raw),
            raw_response=raw,
            raw_truncated=observation.raw_truncated,
            normalized_payload=normalized,
            summary=observation.summary,
            result_count=observation.result_count,
        )
        with self._session_factory() as session:
            stored = repository.insert_record(session, record)
            session.commit()
        return stored

    def _register(self, record: EvidenceRecord) -> None:
        try:
            self._gateway.register_evidence_ref(
                RegisterEvidenceRefCommand(
                    evidence_id=record.evidence_id,
                    incident_id=record.incident_id,
                    investigation_id=record.investigation_id,
                    evidence_type=record.evidence_type.value,
                    content_hash=record.content_hash,
                    source_system=record.source_system.value,
                    collected_at=record.collected_at,
                )
            )
        except Exception:
            # The record exists but is unreferenced: it is never returned,
            # so it can never be cited (citations validate against
            # evidence_refs). Visible in logs/metrics, harmless otherwise.
            metrics.increment("evidence.ref_registration_failed")
            log.exception("evidence.ref_registration_failed", evidence_id=str(record.evidence_id))
            raise
