"""Evidence persistence, references, immutability, provenance and replay,
against real Postgres (both schemas, both roles). Telemetry backends are
canned (MockTransport) here -- the live-backend adapter tests are
test_telemetry_adapters.py; this file is about the store."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from packages.domain.commands import RegisterEvidenceRefCommand
from packages.domain.errors import EvidenceRefConflictError
from packages.domain.errors import IncidentNotFoundError as CoreIncidentNotFound
from packages.evidence.adapters.changes import ChangeRegistryAdapter
from packages.evidence.adapters.git import GitAdapter
from packages.evidence.adapters.loki import LokiAdapter
from packages.evidence.adapters.prometheus import PrometheusAdapter
from packages.evidence.adapters.tempo import TempoAdapter
from packages.evidence.errors import IncidentNotFoundError, ScopeViolationError
from packages.evidence.hashing import content_hash
from packages.evidence.scope import ServiceCatalog
from packages.evidence.service import EvidenceService
from tests.factories import make_alert_command

ROOT = Path(__file__).resolve().parents[2]


def _prometheus_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("query_range"):
        start = float(request.url.params["start"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [{"metric": {}, "values": [[start, "0.2"], [start + 60, "0.3"]]}]
                },
            },
        )
    return httpx.Response(
        200, json={"status": "success", "data": {"result": [{"metric": {}, "value": [1, "0.3"]}]}}
    )


@pytest.fixture
def evidence(core, evidence_session_factory, test_redis) -> EvidenceService:
    def client(handler) -> httpx.Client:
        return httpx.Client(base_url="http://canned", transport=httpx.MockTransport(handler))

    return EvidenceService(
        session_factory=evidence_session_factory,
        gateway=core,
        catalog=ServiceCatalog.load(ROOT / "infrastructure/evidence/service-catalog.json"),
        prometheus=PrometheusAdapter(client(_prometheus_handler)),
        loki=LokiAdapter(client(lambda r: httpx.Response(503))),
        tempo=TempoAdapter(client(lambda r: httpx.Response(404))),
        changes=ChangeRegistryAdapter(test_redis),
        git=GitAdapter(ROOT),
    )


@pytest.fixture
def incident_id(core) -> uuid.UUID:
    result = core.handle_alert_received(
        make_alert_command(service="checkout-service", extra_labels={"region": "us-east-1"})
    )
    return result.incident_id


def test_evidence_is_persisted_referenced_and_resolvable(evidence, core, incident_id):
    item = evidence.get_error_rate(incident_id, requested_by="test")

    record = evidence.get_evidence(item.evidence_id)
    assert record.to_item() == item
    assert record.content_hash == content_hash(record.raw_response)
    [ref] = core.list_evidence_refs(incident_id)
    assert (ref.id, ref.content_hash, ref.source_system) == (
        item.evidence_id,
        item.content_hash,
        "prometheus",
    )
    assert evidence.verify(item.evidence_id).ok

    provenance = record.provenance
    assert provenance.incident_id == incident_id
    assert provenance.requested_by == "test"
    assert provenance.operation == "metric_window:error_rate"
    assert "promql" in provenance.query_spec
    assert provenance.source_reference["endpoint"].startswith("http://canned")
    assert provenance.window_start is not None and provenance.collected_at >= provenance.observed_at
    assert record.expires_at is not None  # metrics go stale


def test_evidence_records_and_refs_are_immutable_in_the_database(
    evidence, evidence_engine, engine, incident_id
):
    item = evidence.get_error_rate(incident_id)
    for db, table in (
        (evidence_engine, "evidence.evidence_records"),
        (engine, "incident_core.evidence_refs"),
    ):
        for statement in (
            f"UPDATE {table} SET content_hash = 'sha256:{'0' * 64}' WHERE id = :id",
            f"DELETE FROM {table} WHERE id = :id",
        ):
            with pytest.raises(DBAPIError, match="immutable"), db.begin() as conn:
                conn.execute(text(statement), {"id": item.evidence_id})
    assert evidence.verify(item.evidence_id).ok


def test_each_role_is_confined_to_its_own_schema(evidence_engine, engine):
    with pytest.raises(DBAPIError, match="permission denied"), engine.begin() as conn:
        conn.execute(text("SELECT 1 FROM evidence.evidence_records"))
    with pytest.raises(DBAPIError, match="permission denied"), evidence_engine.begin() as conn:
        conn.execute(text("SELECT 1 FROM incident_core.incidents"))


def test_a_retried_query_creates_a_new_record_and_never_overwrites(evidence, incident_id):
    first = evidence.get_error_rate(incident_id)
    second = evidence.get_error_rate(incident_id)
    assert first.evidence_id != second.evidence_id
    assert second.collected_at >= first.collected_at
    assert evidence.get_evidence(first.evidence_id).to_item() == first


def test_incident_evidence_replays_in_deterministic_order(evidence, test_redis, incident_id):
    from simulator.changes.registry import ChangeRegistry

    ChangeRegistry(test_redis).seed()
    made = [
        evidence.get_error_rate(incident_id),
        evidence.get_recent_deployments(incident_id),
        evidence.get_config_changes(incident_id),
        evidence.get_recent_commits(incident_id),
        evidence.search_similar_incidents(incident_id),
    ]
    replay = evidence.get_incident_evidence(incident_id)
    assert [r.evidence_id for r in replay] == [m.evidence_id for m in made]
    assert [r.sequence for r in replay] == sorted(r.sequence for r in replay)
    assert evidence.get_incident_evidence(incident_id) == replay


def test_rejected_queries_persist_nothing(evidence, core, incident_id):
    with pytest.raises(ScopeViolationError):
        evidence.get_error_rate(incident_id, service="inventory-service")
    with pytest.raises(IncidentNotFoundError):
        evidence.get_error_rate(uuid.uuid4())
    assert evidence.get_incident_evidence(incident_id) == []
    assert core.list_evidence_refs(incident_id) == []


def test_a_backend_failure_persists_nothing(evidence, incident_id):
    from packages.evidence.errors import BackendUnavailableError

    with pytest.raises(BackendUnavailableError):
        evidence.get_logs(incident_id)
    assert evidence.get_incident_evidence(incident_id) == []


def test_ref_registration_is_idempotent_and_refuses_conflicting_content(core, incident_id):
    command = RegisterEvidenceRefCommand(
        evidence_id=uuid.uuid4(),
        incident_id=incident_id,
        evidence_type="metric",
        content_hash="sha256:" + "a" * 64,
        source_system="prometheus",
        collected_at=datetime.now(UTC),
    )
    assert core.register_evidence_ref(command).newly_registered
    assert not core.register_evidence_ref(command).newly_registered
    with pytest.raises(EvidenceRefConflictError):
        core.register_evidence_ref(
            command.model_copy(update={"content_hash": "sha256:" + "b" * 64})
        )
    with pytest.raises(CoreIncidentNotFound):
        core.register_evidence_ref(command.model_copy(update={"incident_id": uuid.uuid4()}))
